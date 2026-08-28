"""EVIE voice session lifecycle: wake → verify once → active → follow-up → idle."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audio.vad import default_vad_engine
from app.compliance.policy import remote_processing_allowed
from app.config import settings
from app.models import (
    ConsentRecord,
    VoiceAttemptLog,
    VoiceEnrollment,
    VoicePrint,
    VoiceSession,
)
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.training.consent import ConsentRequiredError, require_consent
from app.utils.text import utcnow
from app.voice.anti_spoof import LivenessGate, ReplayError, ReplayGuard
from app.voice.asr import get_transcriber
from app.voice.contracts import (
    LivenessChecker,
    SpeakerVerifier,
    SpeechStyle,
    SynthesisResult,
    Transcriber,
    Transcript,
    TranscriptPartial,
    VoiceError,
    WakeDetection,
    WakeWordEngine,
)
from app.voice.pipeline import (
    PipelineOutcome,
    TtsChunk,
    cached_listen_ack,
    persist_tts_audio,
    run_chat_tts_pipeline,
    stream_chat_tts_pipeline,
    transcribe_input,
)
from app.voice.security import decrypt_payload, encrypt_payload
from app.voice.sensitive import REVERIFY_PURPOSE, classify_sensitive
from app.voice.speaker import default_speaker_verifier
from app.voice.tts import get_synthesizer
from app.voice.wake import configured_wake_engine


class VoiceState:
    IDLE = "idle"
    VERIFYING = "verifying"
    AWAKE = "awake"
    PROCESSING = "processing"
    RESPONDING = "responding"
    FOLLOW_UP = "follow_up"
    REFUSED = "refused"
    ENDED = "ended"


ACTIVE_STATES = {
    VoiceState.VERIFYING,
    VoiceState.AWAKE,
    VoiceState.PROCESSING,
    VoiceState.RESPONDING,
    VoiceState.FOLLOW_UP,
}

BUSY_STATES = {VoiceState.PROCESSING, VoiceState.RESPONDING}
# If a chat/TTS request dies mid-flight, PROCESSING is committed and the
# always-on mic 409s forever. After this many seconds, treat it as crashed.
STALE_BUSY_SECONDS = 60.0
LOGGER = logging.getLogger("ev.voice.lifecycle")

_IN_FLIGHT_SESSIONS: set[str] = set()
_PENDING_INGEST: dict[str, dict] = {}


def mark_session_in_flight(session_id: str) -> None:
    _IN_FLIGHT_SESSIONS.add(str(session_id))


def clear_session_in_flight(session_id: str) -> None:
    _IN_FLIGHT_SESSIONS.discard(str(session_id))


def session_in_flight(session_id: str) -> bool:
    return str(session_id) in _IN_FLIGHT_SESSIONS


def queue_pending_ingest(device_id: str, payload: dict) -> None:
    _PENDING_INGEST[str(device_id)] = payload


def pop_pending_ingest(device_id: str) -> dict | None:
    return _PENDING_INGEST.pop(str(device_id), None)


def pending_ingest_for(device_id: str) -> dict | None:
    return _PENDING_INGEST.get(str(device_id))


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=utcnow().tzinfo)


def _normalized_phrase(text: str) -> str:
    """Lowercase, punctuation-free phrase for sleep-phrase matching."""

    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def _window_remaining_seconds(until, now=None) -> int:
    now = now or utcnow()
    stamp = _aware(until)
    if stamp is None:
        return 0
    return max(0, int((stamp - now).total_seconds()))


def follow_up_hint_expired(until, now=None) -> bool:
    """True when the short REST follow-up hint has elapsed.

    The hint is not a session door. Only the long idle lock (`expires_at`),
    a sleep phrase, or an explicit end close the session.
    """

    return _window_remaining_seconds(until, now) <= 0


def idle_lock_expired(expires_at, now=None) -> bool:
    stamp = _aware(expires_at)
    if stamp is None:
        return False
    now = now or utcnow()
    return stamp < now


def is_sleep_phrase(text: str, phrases: list[str] | None = None) -> bool:
    phrases = list(settings.voice_sleep_phrases) if phrases is None else list(phrases)
    normalized = _normalized_phrase(text)
    return any(normalized == _normalized_phrase(phrase) for phrase in phrases)


@dataclass
class WakeOutcome:
    session_id: str | None
    state: str
    owner_enrolled: bool
    challenge_nonce: str | None = None
    challenge_phrase: str | None = None
    message: str | None = None
    transcript: str | None = None
    reply: str | None = None
    tts: SynthesisResult | None = None
    greeting: str | None = None
    onboarding: str | None = None
    conversation_id: str | None = None


@dataclass
class EarsIngestOutcome:
    accepted: bool
    message: str | None = None
    session_id: str | None = None
    state: str | None = None
    listening: bool = False
    queued: bool = False
    transcript: str | None = None
    reply: str | None = None
    tts: SynthesisResult | None = None
    playback_owner: str = "ears"
    command_deferred: bool = False


@dataclass
class VerifyOutcome:
    session_id: str | None
    state: str
    verified: bool
    confidence: float = 0.0
    reason: str = ""
    conversation_id: str | None = None
    greeting: str | None = None
    onboarding: str | None = None


@dataclass
class UtteranceOutcome:
    session_id: str
    state: str
    transcript: Transcript
    reply: str
    conversation_id: str | None = None
    tts: SynthesisResult | None = None
    style: SpeechStyle | None = None
    model: str | None = None
    context_tokens: int = 0
    memory_deltas: list[dict] | None = None
    error: str | None = None


@dataclass
class SessionStatus:
    session_id: str | None
    state: str
    owner_enrolled: bool
    owner_verified: bool = False
    device_id: str | None = None
    speaker_confidence: float | None = None
    follow_up_remaining_seconds: int = 0
    expires_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: str | None = None


class VoiceRuntime:
    """Orchestrates the full voice lifecycle with privacy and owner-only gates.

    All engines are provider-agnostic protocols; swapping wake/ASR/TTS/speaker
    implementations does not change lifecycle semantics.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        master_key: str,
        actor: str = "voice",
        wake_engine: WakeWordEngine | None = None,
        verifier: SpeakerVerifier | None = None,
        transcriber: Transcriber | None = None,
        synthesizer=None,
        liveness: LivenessChecker | None = None,
        follow_up_seconds: int | None = None,
        verify_timeout_seconds: int | None = None,
        session_timeout_seconds: int | None = None,
        vad_engine=None,
        addressivity_enabled: bool | None = None,
        vad_threshold: float | None = None,
        sleep_phrases: list[str] | None = None,
    ) -> None:
        self.session = session
        self.master_key = master_key
        self.actor = actor
        self.wake_engine = wake_engine or configured_wake_engine()
        self.verifier = verifier or default_speaker_verifier()
        self.transcriber = transcriber or get_transcriber()
        try:
            self.synthesizer = synthesizer or get_synthesizer()
        except Exception:  # noqa: BLE001 - Talk/wake must not 500 on TTS config
            from app.voice.tts import MetaSynthesizer

            self.synthesizer = synthesizer or MetaSynthesizer()
        self.liveness = liveness or LivenessGate()
        self.follow_up_seconds = (
            follow_up_seconds
            if follow_up_seconds is not None
            else settings.voice_follow_up_seconds
        )
        self.verify_timeout_seconds = (
            verify_timeout_seconds
            if verify_timeout_seconds is not None
            else settings.voice_verify_timeout_seconds
        )
        self.session_timeout_seconds = (
            session_timeout_seconds
            if session_timeout_seconds is not None
            else settings.voice_session_timeout_seconds
        )
        self.vad_engine = vad_engine
        self.addressivity_enabled = (
            settings.voice_addressivity_enabled
            if addressivity_enabled is None
            else addressivity_enabled
        )
        self.vad_threshold = (
            vad_threshold
            if vad_threshold is not None
            else settings.voice_addressivity_vad_threshold
        )
        self.sleep_phrases = (
            list(settings.voice_sleep_phrases)
            if sleep_phrases is None
            else list(sleep_phrases)
        )
        self.continuity_conversation_id = settings.voice_continuity_conversation_id
        self.replay_guard = ReplayGuard(session)
        self._interrupts: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _log(
        self,
        kind: str,
        outcome: str,
        *,
        session_id=None,
        device_id: str | None = None,
        reason: str | None = None,
        **metadata,
    ) -> None:
        self.session.add(
            VoiceAttemptLog(
                device_id=device_id,
                kind=kind,
                outcome=outcome,
                session_id=session_id,
                reason=reason,
                metadata_=metadata,
            )
        )
        await self.session.flush()

    async def _expire_stale(self) -> None:
        now = utcnow()
        try:
            result = await self.session.execute(
                select(VoiceSession).where(
                    VoiceSession.ended_at.is_(None),
                    VoiceSession.expires_at.is_not(None),
                    VoiceSession.expires_at < now,
                )
            )
        except Exception as exc:  # noqa: BLE001 - map schema drift to a spoken 503
            from sqlalchemy.exc import ProgrammingError

            if isinstance(exc, ProgrammingError) or "does not exist" in str(exc):
                raise VoiceError(
                    "Voice database is behind. From backend run: alembic upgrade head",
                    status=503,
                    code="schema_behind",
                ) from exc
            raise
        for row in result.scalars().all():
            if not idle_lock_expired(row.expires_at, now):
                continue
            prior_state = row.state
            row.state = VoiceState.ENDED
            row.ended_at = now
            row.end_reason = (
                "silence-lock"
                if prior_state in (
                    VoiceState.AWAKE,
                    VoiceState.PROCESSING,
                    VoiceState.RESPONDING,
                    VoiceState.FOLLOW_UP,
                )
                else "verification timeout"
            )
            await self._log(
                "timeout",
                "timeout",
                session_id=row.id,
                device_id=row.device_id,
                reason=row.end_reason,
            )

    async def _active_session(self, device_id: str) -> VoiceSession | None:
        result = await self.session.execute(
            select(VoiceSession)
            .where(
                VoiceSession.device_id == device_id,
                VoiceSession.ended_at.is_(None),
                VoiceSession.state.in_(ACTIVE_STATES),
            )
            .order_by(VoiceSession.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _latest_session(self, device_id: str) -> VoiceSession | None:
        """Most recent session for this device, including one that already ended.

        Talk reuses the menu-bar's session id. After sleep / idle lock the
        row is ENDED, so ``_active_session`` misses it and a new wake would
        mint a second id — leaving the button stuck on the dead one.
        """

        result = await self.session.execute(
            select(VoiceSession)
            .where(VoiceSession.device_id == device_id)
            .order_by(VoiceSession.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_session(self, session_id) -> VoiceSession:
        if isinstance(session_id, str):
            session_id = UUID(session_id)
        row = await self.session.get(VoiceSession, session_id)
        if row is None:
            raise VoiceError("Voice session not found", status=404, code="session_not_found")
        return row

    async def _current_enrollment(self) -> VoiceEnrollment | None:
        result = await self.session.execute(
            select(VoiceEnrollment)
            .where(
                VoiceEnrollment.is_current.is_(True),
                VoiceEnrollment.status == "active",
                VoiceEnrollment.redacted.is_(False),
            )
            .order_by(VoiceEnrollment.version.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _current_voiceprint(self) -> VoicePrint | None:
        result = await self.session.execute(
            select(VoicePrint)
            .where(
                VoicePrint.is_current.is_(True),
                VoicePrint.redacted.is_(False),
            )
            .order_by(VoicePrint.version.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _require_voice_consent(self) -> ConsentRecord:
        try:
            return await require_consent(self.session, "voice_enrollment")
        except ConsentRequiredError as exc:
            raise VoiceError(
                "Voice enrollment requires active consent; grant it in the privacy center",
                status=403,
                code="consent_required",
            ) from exc

    def _enforce_remote_voiceprint_gate(self) -> None:
        """Regional policy: remote encoders need an explicit processing gate."""
        if settings.voiceprint_provider == "http" and not remote_processing_allowed(
            "voice_enrollment"
        ):
            raise VoiceError(
                "Remote voiceprint processing is denied by regional policy",
                status=403,
                code="remote_processing_denied",
            )

    async def _decrypt_enrollment(self, enrollment: VoiceEnrollment) -> dict:
        if not enrollment.ciphertext or not enrollment.salt:
            raise VoiceError(
                "Voiceprint payload is missing (revoked or deleted)",
                status=409,
                code="enrollment_unavailable",
            )
        try:
            return decrypt_payload(
                enrollment.ciphertext,
                enrollment.salt,
                master_key=self.master_key,
            )
        except ValueError as exc:
            raise VoiceError(str(exc), status=500, code="voiceprint_decrypt_failed") from exc

    # ------------------------------------------------------------------ #
    # Enrollment
    # ------------------------------------------------------------------ #

    async def enroll(self, samples: list[dict], *, reason: str | None = None) -> VoiceEnrollment:
        consent = await self._require_voice_consent()
        self._enforce_remote_voiceprint_gate()
        if len(samples) < 5:
            raise VoiceError("Enrollment requires at least 5 voice samples", code="enroll_samples")
        for index, sample in enumerate(samples, start=1):
            live_ok, live_conf, live_reason = await self.liveness.check(sample=sample)
            if not live_ok:
                raise VoiceError(
                    f"Enrollment sample {index} failed liveness: {live_reason}",
                    status=422,
                    code="enroll_liveness",
                )
        payload = await self.verifier.enroll(samples, reason=reason)
        ciphertext, salt = encrypt_payload(payload, master_key=self.master_key)
        current = await self._current_enrollment()
        next_version = (current.version + 1) if current else 1
        row = VoiceEnrollment(
            version=next_version,
            is_current=True,
            algorithm=self.verifier.name,
            embedding_dim=payload.get("dim", 0),
            threshold=payload.get("threshold", 0.82),
            sample_count=len(samples),
            ciphertext=ciphertext,
            salt=salt,
            consent_id=consent.id,
            supersedes_id=current.id if current else None,
            reason_for_change=reason,
        )
        if current:
            current.is_current = False
            current.superseded_by_id = row.id
        self.session.add(row)
        await self.session.flush()
        previous_print = await self._current_voiceprint()
        now = utcnow()
        if previous_print is not None:
            previous_print.is_current = False
            previous_print.valid_until = now
        voiceprint = VoicePrint(
            enrollment_id=row.id,
            version=next_version,
            embedding_ciphertext=ciphertext,
            embedding_salt=salt,
            threshold=row.threshold,
            sample_hashes=[
                hashlib.sha256(
                    json.dumps(sample, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()
                for sample in samples
            ],
            is_current=True,
            supersedes_id=previous_print.id if previous_print else None,
            valid_from=now,
        )
        self.session.add(voiceprint)
        await self.session.flush()
        from app.ev.training_wheels import mark_step_from_event

        await mark_step_from_event(self.session, "speaker_enroll")
        await mark_step_from_event(self.session, "mic_permission")
        await self._log(
            "enroll",
            "accepted",
            reason=reason,
            enrollment_version=next_version,
            sample_count=len(samples),
        )
        return row

    async def list_enrollments(self) -> list[VoiceEnrollment]:
        result = await self.session.execute(
            select(VoiceEnrollment).order_by(VoiceEnrollment.created_at.desc())
        )
        return list(result.scalars().all())

    async def _get_enrollment(self, enrollment_id: UUID) -> VoiceEnrollment:
        row = await self.session.get(VoiceEnrollment, enrollment_id)
        if row is None:
            raise VoiceError("Voice enrollment not found", status=404, code="enrollment_not_found")
        return row

    async def revoke(self, enrollment_id: UUID, *, reason: str) -> VoiceEnrollment:
        row = await self._get_enrollment(enrollment_id)
        if row.status == "deleted":
            raise VoiceError("Enrollment is already deleted", status=409, code="already_deleted")
        if row.status != "active":
            raise VoiceError("Enrollment is not active", status=409, code="not_active")
        now = utcnow()
        row.status = "revoked"
        row.is_current = False
        row.redacted = True
        row.reason_for_change = reason
        row.updated_at = now
        await self._log(
            "enrollment",
            "revoked",
            reason=reason,
            enrollment_id=str(row.id),
            version=row.version,
        )
        return row

    async def revoke_all(self, *, reason: str) -> int:
        result = await self.session.execute(
            select(VoiceEnrollment).where(VoiceEnrollment.status == "active")
        )
        count = 0
        for row in result.scalars().all():
            await self.revoke(row.id, reason=reason)
            count += 1
        return count

    async def delete(self, enrollment_id: UUID, *, reason: str) -> VoiceEnrollment:
        """Data-subject deletion: redact the encrypted biometric payload."""
        row = await self._get_enrollment(enrollment_id)
        now = utcnow()
        row.status = "deleted"
        row.is_current = False
        row.redacted = True
        row.ciphertext = None
        row.salt = None
        row.embedding_dim = 0
        row.reason_for_change = reason
        row.updated_at = now
        prints = (
            await self.session.execute(
                select(VoicePrint).where(VoicePrint.enrollment_id == row.id)
            )
        ).scalars().all()
        for voiceprint in prints:
            voiceprint.embedding_ciphertext = None
            voiceprint.embedding_salt = None
            voiceprint.is_current = False
            voiceprint.redacted = True
            voiceprint.valid_until = now
        await self._log(
            "enrollment",
            "deleted",
            reason=reason,
            enrollment_id=str(row.id),
            version=row.version,
        )
        return row

    async def rollback(
        self, enrollment_id: UUID, *, target_version: int, reason: str
    ) -> VoiceEnrollment:
        row = await self._get_enrollment(enrollment_id)
        if row.status != "active":
            raise VoiceError("Only active enrollments can be rolled back", status=409, code="not_active")
        if row.status == "deleted" or row.redacted or row.ciphertext is None:
            raise VoiceError("Enrollment data has been deleted", status=409, code="deleted")
        if row.version == target_version:
            return row
        cursor: VoiceEnrollment | None = row
        while cursor is not None and cursor.version != target_version:
            if cursor.supersedes_id is None:
                break
            cursor = await self.session.get(VoiceEnrollment, cursor.supersedes_id)
        if cursor is None or cursor.version != target_version:
            raise VoiceError(
                f"Version {target_version} not found in this enrollment chain",
                status=404,
                code="version_not_found",
            )
        if cursor.status == "deleted" or cursor.redacted or cursor.ciphertext is None:
            raise VoiceError("Target version has been deleted", status=409, code="deleted")
        row.is_current = False
        row.superseded_by_id = cursor.id
        cursor.is_current = True
        cursor.status = "active"
        cursor.redacted = False
        cursor.superseded_by_id = None
        cursor.reason_for_change = reason
        row.updated_at = utcnow()
        print_rows = (
            await self.session.execute(
                select(VoicePrint).where(VoicePrint.enrollment_id.in_([row.id, cursor.id]))
            )
        ).scalars().all()
        for voiceprint in print_rows:
            voiceprint.is_current = voiceprint.version == target_version
            if voiceprint.version == target_version:
                voiceprint.redacted = False
                voiceprint.valid_until = None
        await self._log(
            "enrollment",
            "rolled_back",
            reason=reason,
            enrollment_id=str(row.id),
            target_version=target_version,
        )
        return cursor

    async def export_voiceprints(self) -> dict:
        """Portable biometric export: consents + enrollment metadata + decrypted templates."""
        consents = list(
            (
                await self.session.execute(
                    select(ConsentRecord).order_by(ConsentRecord.granted_at.desc())
                )
            )
            .scalars()
            .all()
        )
        enrollments = await self.list_enrollments()
        voiceprints: list[dict] = []
        for enrollment in enrollments:
            if enrollment.status != "active" or enrollment.redacted or not enrollment.ciphertext:
                continue
            try:
                payload = await self._decrypt_enrollment(enrollment)
            except VoiceError:
                continue
            voiceprints.append(
                {
                    "id": str(enrollment.id),
                    "version": enrollment.version,
                    "algorithm": enrollment.algorithm,
                    "embedding_dim": enrollment.embedding_dim,
                    "threshold": enrollment.threshold,
                    "sample_count": enrollment.sample_count,
                    "embedding": payload.get("embedding"),
                    "is_current": enrollment.is_current,
                    "supersedes_id": str(enrollment.supersedes_id)
                    if enrollment.supersedes_id
                    else None,
                    "reason_for_change": enrollment.reason_for_change,
                    "created_at": enrollment.created_at.isoformat(),
                }
            )
        return {"consents": consents, "enrollments": enrollments, "voiceprints": voiceprints}

    async def enrollment_status(self) -> dict:
        enrollment = await self._current_enrollment()
        if enrollment is None:
            return {"enrolled": False, "version": None, "algorithm": None, "sample_count": 0}
        return {
            "enrolled": True,
            "version": enrollment.version,
            "algorithm": enrollment.algorithm,
            "sample_count": enrollment.sample_count,
            "threshold": enrollment.threshold,
            "created_at": enrollment.created_at.isoformat(),
        }

    async def verify_samples(self, samples: list[dict]) -> dict:
        """Stateless speaker verification (training API): score vs current voiceprint."""
        await self._require_voice_consent()
        self._enforce_remote_voiceprint_gate()
        enrollment = await self._current_enrollment()
        if enrollment is None:
            return {
                "accepted": False,
                "score": 0.0,
                "threshold": settings.voiceprint_threshold,
                "enrollment_id": None,
                "version": None,
                "reason": "no_voiceprint_enrolled",
            }
        if enrollment.status != "active" or enrollment.redacted or not enrollment.ciphertext:
            return {
                "accepted": False,
                "score": 0.0,
                "threshold": enrollment.threshold,
                "enrollment_id": str(enrollment.id),
                "version": enrollment.version,
                "reason": "enrollment_unavailable",
            }
        payload = await self._decrypt_enrollment(enrollment)
        scores: list[float] = []
        for sample in samples:
            decision = await self.verifier.verify(
                sample,
                enrolled_payload=payload,
                threshold=enrollment.threshold,
            )
            scores.append(decision.confidence)
        score = round(sum(scores) / len(scores), 4)
        accepted = score >= enrollment.threshold
        return {
            "accepted": accepted,
            "score": score,
            "threshold": enrollment.threshold,
            "enrollment_id": str(enrollment.id),
            "version": enrollment.version,
            "reason": "ok" if accepted else "score_below_threshold",
        }

    # ------------------------------------------------------------------ #
    # Wake
    # ------------------------------------------------------------------ #

    def _refresh_listen_window(self, row: VoiceSession, *, now=None) -> None:
        """Reset the short hint and the long idle lock after an owner turn."""

        now = now or utcnow()
        row.state = VoiceState.FOLLOW_UP
        row.last_utterance_at = now
        row.follow_up_until = now + timedelta(seconds=self.follow_up_seconds)
        row.expires_at = now + timedelta(seconds=self.session_timeout_seconds)

    async def _bind_live_thread(self, row: VoiceSession) -> str:
        from app.ev.assistant import bind_live_thread

        thread = await bind_live_thread(self.session)
        row.conversation_id = thread.id
        return str(thread.id)

    APP_LIVE_VERIFIERS = frozenset({"push_to_talk", "app_open"})

    def _reuse_push_to_talk_session(
        self, row: VoiceSession, *, verifier_name: str | None = None
    ) -> None:
        """Re-open a Talk / app-open session in place, including one that ended.

        The menu-bar keeps ``sessionId`` across presses. Sleep phrases, the
        idle lock, and SSE ``session_ended`` errors used to leave that id
        pointing at an ENDED row, so every later Talk press failed with
        "wake EVIE again". Talk and in-app live are already owner-authenticated,
        so revive.
        """

        now = utcnow()
        row.state = VoiceState.AWAKE
        row.owner_verified = True
        row.verifier_name = (
            verifier_name or row.verifier_name or "push_to_talk"
        )
        row.ended_at = None
        row.end_reason = None
        row.expires_at = now + timedelta(seconds=self.session_timeout_seconds)
        row.follow_up_until = now + timedelta(seconds=self.session_timeout_seconds)

    async def open_live_session(self, *, device_id: str) -> WakeOutcome:
        """Open a full-duplex live conversation without a wake word.

        Opening EV.app is the door. The owner is already authenticated by
        the API key; there is no Evie gate on this path.
        """

        await self._expire_stale()
        from app.ev.fleet import resolve_registry_device

        resolved = await resolve_registry_device(self.session, device_id)
        canonical = str(resolved.id) if resolved is not None else device_id
        return await self._begin_push_to_talk_session(
            device_id=canonical, wake_word="evie", verifier_name="app_open"
        )

    async def refresh_live_lease(self, session_id) -> None:
        """Keep an open live WebSocket from idle-locking mid-conversation."""

        row = await self._get_session(session_id)
        if row.ended_at is not None:
            return
        now = utcnow()
        row.expires_at = now + timedelta(seconds=self.session_timeout_seconds)
        row.follow_up_until = now + timedelta(seconds=self.session_timeout_seconds)
        if row.state not in {
            VoiceState.AWAKE,
            VoiceState.FOLLOW_UP,
            VoiceState.PROCESSING,
            VoiceState.RESPONDING,
        }:
            self._reuse_push_to_talk_session(row)

    async def _begin_push_to_talk_session(
        self,
        *,
        device_id: str,
        wake_word: str,
        verifier_name: str = "push_to_talk",
    ) -> WakeOutcome:
        """Open or reuse an AWAKE session for Talk or in-app live.

        The client is already authenticated by the API key. A second open on a
        live session must keep the same session (and conversation thread).
        Stale PROCESSING/RESPONDING is recovered in place so a hung turn
        cannot 409 forever; a healthy follow-up is never killed.
        """

        await self._require_voice_consent()
        existing = await self._active_session(device_id)
        if existing is None:
            existing = await self._latest_session(device_id)
        enrollment = await self._current_enrollment()
        if existing is not None:
            from app.ev import assistant as assistant_mod

            prior_state = existing.state
            was_ended = existing.ended_at is not None
            self._reuse_push_to_talk_session(existing, verifier_name=verifier_name)
            if existing.conversation_id is None:
                await self._bind_live_thread(existing)
            greeting = None
            onboarding = None
            if was_ended:
                existing.greeted_at = None
                awake = await assistant_mod.companion_on_awake(
                    self.session, existing, actor=self.actor
                )
                greeting = awake.greeting
                onboarding = awake.onboarding
            await self.session.flush()
            await self._log(
                "wake",
                "accepted",
                session_id=existing.id,
                device_id=device_id,
                reason=f"{verifier_name}-reuse",
                wake_word=wake_word,
                prior_state=prior_state,
            )
            return WakeOutcome(
                session_id=str(existing.id),
                state=VoiceState.AWAKE,
                owner_enrolled=enrollment is not None,
                message="Listening.",
                greeting=greeting,
                onboarding=onboarding,
                conversation_id=(
                    str(existing.conversation_id) if existing.conversation_id else None
                ),
            )
        service = EventService(self.session, actor=self.actor)
        wake_event = await service.create(
            EventCreate(
                source="voice",
                event_type="voice.wake",
                content={
                    "wake_word": wake_word,
                    "confidence": 1.0,
                    "stage": "burst",
                    "power_state": "burst",
                    "engine": verifier_name,
                },
                metadata={"device_id": device_id},
                device_id=device_id,
                privacy_level="sensitive",
            ),
            request_id=None,
        )
        now = utcnow()
        row = VoiceSession(
            device_id=device_id,
            wake_word=wake_word,
            state=VoiceState.AWAKE,
            owner_verified=True,
            speaker_confidence=1.0,
            verifier_name=verifier_name,
            wake_confidence=1.0,
            wake_event_id=wake_event.id,
            verified_at=now,
            expires_at=now + timedelta(seconds=self.session_timeout_seconds),
            follow_up_until=now + timedelta(seconds=self.session_timeout_seconds),
        )
        self.session.add(row)
        await self.session.flush()
        await self._bind_live_thread(row)
        from app.ev import assistant as assistant_mod

        awake = await assistant_mod.companion_on_awake(
            self.session, row, actor=self.actor
        )
        await self.session.flush()
        await self._log(
            "wake",
            "accepted",
            session_id=row.id,
            device_id=device_id,
            reason=verifier_name,
            wake_word=wake_word,
        )
        return WakeOutcome(
            session_id=str(row.id),
            state=VoiceState.AWAKE,
            owner_enrolled=enrollment is not None,
            message="Listening.",
            greeting=awake.greeting,
            onboarding=awake.onboarding,
            conversation_id=str(row.conversation_id) if row.conversation_id else None,
        )

    async def _fallback_utterance(
        self,
        *,
        row: VoiceSession,
        transcript: Transcript,
        reply: str,
        error: str | None = None,
    ) -> UtteranceOutcome:
        """Spoken recovery when ASR/chat/TTS would otherwise hang or 5xx."""

        style = SpeechStyle(brevity=0.9, warmth=0.6)
        try:
            tts = await asyncio.wait_for(
                self.synthesizer.synthesize(reply, style=style),
                timeout=float(settings.voice_tts_timeout_seconds),
            )
            tts = await persist_tts_audio(tts)
        except Exception:  # noqa: BLE001 - text reply is enough
            tts = SynthesisResult(
                text=reply,
                provider=getattr(self.synthesizer, "name", "none"),
                style=style,
                degraded=True,
                details={"reason": "fallback"},
            )
        self._remember_spoken_reply(row.device_id, reply, tts=tts)
        self._refresh_listen_window(row)
        await self._log(
            "utterance",
            "accepted",
            session_id=row.id,
            device_id=row.device_id,
            reason="fallback",
            reply_chars=len(reply),
        )
        return UtteranceOutcome(
            session_id=str(row.id),
            state=VoiceState.FOLLOW_UP,
            transcript=transcript,
            reply=reply,
            conversation_id=None,
            tts=tts,
            style=style,
            model=None,
            context_tokens=0,
            memory_deltas=[],
            error=error or reply,
        )

    async def handle_wake(
        self,
        *,
        device_id: str,
        priority: float = 0.5,
        audio_ref: str | None = None,
        text_hint: str | None = None,
        wake_word: str = "evie",
        frames: bytes | None = None,
        audio_b64: str | None = None,
        push_to_talk: bool = False,
        sample_rate: int = 16000,
        min_wake_confidence: float | None = None,
    ) -> WakeOutcome:
        await self._expire_stale()
        if push_to_talk:
            # Menu-bar Talk is already owner-authenticated. Do not run wake
            # spotting, quiet hours, or voiceprint on this path — and do not
            # require a clip (the spoken turn is the following utterance).
            return await self._begin_push_to_talk_session(
                device_id=device_id, wake_word=wake_word
            )
        sample = self._audio_sample(
            frames=frames,
            audio_b64=audio_b64,
            audio_ref=audio_ref,
            sample_rate=sample_rate,
        )
        # Trusted on-device wake: the always-on ears process already ran a
        # real wake engine (openWakeWord head, or the local Whisper spotter
        # when the head is not exported) and carried its transcript in
        # ``text_hint``. Trust that confidence and skip the server-side
        # engine entirely — re-running a full Whisper pass on every clip was
        # the latency bottleneck of every wake (docs/VOICE.md §10).
        trusted_ears_detection = (
            min_wake_confidence is not None
            and min_wake_confidence >= (settings.ears_wake_threshold or 0.5)
        )
        if trusted_ears_detection and min_wake_confidence is not None:
            detection = WakeDetection(
                triggered=True,
                wake_word=wake_word,
                confidence=min_wake_confidence,
                device_id=device_id,
                stage="burst",
                power_state="burst",
                details={
                    "engine": "ears_confidence",
                    "wake_confidence": min_wake_confidence,
                    "transcript": text_hint,
                },
            )
        else:
            # Spot "EVIE" first. Speaker verify on every ambient VAD clip
            # was rejecting the owner before the name was even heard.
            detection = await self.wake_engine.detect(
                audio_ref=audio_ref,
                device_id=device_id,
                frames=frames,
                text_hint=text_hint,
            )
            if (
                not detection.triggered
                and min_wake_confidence is not None
                and min_wake_confidence >= (settings.ears_wake_threshold or 0.5)
            ):
                detection = WakeDetection(
                    triggered=True,
                    wake_word=wake_word,
                    confidence=min_wake_confidence,
                    device_id=device_id,
                    stage="burst",
                    power_state="burst",
                    details={
                        "engine": "ears_confidence",
                        "wake_confidence": min_wake_confidence,
                    },
                )
        await self._log(
            "wake",
            "accepted" if detection.triggered else "rejected",
            device_id=device_id,
            reason=None if detection.triggered else "wake word not detected",
            wake_word=wake_word,
            wake_confidence=detection.confidence,
            stage=detection.stage,
            power_state=detection.power_state,
            transcript=detection.details.get("transcript"),
            error=detection.details.get("error"),
            audio_sha256=None,
        )
        if not detection.triggered:
            heard = detection.details.get("transcript") or detection.details.get("error")
            message = "Wake word not detected"
            if heard:
                message = f"Wake word not detected (heard: {str(heard)[:80]})"
            return WakeOutcome(
                session_id=None,
                state=VoiceState.IDLE,
                owner_enrolled=(await self._current_enrollment()) is not None,
                message=message,
                transcript=detection.details.get("transcript"),
            )
        from app.ev.ev_sense import quiet_hours_active

        if quiet_hours_active(utcnow()) and priority < settings.runtime_urgent_priority_threshold:
            await self._log(
                "refusal",
                "refused",
                device_id=device_id,
                reason="quiet hours",
                wake_word=wake_word,
                priority=priority,
            )
            return WakeOutcome(
                session_id=None,
                state=VoiceState.IDLE,
                owner_enrolled=(await self._current_enrollment()) is not None,
                message="EVIE is resting during quiet hours. Urgent requests only.",
            )
        await self._require_voice_consent()
        self._enforce_remote_voiceprint_gate()

        existing = await self._active_session(device_id)
        if existing is not None:
            if existing.state == VoiceState.VERIFYING and detection.triggered:
                existing.state = VoiceState.ENDED
                existing.ended_at = utcnow()
                existing.end_reason = "superseded by new wake"
            else:
                return WakeOutcome(
                    session_id=str(existing.id),
                    state=existing.state,
                    owner_enrolled=True,
                    challenge_nonce=existing.challenge_nonce,
                    challenge_phrase=existing.challenge_phrase,
                    message="Voice session already active",
                    transcript=detection.details.get("transcript"),
                )

        enrollment = await self._current_enrollment()
        if enrollment is None:
            await self._log(
                "refusal",
                "refused",
                device_id=device_id,
                reason="no owner voiceprint enrolled",
                wake_word=wake_word,
            )
            # The wake word WAS heard; give the owner audible feedback instead
            # of silent failure (silent failure reads as "EVIE not listening").
            reply = "No owner voiceprint enrolled yet. Open the EV app and enroll your voice."
            tts: SynthesisResult | None = None
            try:
                tts = await self.synthesizer.synthesize(
                    reply,
                    style=SpeechStyle(warmth=0.7, brevity=0.6),
                )
            except Exception:  # noqa: BLE001 - spoken feedback is best-effort
                tts = None
            return WakeOutcome(
                session_id=None,
                state=VoiceState.IDLE,
                owner_enrolled=False,
                message="No owner voiceprint enrolled. Enroll before enabling voice activation.",
                transcript=detection.details.get("transcript"),
                reply=reply,
                tts=tts,
            )

        service = EventService(self.session, actor=self.actor)
        wake_event = await service.create(
            EventCreate(
                source="voice",
                event_type="voice.wake",
                content={
                    "wake_word": wake_word,
                    "confidence": detection.confidence,
                    "stage": detection.stage,
                    "power_state": detection.power_state,
                },
                metadata={"device_id": device_id},
                device_id=device_id,
                privacy_level="sensitive",
            ),
            request_id=None,
        )
        now = utcnow()
        if sample is not None:
            try:
                decision = await self._wake_speaker_ok(
                    sample=sample,
                    enrollment=enrollment,
                    heard=str(detection.details.get("transcript") or text_hint or ""),
                    triggered=bool(detection.triggered),
                )
            except (ValueError, RuntimeError) as exc:
                raise VoiceError(
                    f"Could not read wake audio: {exc}",
                    status=422,
                    code="bad_audio",
                ) from exc
            if decision is None or not decision.verified:
                heard = str(detection.details.get("transcript") or text_hint or "")
                score = 0.0 if decision is None else float(decision.confidence)
                need = (
                    0.0
                    if decision is None
                    else float(decision.threshold or settings.voiceprint_wake_threshold)
                )
                message = (
                    f"Wake ignored — not the owner "
                    f"(heard: {heard[:80]!r} score={score:.2f} need={need:.2f})."
                )
                await self._log(
                    "wake",
                    "rejected",
                    device_id=device_id,
                    reason="not the owner",
                    wake_word=wake_word,
                    confidence=score,
                    threshold=need,
                )
                return WakeOutcome(
                    session_id=None,
                    state=VoiceState.IDLE,
                    owner_enrolled=True,
                    message=message,
                    transcript=heard or None,
                )
            row = VoiceSession(
                device_id=device_id,
                wake_word=wake_word,
                state=VoiceState.AWAKE,
                owner_verified=True,
                speaker_confidence=decision.confidence,
                verifier_name=decision.algorithm,
                wake_confidence=detection.confidence,
                wake_event_id=wake_event.id,
                verified_at=now,
                expires_at=now + timedelta(seconds=self.session_timeout_seconds),
                follow_up_until=now + timedelta(seconds=self.session_timeout_seconds),
            )
            self.session.add(row)
            await self.session.flush()
            await self._log(
                "wake",
                "accepted",
                session_id=row.id,
                device_id=device_id,
                reason="wake word + owner voiceprint",
                wake_word=wake_word,
                wake_confidence=detection.confidence,
                confidence=decision.confidence,
            )
            return WakeOutcome(
                session_id=str(row.id),
                state=VoiceState.AWAKE,
                owner_enrolled=True,
                message="Wake accepted. Listening.",
                transcript=detection.details.get("transcript"),
            )

        row = VoiceSession(
            device_id=device_id,
            wake_word=wake_word,
            state=VoiceState.VERIFYING,
            wake_confidence=detection.confidence,
            wake_event_id=wake_event.id,
            expires_at=now + timedelta(seconds=self.verify_timeout_seconds),
        )
        self.session.add(row)
        await self.session.flush()
        challenge = await self.replay_guard.issue(
            purpose="verify",
            session_id=row.id,
            ttl_seconds=self.verify_timeout_seconds,
        )
        row.challenge_nonce = challenge.nonce
        row.challenge_phrase = challenge.phrase
        await self._log(
            "wake",
            "accepted",
            session_id=row.id,
            device_id=device_id,
            reason="wake word detected",
            wake_word=wake_word,
            wake_confidence=detection.confidence,
            power_state=detection.power_state,
        )
        return WakeOutcome(
            session_id=str(row.id),
            state=VoiceState.VERIFYING,
            owner_enrolled=True,
            challenge_nonce=challenge.nonce,
            challenge_phrase=challenge.phrase,
            message="Wake accepted. Speak the challenge phrase to verify ownership.",
        )

    async def recent_client_owned_session(self) -> VoiceSession | None:
        """Any host-wide Talk/PTT session that still owns playback.

        Menu-bar Talk uses ``mac-<hostname>`` while ev.ears uses
        ``EV_EARS_DEVICE_ID`` (usually ``mac-ears``). Looking up only the
        ears device id misses the Talk session, so both speakers fire.
        """

        from app.voice.speech import PTT_CLIENT_OWNER, PTT_ECHO_GRACE_SECONDS

        now = utcnow()
        result = await self.session.execute(
            select(VoiceSession)
            .where(
                VoiceSession.verifier_name == PTT_CLIENT_OWNER,
                VoiceSession.ended_at.is_(None),
                VoiceSession.state.in_(ACTIVE_STATES),
            )
            .order_by(VoiceSession.created_at.desc())
        )
        for row in result.scalars().all():
            if row.state in BUSY_STATES:
                return row
            stamp = _aware(row.last_utterance_at) or _aware(row.verified_at) or _aware(
                row.created_at
            )
            if stamp is None:
                return row
            if 0 <= (now - stamp).total_seconds() < PTT_ECHO_GRACE_SECONDS:
                return row
        return None

    async def runtime_winner_silences_ears(self, ears_device_id: str) -> bool:
        """True when fleet wake already picked another device to speak."""

        from app.models import Device
        from app.services.runtime import active_session
        from app.voice.speech import ears_device_matches_winner

        current = await active_session(self.session)
        if current is None or current.ended_at is not None:
            return False
        if current.state == "idle" or current.device_id is None:
            return False
        winner = await self.session.get(Device, current.device_id)
        if winner is None:
            return False
        return not ears_device_matches_winner(
            ears_device_id=ears_device_id,
            winner_name=winner.name,
            winner_id=str(winner.id),
            winner_type=winner.device_type,
        )

    async def _ears_playback_owner(self, device_id: str) -> str:
        if await self.recent_client_owned_session() is not None:
            return "client"
        if await self.runtime_winner_silences_ears(device_id):
            return "none"
        return "ears"

    async def drain_pending_ingest(
        self, *, device_id: str, session_id
    ) -> EarsIngestOutcome | None:
        """Process a follow-up that arrived while this session was busy."""

        pending = pop_pending_ingest(device_id)
        if not pending:
            return None
        row = await self._get_session(session_id)
        if row.state in BUSY_STATES:
            row.state = VoiceState.AWAKE
        try:
            utterance = await self.handle_utterance(
                session_id=session_id,
                audio_b64=pending.get("audio_b64"),
                audio_ref=pending.get("audio_ref"),
                text=pending.get("text_hint") if not pending.get("audio_b64") else None,
                follow_up=False,
                from_ears=True,
            )
        except VoiceError:
            queue_pending_ingest(device_id, pending)
            return None
        heard = utterance.transcript.text if utterance.transcript else ""
        return EarsIngestOutcome(
            accepted=True,
            message="follow-up",
            session_id=utterance.session_id,
            state=utterance.state,
            listening=utterance.state in ACTIVE_STATES,
            transcript=heard or None,
            reply=utterance.reply,
            tts=utterance.tts,
        )

    async def handle_ears_ingest(
        self,
        *,
        device_id: str,
        frames_b64: str | None,
        sample_rate: int = 16000,
        wake_confidence: float = 0.0,
        consent: bool = False,
        audio_ref: str | None = None,
        text_hint: str | None = None,
        defer_command: bool = False,
    ) -> EarsIngestOutcome:
        """Always-on mic path: wake if idle, otherwise owner-only follow-up.

        Production gate EV_ALWAYS_AVAILABLE_WAKE: OFF (no wake), SHADOW (score
        and log bounded diagnostics only, never initiate live), ON (real handoff).
        """
        # Feature-flag gate (§36) — must be first so OFF/SHADOW never opens a session.
        gate = (settings.always_available_wake or "OFF").strip().upper()
        if gate == "OFF":
            # Also respect explicit owner disable: ears_consent false already above.
            return EarsIngestOutcome(
                accepted=False,
                message="wake_disabled_off",
                listening=False,
            )
        is_shadow = gate == "SHADOW"

        if not consent:
            return EarsIngestOutcome(
                accepted=False,
                message="consent_not_granted",
                listening=False,
            )
        frames = None
        audio_b64 = None
        if frames_b64:
            import base64

            from app.audio.capture import pcm_to_wav_bytes

            frames = base64.b64decode(frames_b64)
            wav = pcm_to_wav_bytes(frames, sample_rate)
            audio_b64 = base64.b64encode(wav).decode("ascii")

        await self._expire_stale()
        playback_owner = await self._ears_playback_owner(device_id)
        if playback_owner != "ears":
            # Talk on mac-<host> or a phone wake winner already owns this turn.
            return EarsIngestOutcome(
                accepted=True,
                message="still listening",
                listening=True,
                playback_owner=playback_owner,
            )
        existing = await self._active_session(device_id)
        if (
            existing is not None
            and existing.owner_verified
            and existing.state
            in {
                VoiceState.AWAKE,
                VoiceState.FOLLOW_UP,
                VoiceState.PROCESSING,
                VoiceState.RESPONDING,
            }
        ):
            # One wake opens a Siri-style session. The short follow-up
            # window is only a REST hint; ears stays listening until a
            # sleep phrase or the long idle lock. Hint expiry must not
            # extend that lock — only an accepted owner turn does.
            if (
                existing.state in BUSY_STATES
                and self._busy_session_age(existing) < STALE_BUSY_SECONDS
                and session_in_flight(str(existing.id))
            ):
                queue_pending_ingest(
                    existing.device_id,
                    {
                        "audio_b64": audio_b64,
                        "audio_ref": audio_ref,
                        "text_hint": text_hint,
                        "session_id": str(existing.id),
                    },
                )
                return EarsIngestOutcome(
                    accepted=True,
                    message="queued",
                    session_id=str(existing.id),
                    state=existing.state,
                    listening=True,
                    queued=True,
                )
            if existing.state in BUSY_STATES:
                existing.state = VoiceState.AWAKE
            if existing.state == VoiceState.FOLLOW_UP and follow_up_hint_expired(
                existing.follow_up_until
            ):
                existing.state = VoiceState.AWAKE
            from app.voice.speech import (
                ears_should_handle_follow_up,
                session_playback_owner,
            )

            owner = session_playback_owner(existing.verifier_name)
            if not ears_should_handle_follow_up(
                verifier_name=existing.verifier_name,
                last_utterance_at=_aware(existing.last_utterance_at),
                now=utcnow(),
                busy=existing.state in BUSY_STATES
                and self._busy_session_age(existing) < STALE_BUSY_SECONDS,
            ):
                return EarsIngestOutcome(
                    accepted=True,
                    message="still listening",
                    session_id=str(existing.id),
                    state=existing.state,
                    listening=True,
                    playback_owner=owner,
                )
            try:
                utterance = await self.handle_utterance(
                    session_id=existing.id,
                    audio_b64=audio_b64,
                    audio_ref=audio_ref,
                    text=text_hint if not audio_b64 and not audio_ref else None,
                    follow_up=False,
                    from_ears=True,
                )
            except VoiceError as exc:
                if exc.code in {
                    "voice_ignored",
                    "asr_empty_result",
                    "asr_no_final",
                    "invalid_state",
                }:
                    return EarsIngestOutcome(
                        accepted=True,
                        message="still listening",
                        session_id=str(existing.id),
                        state=existing.state,
                        listening=True,
                    )
                if exc.code not in {"follow_up_expired", "session_expired", "session_ended"}:
                    raise
            else:
                listening = utterance.state in ACTIVE_STATES
                heard = utterance.transcript.text if utterance.transcript else ""
                if listening and self._is_wake_only(heard) and not self._is_sleep_phrase(heard):
                    return EarsIngestOutcome(
                        accepted=True,
                        message="still listening",
                        session_id=utterance.session_id,
                        state=utterance.state,
                        listening=True,
                        transcript=heard or "EVIE",
                        reply=utterance.reply,
                        tts=utterance.tts,
                    )
                return EarsIngestOutcome(
                    accepted=True,
                    message="follow-up" if listening else utterance.reply,
                    session_id=utterance.session_id,
                    state=utterance.state,
                    listening=listening,
                    transcript=heard or None,
                    reply=utterance.reply,
                    tts=utterance.tts,
                )

        wake = await self.handle_wake(
            device_id=device_id,
            audio_ref=audio_ref,
            text_hint=text_hint,
            frames=frames,
            audio_b64=audio_b64,
            sample_rate=sample_rate,
            min_wake_confidence=wake_confidence,
        )
        if wake.session_id is None:
            return EarsIngestOutcome(
                accepted=False,
                message=wake.message,
                state=wake.state,
                listening=False,
                transcript=wake.transcript,
                reply=wake.reply,
                tts=wake.tts,
            )
        # SHADOW: never hand off, only score/log bounded diagnostics.
        if is_shadow:
            try:
                from app.wake.directed import DirectedSpeechChecker

                _chk = DirectedSpeechChecker()
                _dir = _chk.is_directed((wake.transcript or text_hint or "evie"), asr_confidence=wake_confidence)
                await self._log(
                    "wake",
                    "shadow_scored",
                    device_id=device_id,
                    reason="shadow_cascade",
                    directed=_dir.directed,
                    diagnostics=_dir.diagnostics,
                    wake_confidence=wake_confidence,
                    shadow=True,
                )
            except Exception:
                pass
            # SHADOW: end speculative session immediately, no handoff, no commit.
            try:
                row = await self._get_session(wake.session_id)
                row.state = VoiceState.ENDED
                row.ended_at = utcnow()
                row.end_reason = "shadow_no_handoff"
            except Exception:
                pass
            return EarsIngestOutcome(
                accepted=False,
                message="shadow_scored",
                state=VoiceState.ENDED,
                listening=False,
                transcript=wake.transcript,
            )
        # WAKE W3: directed-speech / false-trigger check — before meaningful action.
        # Acoustic + transcript + semantic evidence; cancel silently if not directed
        # or not owner, bounded diagnostics only, do not announce.
        try:
            from app.wake.directed import DirectedSpeechChecker

            checker = DirectedSpeechChecker()
            heard_text = (wake.transcript or text_hint or "").strip()
            # For an accepted wake the detector already trusted the confidence;
            # still verify directedness from the full utterance text.
            directed = checker.is_directed(
                heard_text or "evie",
                asr_confidence=wake_confidence,
            )
            # Also run fast speaker confidence if we have audio (wake stage fast)
            # is already done in handle_wake; full-utterance recheck happens via
            # addressivity_gate below with accumulated command PCM.
            if not directed.directed and heard_text:
                # "Evie is..." / "Did you see Evie yesterday?" → not directed.
                row = await self._get_session(wake.session_id)
                row.state = VoiceState.ENDED
                row.ended_at = utcnow()
                row.end_reason = f"not_directed:{directed.reason}"
                await self._log(
                    "wake",
                    "rejected",
                    session_id=row.id,
                    device_id=device_id,
                    reason=directed.reason,
                    diagnostics=directed.diagnostics,
                    transcript=heard_text[:80],
                )
                return EarsIngestOutcome(
                    accepted=False,
                    message="not_directed",
                    state=VoiceState.ENDED,
                    listening=False,
                    transcript=heard_text[:80],
                )
        except Exception as exc:  # noqa: BLE001 - directed must never crash wake
            LOGGER.debug("directed check skipped: %s", exc)
        # WAKE W4: device arbitration groundwork — ONE device wins.
        # Deterministic factors: wake confidence, conversation continuity,
        # availability, nearby context. Reuse ConversationLease.
        try:
            from app.device_gateway.lease import current_lease
            from app.wake.arbitration import WakeArbitration, WakeCandidate

            lease = await current_lease(self.session)
            # Current device candidate for this wake
            candidate = WakeCandidate(
                device_id=device_id,
                confidence=float(wake_confidence or 0.0),
                has_active_session=False,
                last_activity=utcnow(),
            )
            arb = WakeArbitration()
            # Build a two-element candidate set when a lease holder exists
            # (holder's confidence approximated from lease recency).
            other_candidates: list[WakeCandidate] = [candidate]
            lease_dict = None
            if lease is not None:
                lease_dict = {
                    "device_id": str(lease.device_id),
                    "instance_id": lease.instance_id,
                }
                holder_conf = 0.5  # unknown remote confidence → neutral
                # If lease holder is not this device, add it as competing candidate
                if str(lease.device_id) != device_id:
                    other_candidates.append(
                        WakeCandidate(
                            device_id=str(lease.device_id),
                            confidence=holder_conf,
                            has_active_session=True,
                            last_activity=lease.last_activity,
                        )
                    )
            winner = arb.pick_winner(other_candidates, current_lease=lease_dict)
            if winner and winner.winner_device_id != device_id:
                row = await self._get_session(wake.session_id)
                row.state = VoiceState.ENDED
                row.ended_at = utcnow()
                row.end_reason = f"arbitration_lost_to_{winner.winner_device_id[:8]}"
                await self._log(
                    "wake",
                    "rejected",
                    session_id=row.id,
                    device_id=device_id,
                    reason=winner.reason,
                    winner=winner.winner_device_id,
                    confidence=wake_confidence,
                )
                return EarsIngestOutcome(
                    accepted=False,
                    message="arbitration_lost",
                    state=VoiceState.ENDED,
                    listening=False,
                    transcript=wake.transcript,
                )
        except Exception as exc:  # noqa: BLE001 - arbitration must not crash wake
            LOGGER.debug("arbitration skipped: %s", exc)
        listening = wake.state in ACTIVE_STATES and wake.state != VoiceState.VERIFYING
        transcript = None
        reply = None
        tts = None
        state = wake.state
        if (
            wake.state in {VoiceState.AWAKE, VoiceState.FOLLOW_UP}
            and audio_b64
        ):
            heard = wake.transcript or ""
            command = self._command_after_wake(heard)
            # Wake is the door. If this clip is only the name — or the
            # spotter fired without a transcript — say "Yes?" and listen.
            # One-shot "EVIE what's next" still has a leftover command.
            from app.voice.speech import choose_listen_ack

            if not command:
                tts = await self._listening_ack(heard)
                self._remember_spoken_reply(
                    device_id, choose_listen_ack(heard or "evie"), tts=tts
                )
                drained = await self.drain_pending_ingest(
                    device_id=device_id, session_id=wake.session_id
                )
                if drained is not None:
                    return drained
                return EarsIngestOutcome(
                    accepted=True,
                    message=wake.message,
                    session_id=wake.session_id,
                    state=wake.state,
                    listening=True,
                    transcript=heard or "EVIE",
                    reply=choose_listen_ack(heard or "evie"),
                    tts=tts,
                )
            if defer_command:
                # Wake is a handshake. Let the client start the streaming
                # utterance request after the acknowledgment instead of making
                # this endpoint wait for ASR, model generation, and TTS.
                row = await self._get_session(wake.session_id)
                self._refresh_listen_window(row)
                ack_phrase = choose_listen_ack(heard or "evie")
                tts = await self._listening_ack(heard)
                self._remember_spoken_reply(device_id, ack_phrase, tts=tts)
                return EarsIngestOutcome(
                    accepted=True,
                    message="command deferred",
                    session_id=wake.session_id,
                    state=VoiceState.FOLLOW_UP,
                    listening=True,
                    transcript=heard or None,
                    reply=ack_phrase,
                    tts=tts,
                    command_deferred=True,
                )
            try:
                row = await self._get_session(wake.session_id)
                await self._addressivity_gate(
                    row=row,
                    audio_b64=audio_b64,
                    audio_ref=audio_ref,
                    text=None,
                    push_to_talk=False,
                )
                ack_phrase = choose_listen_ack(heard or "evie")
                ack_tts = await self._listening_ack(heard)
                # Keep the original Evie-start text so the utterance
                # pipeline also treats this as a listen-ack turn (does
                # not first-speak Checking/Searching).
                utterance = await self.handle_utterance(
                    session_id=wake.session_id,
                    text=heard,
                    push_to_talk=False,
                    from_ears=True,
                )
                state = utterance.state
                listening = utterance.state in ACTIVE_STATES
                transcript = utterance.transcript.text if utterance.transcript else None
                reply = utterance.reply or ""
                if ack_phrase and not reply.startswith(ack_phrase):
                    reply = f"{ack_phrase} {reply}".strip()
                tts = await self._with_leading_listen_ack(ack_tts, utterance.tts)
            except VoiceError as exc:
                if exc.code != "voice_ignored":
                    return EarsIngestOutcome(
                        accepted=True,
                        message=exc.message,
                        session_id=wake.session_id,
                        state=wake.state,
                        listening=True,
                    )
        return EarsIngestOutcome(
            accepted=True,
            message=wake.message,
            session_id=wake.session_id,
            state=state,
            listening=listening,
            transcript=transcript,
            reply=reply,
            tts=tts,
        )

    # ------------------------------------------------------------------ #
    # Verification
    # ------------------------------------------------------------------ #

    async def handle_verify(
        self,
        *,
        session_id,
        nonce: str,
        samples: list[dict] | None = None,
        phrase: str | None = None,
        features: list[float] | None = None,
        audio_ref: str | None = None,
        liveness_proof: str | None = None,
        live_score: float | None = None,
        audio_sha256: str | None = None,
    ) -> VerifyOutcome:
        await self._expire_stale()
        row = await self._get_session(session_id)
        if row.state != VoiceState.VERIFYING:
            raise VoiceError(
                f"Cannot verify in state {row.state}",
                status=409,
                code="invalid_state",
            )
        await self._require_voice_consent()
        self._enforce_remote_voiceprint_gate()
        if row.owner_verified:
            raise VoiceError("Session already verified", status=409, code="already_verified")
        if _aware(row.expires_at) is not None and _aware(row.expires_at) < utcnow():
            row.state = VoiceState.ENDED
            row.ended_at = utcnow()
            row.end_reason = "verification timeout"
            raise VoiceError(
                "Verification window expired — wake EVIE again",
                status=428,
                code="verify_timeout",
            )

        try:
            await self.replay_guard.consume(nonce, purpose="verify", session_id=row.id)
        except ReplayError as exc:
            await self._log(
                "replay",
                "rejected",
                session_id=row.id,
                device_id=row.device_id,
                reason=str(exc),
                purpose="verify",
            )
            row.state = VoiceState.ENDED
            row.ended_at = utcnow()
            row.end_reason = "replay detected"
            raise VoiceError(
                f"Replay attack rejected: {exc}",
                status=403,
                code="replay_rejected",
            ) from exc

        if await self.replay_guard.fingerprint_replayed(
            audio_sha256, device_id=row.device_id
        ):
            await self._log(
                "replay",
                "rejected",
                session_id=row.id,
                device_id=row.device_id,
                reason="audio fingerprint already accepted",
                purpose="verify",
            )
            row.state = VoiceState.ENDED
            row.ended_at = utcnow()
            row.end_reason = "replay detected"
            raise VoiceError(
                "Replay attack rejected: audio already used",
                status=403,
                code="replay_rejected",
            )

        enrollment = await self._current_enrollment()
        if enrollment is None:
            raise VoiceError("No owner voiceprint enrolled", status=428, code="not_enrolled")
        enrolled_payload = await self._decrypt_enrollment(enrollment)
        sample: dict = {"text": phrase} if phrase else {}
        if features is not None:
            sample["features"] = features
        if audio_ref:
            sample["audio_ref"] = audio_ref
        if liveness_proof:
            sample["liveness_proof"] = liveness_proof
        if live_score is not None:
            sample["live_score"] = live_score
        if audio_sha256:
            sample["audio_sha256"] = audio_sha256

        live_ok, live_conf, live_reason = await self.liveness.check(
            sample=sample,
            challenge_phrase=phrase,
            expected_phrase=row.challenge_phrase,
        )
        if not live_ok:
            await self._log(
                "verify",
                "rejected",
                session_id=row.id,
                device_id=row.device_id,
                reason=live_reason,
                liveness_confidence=live_conf,
                audio_sha256=audio_sha256,
            )
            row.state = VoiceState.ENDED
            row.ended_at = utcnow()
            row.end_reason = "liveness failed"
            return VerifyOutcome(
                session_id=str(row.id),
                state=VoiceState.ENDED,
                verified=False,
                confidence=live_conf,
                reason=live_reason,
            )

        verify_inputs = samples if samples else ([sample] if sample else [])
        if not verify_inputs:
            raise VoiceError(
                "Verification requires an audio sample, phrase, or features",
                status=422,
                code="missing_sample",
            )
        decisions = [
            await self.verifier.verify(
                verify_sample,
                enrolled_payload=enrolled_payload,
                threshold=enrollment.threshold,
            )
            for verify_sample in verify_inputs
        ]
        confidence = round(
            sum(decision.confidence for decision in decisions) / len(decisions),
            4,
        )
        verified = confidence >= enrollment.threshold
        if verified:
            from app.ev import assistant as assistant_mod

            thread = await assistant_mod.bind_live_thread(self.session)
            row.state = VoiceState.AWAKE
            row.owner_verified = True
            row.speaker_confidence = confidence
            row.verifier_name = decisions[0].algorithm
            row.verified_at = utcnow()
            row.expires_at = utcnow() + timedelta(seconds=self.session_timeout_seconds)
            row.conversation_id = thread.id
            awake = await assistant_mod.companion_on_awake(
                self.session, row, actor=self.actor
            )
            await self._log(
                "verify",
                "accepted",
                session_id=row.id,
                device_id=row.device_id,
                reason="owner verified",
                confidence=confidence,
                verifier=decisions[0].algorithm,
                liveness_confidence=live_conf,
                audio_sha256=audio_sha256,
            )
            return VerifyOutcome(
                session_id=str(row.id),
                state=VoiceState.AWAKE,
                verified=True,
                confidence=confidence,
                reason="owner voiceprint match",
                conversation_id=str(thread.id),
                greeting=awake.greeting,
                onboarding=awake.onboarding,
            )

        await self._log(
            "refusal",
            "refused",
            session_id=row.id,
            device_id=row.device_id,
            reason="unknown voice",
            confidence=confidence,
            threshold=enrollment.threshold,
            audio_sha256=audio_sha256,
        )
        row.state = VoiceState.ENDED
        row.ended_at = utcnow()
        row.end_reason = "unknown voice"
        return VerifyOutcome(
            session_id=str(row.id),
            state=VoiceState.ENDED,
            verified=False,
            confidence=confidence,
            reason="Unknown voice — polite refusal. Only the owner can activate EVIE.",
        )

    # ------------------------------------------------------------------ #
    # Utterance / follow-up
    # ------------------------------------------------------------------ #

    def _interrupt_event(self, session_id) -> asyncio.Event:
        return self._interrupts.setdefault(str(session_id), asyncio.Event())

    def _normalize_conversation_id(self, conversation_id):
        """Map the CONTINUITY_LIVE name to the one default lifelong thread."""

        if conversation_id is None:
            return None
        if str(conversation_id) == self.continuity_conversation_id:
            return None
        return conversation_id

    # Far-field MacBook "EVIE" often lands well under the close-talk mean.
    _WAKE_NEAR_MISS = 0.20

    async def _wake_speaker_ok(
        self,
        *,
        sample: dict,
        enrollment,
        heard: str,
        triggered: bool = False,
    ):
        """Owner check used only after the wake word was spotted."""

        from app.voice.contracts import SpeakerDecision
        from app.voice.wake import WhisperPhraseWakeEngine

        enrolled_payload = await self._decrypt_enrollment(enrollment)
        wake_threshold = min(
            enrollment.threshold,
            settings.voiceprint_wake_threshold,
        )
        decision = await self.verifier.verify(
            sample,
            enrolled_payload=enrolled_payload,
            threshold=wake_threshold,
        )
        if decision.verified:
            return decision
        heard_evie = triggered or bool(
            WhisperPhraseWakeEngine.WAKE_TOKEN.search(heard or "")
        )
        if heard_evie and decision.confidence >= self._WAKE_NEAR_MISS:
            return SpeakerDecision(
                verified=True,
                confidence=decision.confidence,
                threshold=self._WAKE_NEAR_MISS,
                algorithm=decision.algorithm,
                speaker_id=decision.speaker_id,
                reason="wake near-miss",
            )
        return decision

    def _audio_sample(
        self,
        *,
        frames: bytes | None = None,
        audio_b64: str | None = None,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
    ) -> dict | None:
        """Build a speaker-verify sample from wake audio, if any was supplied."""

        if audio_b64:
            return {"audio_b64": audio_b64}
        if audio_ref:
            return {"audio_ref": audio_ref}
        if frames:
            import base64

            from app.audio.capture import pcm_to_wav_bytes

            wav = pcm_to_wav_bytes(frames, sample_rate)
            return {"audio_b64": base64.b64encode(wav).decode("ascii")}
        return None

    async def _pcm_from_audio(
        self,
        *,
        audio_b64: str | None,
        audio_ref: str | None,
    ) -> list[int]:
        """Decode utterance audio to 16 kHz mono PCM samples for VAD."""

        from app.voice.asr import _read_audio
        from app.voice.speaker import decode_waveform

        raw, _filename = await _read_audio(audio_b64, audio_ref)
        try:
            values, _sample_rate = decode_waveform(raw)
        except (ValueError, wave.Error, EOFError) as exc:
            from app.voice.asr import hear_status_message

            raise VoiceError(
                hear_status_message("asr_undecodable_audio"),
                status=422,
                code="asr_undecodable_audio",
            ) from exc
        return [
            max(-32768, min(32767, int(round(value * 32767))))
            for value in values
        ]

    async def _addressivity_gate(
        self,
        *,
        row: VoiceSession,
        audio_b64: str | None,
        audio_ref: str | None,
        text: str | None,
        push_to_talk: bool,
    ) -> None:
        """Owner-only utterance gate: VAD + speaker verification or PTT."""

        if not self.addressivity_enabled:
            return
        if push_to_talk or row.verifier_name == "push_to_talk":
            return
        if text is not None and audio_b64 is None and audio_ref is None:
            # Trusted, owner-authenticated dev/test text surface (no ambient
            # audio); real ASR providers still require audio end-to-end.
            return
        if audio_b64 is None and audio_ref is None:
            raise VoiceError(
                "Utterance requires audio or explicit push-to-talk",
                status=422,
                code="voice_audio_required",
            )
        vad = self.vad_engine or default_vad_engine()
        try:
            pcm = await self._pcm_from_audio(
                audio_b64=audio_b64,
                audio_ref=audio_ref,
            )
        except VoiceError as exc:
            await self._log(
                "addressivity",
                "ignored",
                session_id=row.id,
                device_id=row.device_id,
                reason="audio_undecodable",
            )
            raise VoiceError(
                "Speech ignored — audio could not be decoded",
                status=403,
                code="voice_ignored",
            ) from exc
        probabilities = await vad.frame_probabilities(pcm, 16000)
        speech_hit = any(p >= self.vad_threshold for p in probabilities)
        if not speech_hit:
            mean_probability = (
                sum(probabilities) / len(probabilities) if probabilities else 0.0
            )
            await self._log(
                "addressivity",
                "ignored",
                session_id=row.id,
                device_id=row.device_id,
                reason="no_speech",
                speech_probability=round(mean_probability, 4),
                vad_threshold=self.vad_threshold,
            )
            raise VoiceError(
                "Speech ignored — no clear speech",
                status=403,
                code="voice_ignored",
            )
        enrollment = await self._current_enrollment()
        if enrollment is None:
            raise VoiceError(
                "No owner voiceprint enrolled",
                status=428,
                code="not_enrolled",
            )
        enrolled_payload = await self._decrypt_enrollment(enrollment)
        sample = {"audio_b64": audio_b64} if audio_b64 else {"audio_ref": audio_ref}
        decision = await self.verifier.verify(
            sample,
            enrolled_payload=enrolled_payload,
            threshold=min(enrollment.threshold, settings.voiceprint_wake_threshold),
        )
        if not decision.verified:
            await self._log(
                "addressivity",
                "ignored",
                session_id=row.id,
                device_id=row.device_id,
                reason="not_owner",
                confidence=round(decision.confidence, 4),
                threshold=decision.threshold,
                verifier=decision.algorithm,
            )
            raise VoiceError(
                "Speech ignored — not the owner",
                status=403,
                code="voice_ignored",
            )
        from app.voice.anti_spoof import follow_up_liveness
        from app.voice.speaker import sample_audio_bytes

        try:
            raw = sample_audio_bytes(sample)
        except ValueError as exc:
            raise VoiceError(
                "Speech ignored — audio could not be decoded",
                status=403,
                code="voice_ignored",
            ) from exc
        live_ok, live_conf, live_reason = follow_up_liveness(
            raw,
            owner_verified=bool(row.owner_verified),
            speaker_matched=True,
        )
        if not live_ok:
            await self._log(
                "addressivity",
                "ignored",
                session_id=row.id,
                device_id=row.device_id,
                reason=live_reason,
                liveness_confidence=live_conf,
            )
            raise VoiceError(
                "Speech ignored — liveness failed",
                status=403,
                code="voice_ignored",
            )
        await self._log(
            "addressivity",
            "accepted",
            session_id=row.id,
            device_id=row.device_id,
            reason="owner voiceprint match",
            confidence=round(decision.confidence, 4),
            verifier=decision.algorithm,
            liveness_confidence=live_conf,
        )

    def _command_after_wake(self, text: str) -> str:
        """Strip 'hey/hello EVIE' so the rest of the sentence is the command."""

        stripped = re.sub(
            r"^(?:hey|ok|okay|hi|hello)?\s*(?:evie+|eevee|evy|evi|eve|evil|every|ee\s*vee)(?:\s+here)?\b[\s,!.?\-]*",
            "",
            (text or "").strip(),
            count=1,
            flags=re.IGNORECASE,
        )
        return stripped.strip(" ,.!?")

    def _is_wake_only(self, text: str) -> bool:
        from app.voice.speech import is_wake_only_name

        return is_wake_only_name(text)

    async def _listening_ack(self, heard: str = "") -> SynthesisResult:
        _phrase, result = await cached_listen_ack(self.synthesizer, heard)
        return result

    def _remember_spoken_reply(
        self,
        device_id: str | None,
        text: str,
        *,
        tts: SynthesisResult | None = None,
        now=None,
    ) -> None:
        from app.voice.speech import estimate_spoken_duration_s, remember_spoken

        duration_s = estimate_spoken_duration_s(
            text,
            duration_ms=tts.duration_ms if tts is not None else None,
            audio=tts.audio if tts is not None else None,
            content_type=tts.content_type if tts is not None else None,
        )
        remember_spoken(str(device_id or ""), text, duration_s=duration_s, now=now)

    def _is_self_echo(self, row: VoiceSession, heard: str) -> bool:
        from app.voice.speech import (
            is_playback_window,
            last_spoken,
            should_drop_as_echo,
        )

        spoken = last_spoken(row.device_id) or {}
        return should_drop_as_echo(
            heard,
            last_reply=spoken.get("text"),
            spoken_at=spoken.get("at"),
            now=utcnow(),
            playing=is_playback_window(row.device_id),
            duration_s=spoken.get("duration_s"),
        )

    async def _wake_only_listen_outcome(
        self, *, row: VoiceSession, transcript: Transcript
    ) -> UtteranceOutcome:
        """Eve/EVIE with no leftover command: ack and keep listening. No chat."""

        from app.voice.speech import choose_listen_ack

        self._refresh_listen_window(row)
        phrase = choose_listen_ack(transcript.text or "evie")
        tts = await self._listening_ack(transcript.text)
        self._remember_spoken_reply(row.device_id, phrase, tts=tts)
        await self._log(
            "utterance",
            "accepted",
            session_id=row.id,
            device_id=row.device_id,
            reason="wake_only",
        )
        return UtteranceOutcome(
            session_id=str(row.id),
            state=VoiceState.FOLLOW_UP,
            transcript=transcript,
            reply=phrase,
            conversation_id=str(row.conversation_id) if row.conversation_id else None,
            tts=tts,
        )

    async def _with_leading_listen_ack(
        self,
        ack: SynthesisResult | None,
        answer: SynthesisResult | None,
    ) -> SynthesisResult | None:
        """Ears plays one clip — it must start with the listen-ack audio."""

        from app.voice.speech import concat_wav_bytes

        if ack is None or not ack.audio:
            return answer
        if answer is None or not answer.audio:
            return ack
        combined = concat_wav_bytes([ack.audio, answer.audio])
        if combined and combined.startswith(b"RIFF"):
            answer.audio = combined
            return answer
        phrase = f"{(ack.text or '').strip()} {(answer.text or '').strip()}".strip()
        if not phrase:
            return answer
        try:
            return await self.synthesizer.synthesize(
                phrase, style=answer.style or ack.style
            )
        except Exception:  # noqa: BLE001 - keep the command audio
            return answer

    async def _ack_if_wake(
        self,
        *,
        frames: bytes | None,
        sample_rate: int,
        device_id: str,
    ) -> tuple[str | None, SynthesisResult] | None:
        detection = await self.wake_engine.detect(
            frames=frames,
            sample_rate=sample_rate,
            device_id=device_id,
        )
        if not detection.triggered:
            return None
        heard = detection.details.get("transcript") or "EVIE"
        tts = await self._listening_ack(heard)
        return heard, tts

    def _is_sleep_phrase(self, text: str) -> bool:
        return is_sleep_phrase(text, self.sleep_phrases)

    async def _sleep_outcome(
        self,
        *,
        row: VoiceSession,
        transcript: Transcript,
    ) -> UtteranceOutcome:
        """End the session on a clear dismissal phrase, without a chat reply."""

        style = SpeechStyle()
        synthesis = await self.synthesizer.synthesize("Goodnight.", style=style)
        row.state = VoiceState.ENDED
        row.ended_at = utcnow()
        row.end_reason = "sleep phrase"
        row.follow_up_until = None
        await self._log(
            "end",
            "accepted",
            session_id=row.id,
            device_id=row.device_id,
            reason="sleep phrase",
            asr_provider=transcript.provider,
        )
        return UtteranceOutcome(
            session_id=str(row.id),
            state=VoiceState.ENDED,
            transcript=transcript,
            reply="Goodnight.",
            tts=synthesis,
            style=style,
        )

    def _is_talk_request(self, row: VoiceSession, push_to_talk: bool) -> bool:
        """Talk / in-app live: explicit PTT flag or an owner-authenticated door."""

        return bool(
            push_to_talk
            or row.verifier_name in self.APP_LIVE_VERIFIERS
        )

    async def _validate_utterance_row(
        self, row: VoiceSession, follow_up: bool, *, push_to_talk: bool = False
    ) -> None:
        if self._is_talk_request(row, push_to_talk):
            # Talk is owner-authenticated. Never trap the button behind a
            # stale ENDED / VERIFYING / idle-locked row the client still holds.
            if row.state in BUSY_STATES:
                if (
                    self._busy_session_age(row) < STALE_BUSY_SECONDS
                    and session_in_flight(str(row.id))
                ):
                    raise VoiceError(
                        f"Utterance only valid from a listening state (current: {row.state})",
                        status=409,
                        code="invalid_state",
                    )
                self._reuse_push_to_talk_session(row)
            elif (
                row.ended_at is not None
                or row.state not in {VoiceState.AWAKE, VoiceState.FOLLOW_UP}
                or not row.owner_verified
            ):
                self._reuse_push_to_talk_session(row)
            if row.state == VoiceState.FOLLOW_UP and follow_up_hint_expired(
                row.follow_up_until
            ):
                row.state = VoiceState.AWAKE
            return
        if row.ended_at is not None or row.state == VoiceState.ENDED:
            raise VoiceError(
                "Voice session ended — wake EVIE again",
                status=428,
                code="session_ended",
            )
        if not row.owner_verified:
            raise VoiceError(
                "Session not verified — owner verification required",
                status=403,
                code="not_verified",
            )
        if row.state in BUSY_STATES:
            if self._busy_session_age(row) < STALE_BUSY_SECONDS:
                raise VoiceError(
                    f"Utterance only valid from a listening state (current: {row.state})",
                    status=409,
                    code="invalid_state",
                )
            row.state = VoiceState.AWAKE
        if row.state not in {VoiceState.AWAKE, VoiceState.FOLLOW_UP}:
            raise VoiceError(
                f"Utterance only valid from a listening state (current: {row.state})",
                status=409,
                code="invalid_state",
            )
        # Short follow-up hint is a REST remaining-time display, not a door.
        # Promote expired follow-up back to listening; do not end the session.
        if row.state == VoiceState.FOLLOW_UP and follow_up_hint_expired(
            row.follow_up_until
        ):
            row.state = VoiceState.AWAKE
        _ = follow_up  # accepted from either listening state

    async def _classify_and_reverify(
        self,
        *,
        transcript: Transcript,
        reverify_token: str | None,
        ctx,
    ) -> tuple[str | None, bool]:
        sensitive_purpose = classify_sensitive(transcript.text)
        if sensitive_purpose is None:
            return None, False
        if not reverify_token or ctx is None:
            raise VoiceError(
                "Re-verification required for sensitive voice command "
                f"({sensitive_purpose}). Issue a proof via "
                "POST /v1/identity/reverification with purpose "
                f"{REVERIFY_PURPOSE!r}, then retry with the token.",
                status=403,
                code="reverification_required",
            )
        from app.identity.service import IdentityError, consume_reverification

        try:
            await consume_reverification(
                self.session,
                token=reverify_token,
                purpose=REVERIFY_PURPOSE,
                ctx=ctx,
            )
        except IdentityError as exc:
            raise VoiceError(
                exc.message,
                status=exc.status,
                code=exc.code,
            ) from exc
        return sensitive_purpose, True

    def _degraded_transcript_error(self, transcript: Transcript) -> VoiceError:
        return VoiceError(
            "ASR is degraded: the real provider could not run "
            f"({transcript.provider}); configure weights or a working engine",
            status=503,
            code="asr_degraded",
        )

    _ASR_RECOVERABLE = frozenset(
        {
            "asr_empty_result",
            "asr_timeout",
            "asr_no_final",
            "asr_no_speech",
            "asr_undecodable_audio",
            "asr_empty_audio",
            "asr_bad_base64",
            "asr_audio_required",
            "asr_echo_no_audio",
            "asr_engine_error",
            "asr_unreadable",
        }
    )

    def _is_recoverable_asr(self, exc: VoiceError) -> bool:
        return exc.code in self._ASR_RECOVERABLE

    def _asr_recovery_reply(self, code: str) -> str:
        from app.voice.asr import hear_status_message

        return hear_status_message(code)

    def _busy_session_age(self, row: VoiceSession) -> float:
        stamp = _aware(row.updated_at)
        if stamp is None:
            return 0.0
        return max(0.0, (utcnow() - stamp).total_seconds())

    async def _drain_asr_stream(
        self,
        *,
        audio_ref: str | None,
        audio_b64: str | None,
        text_hint: str | None,
        language: str,
    ) -> list[tuple[str, object]]:
        """Collect ASR stream events under the ASR timeout (not a silent wait)."""

        events: list[tuple[str, object]] = []
        deadline = time.monotonic() + float(settings.voice_asr_timeout_seconds)
        stream = self.transcriber.stream(
            audio_ref=audio_ref,
            audio_b64=audio_b64,
            text_hint=text_hint,
            language=language,
        )
        if asyncio.iscoroutine(stream):
            remaining = deadline - time.monotonic()
            stream = await asyncio.wait_for(stream, timeout=max(0.05, remaining))
        iterator = stream.__aiter__()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VoiceError(
                    "Speech recognition took too long",
                    status=504,
                    code="asr_timeout",
                )
            try:
                item = await asyncio.wait_for(
                    iterator.__anext__(), timeout=remaining
                )
            except StopAsyncIteration:
                break
            except TimeoutError as exc:
                raise VoiceError(
                    "Speech recognition took too long",
                    status=504,
                    code="asr_timeout",
                ) from exc
            if isinstance(item, TranscriptPartial):
                events.append(("partial", item))
            else:
                events.append(("final_transcript", item))
        return events

    async def _unreadable_outcome(
        self, *, row: VoiceSession, transcript: Transcript
    ) -> UtteranceOutcome:
        from app.voice.asr import classify_hear_failure

        raw = (transcript.text or "").strip()
        if not raw:
            details = transcript.details or {}
            if details.get("code") in {"asr_empty_audio", "asr_undecodable_audio", "asr_no_speech"}:
                _code, reply = classify_hear_failure(code=str(details["code"]))
            else:
                _code, reply = classify_hear_failure(no_speech=True)
        else:
            reply = self._asr_recovery_reply("asr_unreadable")
        return await self._fallback_utterance(
            row=row,
            transcript=transcript,
            reply=reply,
            error=reply,
        )

    async def _run_pipeline_for(
        self,
        *,
        row: VoiceSession,
        transcript: Transcript,
        conversation_id,
        follow_up: bool,
        sensitive_purpose: str | None,
        reverified: bool,
    ):
        prior = row.state
        row.state = VoiceState.PROCESSING
        mark_session_in_flight(str(row.id))
        conversation_id = self._normalize_conversation_id(
            conversation_id or row.conversation_id
        )
        started = time.monotonic()
        try:
            outcome = await asyncio.wait_for(
                run_chat_tts_pipeline(
                    self.session,
                    actor=self.actor,
                    device_id=row.device_id,
                    transcript=transcript,
                    conversation_id=conversation_id,
                    synthesizer=self.synthesizer,
                    speaker_confidence=row.speaker_confidence,
                ),
                timeout=settings.voice_turn_timeout_seconds,
            )
        except TimeoutError:
            if row.state == VoiceState.PROCESSING:
                row.state = (
                    prior
                    if prior in {VoiceState.AWAKE, VoiceState.FOLLOW_UP}
                    else VoiceState.AWAKE
                )
            LOGGER.warning(
                "voice chat/tts timed out after %.1fs session=%s",
                time.monotonic() - started,
                row.id,
            )
            return await self._fallback_utterance(
                row=row,
                transcript=transcript,
                reply=(
                    "I heard you, but the answer is taking too long. "
                    "Try asking again in a moment."
                ),
                error="That took too long to hear. Try a shorter question.",
            )
        except BaseException:
            if row.state == VoiceState.PROCESSING:
                row.state = (
                    prior
                    if prior in {VoiceState.AWAKE, VoiceState.FOLLOW_UP}
                    else VoiceState.AWAKE
                )
            raise
        finally:
            clear_session_in_flight(str(row.id))
        if not (getattr(outcome, "reply", None) or "").strip():
            return await self._fallback_utterance(
                row=row,
                transcript=transcript,
                reply="I heard you, but I don't have a spoken answer yet.",
                error="empty_reply",
            )
        self._refresh_listen_window(row)
        await self._log(
            "utterance" if not follow_up else "follow_up",
            "accepted",
            session_id=row.id,
            device_id=row.device_id,
            transcript_chars=len(outcome.transcript.text),
            reply_chars=len(outcome.reply),
            asr_provider=outcome.transcript.provider,
            tts_provider=outcome.tts.provider,
            asr_degraded=outcome.transcript.degraded,
            tts_degraded=outcome.tts.degraded,
            model=outcome.model,
            sensitive_purpose=sensitive_purpose,
            reverified=reverified,
        )
        return outcome

    async def handle_utterance(
        self,
        *,
        session_id,
        text: str | None = None,
        reverify_token: str | None = None,
        ctx=None,
        audio_b64: str | None = None,
        audio_ref: str | None = None,
        language: str = "en",
        conversation_id=None,
        follow_up: bool = False,
        push_to_talk: bool = False,
        from_ears: bool = False,
    ) -> UtteranceOutcome:
        await self._expire_stale()
        row = await self._get_session(session_id)
        await self._validate_utterance_row(
            row, follow_up, push_to_talk=push_to_talk
        )
        if row.conversation_id is None:
            await self._bind_live_thread(row)
        await self._addressivity_gate(
            row=row,
            audio_b64=audio_b64,
            audio_ref=audio_ref,
            text=text,
            push_to_talk=push_to_talk,
        )

        try:
            transcript = await transcribe_input(
                self.transcriber,
                text=text,
                audio_b64=audio_b64,
                audio_ref=audio_ref,
                language=language,
            )
        except VoiceError as exc:
            if self._is_recoverable_asr(exc):
                dummy = Transcript(
                    text="",
                    confidence=0.0,
                    provider=getattr(self.transcriber, "name", "asr"),
                    details={"code": exc.code},
                )
                reply = self._asr_recovery_reply(exc.code)
                return await self._fallback_utterance(
                    row=row,
                    transcript=dummy,
                    reply=reply,
                    error=reply,
                )
            raise
        if transcript.degraded:
            await self._log(
                "asr",
                "degraded",
                session_id=row.id,
                device_id=row.device_id,
                reason="real ASR provider unavailable",
                asr_provider=transcript.provider,
            )
            raise self._degraded_transcript_error(transcript)
        from app.voice.speech import is_unreadable_transcript

        if is_unreadable_transcript(transcript.text):
            return await self._unreadable_outcome(row=row, transcript=transcript)
        if self._is_sleep_phrase(transcript.text):
            return await self._sleep_outcome(row=row, transcript=transcript)
        if self._is_wake_only(transcript.text):
            return await self._wake_only_listen_outcome(row=row, transcript=transcript)
        if from_ears and self._is_self_echo(row, transcript.text):
            await self._log(
                "utterance",
                "ignored",
                session_id=row.id,
                device_id=row.device_id,
                reason="heard own playback",
            )
            raise VoiceError(
                "Speech ignored — heard EVIE's own reply",
                status=403,
                code="voice_ignored",
            )
        sensitive_purpose, reverified = await self._classify_and_reverify(
            transcript=transcript,
            reverify_token=reverify_token,
            ctx=ctx,
        )
        outcome = await self._run_pipeline_for(
            row=row,
            transcript=transcript,
            conversation_id=conversation_id,
            follow_up=follow_up,
            sensitive_purpose=sensitive_purpose,
            reverified=reverified,
        )
        await self.drain_pending_ingest(device_id=row.device_id, session_id=row.id)
        self._remember_spoken_reply(row.device_id, outcome.reply, tts=outcome.tts)
        if self._interrupt_event(str(session_id)).is_set():
            self._interrupt_event(str(session_id)).clear()
            raise VoiceError(
                "Playback interrupted by barge-in; EVIE is listening",
                status=409,
                code="barge_in",
            )
        return UtteranceOutcome(
            session_id=str(row.id),
            state=VoiceState.FOLLOW_UP,
            transcript=outcome.transcript,
            reply=outcome.reply,
            conversation_id=outcome.conversation_id,
            tts=outcome.tts,
            style=outcome.style,
            model=outcome.model,
            context_tokens=outcome.context_tokens,
            memory_deltas=outcome.memory_deltas,
        )

    async def stream_utterance(
        self,
        *,
        session_id,
        text: str | None = None,
        reverify_token: str | None = None,
        ctx=None,
        audio_b64: str | None = None,
        audio_ref: str | None = None,
        language: str = "en",
        conversation_id=None,
        follow_up: bool = False,
        push_to_talk: bool = False,
    ):
        """SSE-friendly utterance: partial hypotheses, final transcript, reply.

        Yields ``(event, payload)`` tuples: ``partial`` / ``final_transcript``
        / ``reply`` / ``error``. The caller (API layer) serializes them.
        """

        from app.voice.speech import (
            choose_listen_ack,
            is_unreadable_transcript,
            starts_with_evie,
        )

        await self._expire_stale()
        try:
            row = await self._get_session(session_id)
            await self._validate_utterance_row(
                row, follow_up, push_to_talk=push_to_talk
            )
            if row.conversation_id is None:
                await self._bind_live_thread(row)
        except VoiceError as exc:
            # Must yield an SSE error — raising here aborts the stream after
            # HTTP 200 and the Mac client surfaces that as EVAPIError error 0.
            yield "error", exc
            return
        final_transcript: Transcript | None = None
        already_acked = False
        mark_session_in_flight(str(row.id))
        try:
            await self._addressivity_gate(
                row=row,
                audio_b64=audio_b64,
                audio_ref=audio_ref,
                text=text,
                push_to_talk=push_to_talk,
            )
            # Talk / Evie-start: speak the listen-ack immediately. Do not sit
            # silent through ASR (45s) + LLM + TTS. ASR starts in parallel.
            evie_start = starts_with_evie(text or "")
            talk_turn = bool(push_to_talk or row.verifier_name == "push_to_talk")
            early_ack = evie_start or talk_turn
            need_asr = not (text and audio_b64 is None and audio_ref is None)
            asr_task: asyncio.Task | None = None
            if need_asr:
                asr_task = asyncio.create_task(
                    self._drain_asr_stream(
                        audio_ref=audio_ref,
                        audio_b64=audio_b64,
                        text_hint=text,
                        language=language,
                    )
                )
            if early_ack:
                ack_heard = (text or "").strip() or "evie"
                ack_phrase = choose_listen_ack(ack_heard)
                ack_tts = await self._listening_ack(ack_heard)
                already_acked = True
                self._remember_spoken_reply(row.device_id, ack_phrase, tts=ack_tts)
                yield "tts_chunk", TtsChunk(index=0, text=ack_phrase, tts=ack_tts)
            if not need_asr:
                final_transcript = Transcript(
                    text=text or "",
                    confidence=1.0,
                    language=language,
                    provider="text",
                    details={"source": "supplied"},
                )
                yield "final_transcript", final_transcript
            else:
                assert asr_task is not None
                asr_events = await asr_task
                for kind, payload in asr_events:
                    yield kind, payload
                    if kind == "final_transcript" and isinstance(payload, Transcript):
                        final_transcript = payload
            if final_transcript is None:
                raise VoiceError(
                    "ASR stream ended without a final transcript",
                    status=502,
                    code="asr_no_final",
                )
            if final_transcript.degraded:
                await self._log(
                    "asr",
                    "degraded",
                    session_id=row.id,
                    device_id=row.device_id,
                    reason="real ASR provider unavailable",
                    asr_provider=final_transcript.provider,
                )
                raise self._degraded_transcript_error(final_transcript)
            if is_unreadable_transcript(final_transcript.text):
                yield "reply", await self._unreadable_outcome(
                    row=row, transcript=final_transcript
                )
                return
            if self._is_sleep_phrase(final_transcript.text):
                yield "reply", await self._sleep_outcome(
                    row=row,
                    transcript=final_transcript,
                )
                return
            if self._is_wake_only(final_transcript.text):
                if already_acked:
                    self._refresh_listen_window(row)
                    phrase = choose_listen_ack(final_transcript.text or "evie")
                    yield "reply", UtteranceOutcome(
                        session_id=str(row.id),
                        state=VoiceState.FOLLOW_UP,
                        transcript=final_transcript,
                        reply=phrase,
                        conversation_id=(
                            str(row.conversation_id) if row.conversation_id else None
                        ),
                        tts=await self._listening_ack(final_transcript.text),
                    )
                    return
                yield "reply", await self._wake_only_listen_outcome(
                    row=row, transcript=final_transcript
                )
                return
            if self._is_self_echo(row, final_transcript.text):
                await self._log(
                    "utterance",
                    "ignored",
                    session_id=row.id,
                    device_id=row.device_id,
                    reason="heard own playback",
                )
                raise VoiceError(
                    "Speech ignored — heard EVIE's own reply",
                    status=403,
                    code="voice_ignored",
                )
            sensitive_purpose, reverified = await self._classify_and_reverify(
                transcript=final_transcript,
                reverify_token=reverify_token,
                ctx=ctx,
            )
            prior = row.state
            row.state = VoiceState.PROCESSING
            conversation_id = self._normalize_conversation_id(
                conversation_id or row.conversation_id
            )
            outcome: PipelineOutcome | None = None
            try:
                async for kind, payload in stream_chat_tts_pipeline(
                    self.session,
                    actor=self.actor,
                    device_id=row.device_id,
                    transcript=final_transcript,
                    conversation_id=conversation_id,
                    synthesizer=self.synthesizer,
                    speaker_confidence=row.speaker_confidence,
                    skip_listen_ack=already_acked,
                ):
                    if kind == "tts_chunk":
                        yield "tts_chunk", payload
                    elif kind == "outcome" and isinstance(payload, PipelineOutcome):
                        outcome = payload
            except TimeoutError:
                if row.state == VoiceState.PROCESSING:
                    row.state = (
                        prior
                        if prior in {VoiceState.AWAKE, VoiceState.FOLLOW_UP}
                        else VoiceState.AWAKE
                    )
                yield "reply", await self._fallback_utterance(
                    row=row,
                    transcript=final_transcript,
                    reply=(
                        "I heard you, but the answer is taking too long. "
                        "Try asking again in a moment."
                    ),
                    error="That took too long to hear. Try a shorter question.",
                )
                return
            if outcome is None:
                raise VoiceError("Voice reply failed", status=503, code="voice_pipeline")
            if is_unreadable_transcript(outcome.reply or ""):
                yield "reply", await self._unreadable_outcome(
                    row=row, transcript=final_transcript
                )
                return
            self._refresh_listen_window(row)
            self._remember_spoken_reply(row.device_id, outcome.reply, tts=outcome.tts)
            if self._interrupt_event(str(session_id)).is_set():
                self._interrupt_event(str(session_id)).clear()
                raise VoiceError(
                    "Playback interrupted by barge-in; EVIE is listening",
                    status=409,
                    code="barge_in",
                )
            yield "reply", UtteranceOutcome(
                session_id=str(row.id),
                state=VoiceState.FOLLOW_UP,
                transcript=outcome.transcript,
                reply=outcome.reply,
                conversation_id=outcome.conversation_id,
                tts=outcome.tts,
                style=outcome.style,
                model=outcome.model,
                context_tokens=outcome.context_tokens,
                memory_deltas=outcome.memory_deltas,
            )
        except VoiceError as exc:
            if row.state == VoiceState.PROCESSING:
                row.state = VoiceState.AWAKE
            if self._is_recoverable_asr(exc):
                dummy = Transcript(
                    text=final_transcript.text if final_transcript else "",
                    confidence=0.0,
                    provider=getattr(self.transcriber, "name", "asr"),
                    details={"code": exc.code},
                )
                reply = self._asr_recovery_reply(exc.code)
                yield "reply", await self._fallback_utterance(
                    row=row,
                    transcript=dummy,
                    reply=reply,
                    error=reply,
                )
                return
            yield "error", exc
        except Exception:  # noqa: BLE001 - Talk must never drop the SSE socket
            if row.state == VoiceState.PROCESSING:
                row.state = VoiceState.AWAKE
            yield "error", VoiceError(
                "Voice reply failed — try again.",
                status=503,
                code="voice_pipeline",
            )
        finally:
            clear_session_in_flight(str(row.id))

    async def handle_barge_in(self, session_id) -> SessionStatus:
        """Stop playback immediately and re-enter listening (AWAKE)."""

        await self._expire_stale()
        row = await self._get_session(session_id)
        if row.ended_at is not None or row.state == VoiceState.ENDED:
            raise VoiceError(
                "Voice session ended — wake EVIE again",
                status=428,
                code="session_ended",
            )
        if not row.owner_verified:
            raise VoiceError(
                "Session not verified — owner verification required",
                status=403,
                code="not_verified",
            )
        self._interrupt_event(str(session_id)).set()
        now = utcnow()
        row.state = VoiceState.AWAKE
        row.follow_up_until = None
        row.expires_at = now + timedelta(seconds=self.session_timeout_seconds)
        await self._log(
            "barge_in",
            "accepted",
            session_id=row.id,
            device_id=row.device_id,
            reason="speech detected during playback",
        )
        return await self.status(session_id)

    # ------------------------------------------------------------------ #
    # Session control
    # ------------------------------------------------------------------ #

    async def handle_end(self, session_id, *, reason: str = "user-ended") -> SessionStatus:
        row = await self._get_session(session_id)
        if row.ended_at is None:
            row.state = VoiceState.ENDED
            row.ended_at = utcnow()
            row.end_reason = reason
            await self._log(
                "end",
                "accepted",
                session_id=row.id,
                device_id=row.device_id,
                reason=reason,
            )
        return await self.status(session_id)

    async def status(self, session_id) -> SessionStatus:
        row = await self._get_session(session_id)
        enrollment = await self._current_enrollment()
        follow_up_remaining = 0
        if row.state == VoiceState.FOLLOW_UP:
            follow_up_remaining = _window_remaining_seconds(row.follow_up_until)
        return SessionStatus(
            session_id=str(row.id),
            state=row.state,
            owner_enrolled=enrollment is not None,
            owner_verified=row.owner_verified,
            device_id=row.device_id,
            speaker_confidence=row.speaker_confidence,
            follow_up_remaining_seconds=follow_up_remaining,
            expires_at=row.expires_at,
            ended_at=row.ended_at,
            end_reason=row.end_reason,
        )
