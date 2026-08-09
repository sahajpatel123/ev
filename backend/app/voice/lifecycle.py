"""EVIE voice session lifecycle: wake → verify → listen → act → reply → 30s follow-up → idle."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    WakeDetection,
    WakeWordEngine,
)
from app.voice.security import decrypt_payload, encrypt_payload
from app.voice.sensitive import REVERIFY_PURPOSE, classify_sensitive
from app.voice.speaker import ProfileSpeakerVerifier
from app.voice.tts import get_synthesizer
from app.voice.wake import default_wake_engine


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


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=utcnow().tzinfo)


class VoiceError(Exception):
    """Domain error with an HTTP-ish status for the API layer."""

    def __init__(self, message: str, *, status: int = 400, code: str = "voice_error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@dataclass
class WakeOutcome:
    session_id: str | None
    state: str
    owner_enrolled: bool
    challenge_nonce: str | None = None
    challenge_phrase: str | None = None
    message: str | None = None


@dataclass
class VerifyOutcome:
    session_id: str | None
    state: str
    verified: bool
    confidence: float = 0.0
    reason: str = ""


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
        follow_up_seconds: int = 30,
        verify_timeout_seconds: int = 20,
        session_timeout_seconds: int = 120,
    ) -> None:
        self.session = session
        self.master_key = master_key
        self.actor = actor
        self.wake_engine = wake_engine or default_wake_engine()
        self.verifier = verifier or ProfileSpeakerVerifier()
        self.transcriber = transcriber or get_transcriber()
        self.synthesizer = synthesizer or get_synthesizer()
        self.liveness = liveness or LivenessGate()
        self.follow_up_seconds = follow_up_seconds
        self.verify_timeout_seconds = verify_timeout_seconds
        self.session_timeout_seconds = session_timeout_seconds
        self.replay_guard = ReplayGuard(session)

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
        result = await self.session.execute(
            select(VoiceSession).where(
                VoiceSession.ended_at.is_(None),
                VoiceSession.expires_at.is_not(None),
                VoiceSession.expires_at < now,
            )
        )
        for row in result.scalars().all():
            row.state = VoiceState.ENDED
            row.ended_at = now
            row.end_reason = "session timeout"
            await self._log(
                "timeout",
                "timeout",
                session_id=row.id,
                device_id=row.device_id,
                reason="session timeout",
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

    async def _get_session(self, session_id) -> VoiceSession:
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

    async def handle_wake(
        self,
        *,
        device_id: str,
        audio_ref: str | None = None,
        text_hint: str | None = None,
        wake_word: str = "evie",
        frames: bytes | None = None,
    ) -> WakeOutcome:
        await self._expire_stale()
        detection: WakeDetection = await self.wake_engine.detect(
            audio_ref=audio_ref,
            device_id=device_id,
            frames=frames,
            text_hint=text_hint,
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
            audio_sha256=None,
        )
        if not detection.triggered:
            return WakeOutcome(
                session_id=None,
                state=VoiceState.IDLE,
                owner_enrolled=(await self._current_enrollment()) is not None,
                message="Wake word not detected",
            )
        await self._require_voice_consent()
        self._enforce_remote_voiceprint_gate()

        existing = await self._active_session(device_id)
        if existing is not None:
            return WakeOutcome(
                session_id=str(existing.id),
                state=existing.state,
                owner_enrolled=True,
                challenge_nonce=existing.challenge_nonce,
                challenge_phrase=existing.challenge_phrase,
                message="Voice session already active",
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
            return WakeOutcome(
                session_id=None,
                state=VoiceState.IDLE,
                owner_enrolled=False,
                message="No owner voiceprint enrolled. Enroll before enabling voice activation.",
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
            row.state = VoiceState.AWAKE
            row.owner_verified = True
            row.speaker_confidence = confidence
            row.verifier_name = decisions[0].algorithm
            row.verified_at = utcnow()
            row.expires_at = utcnow() + timedelta(seconds=self.session_timeout_seconds)
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
    ) -> UtteranceOutcome:
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
        if follow_up:
            if row.state != VoiceState.FOLLOW_UP:
                raise VoiceError(
                    f"Follow-up only valid from follow_up state (current: {row.state})",
                    status=409,
                    code="invalid_state",
                )
            if row.follow_up_until is None or _aware(row.follow_up_until) < utcnow():
                row.state = VoiceState.ENDED
                row.ended_at = utcnow()
                row.end_reason = "follow-up window expired"
                raise VoiceError(
                    "30-second follow-up window expired — say 'EVIE' to wake again",
                    status=428,
                    code="follow_up_expired",
                )
        elif row.state != VoiceState.AWAKE:
            raise VoiceError(
                f"Utterance only valid from awake state (current: {row.state})",
                status=409,
                code="invalid_state",
            )

        row.state = VoiceState.PROCESSING
        from app.voice.pipeline import run_chat_tts_pipeline, transcribe_input

        transcript = await transcribe_input(
            self.transcriber,
            text=text,
            audio_b64=audio_b64,
            audio_ref=audio_ref,
            language=language,
        )
        sensitive_purpose = classify_sensitive(transcript.text)
        reverified = False
        if sensitive_purpose is not None:
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
            reverified = True
        outcome = await run_chat_tts_pipeline(
            self.session,
            actor=self.actor,
            device_id=row.device_id,
            transcript=transcript,
            conversation_id=conversation_id,
            synthesizer=self.synthesizer,
        )

        now = utcnow()
        row.state = VoiceState.FOLLOW_UP
        row.last_utterance_at = now
        row.follow_up_until = now + timedelta(seconds=self.follow_up_seconds)
        row.expires_at = now + timedelta(seconds=self.session_timeout_seconds)
        await self._log(
            "utterance" if not follow_up else "follow_up",
            "accepted",
            session_id=row.id,
            device_id=row.device_id,
            transcript_chars=len(outcome.transcript.text),
            reply_chars=len(outcome.reply),
            asr_provider=outcome.transcript.provider,
            tts_provider=outcome.tts.provider,
            model=outcome.model,
            sensitive_purpose=sensitive_purpose,
            reverified=reverified,
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
        if row.state == VoiceState.FOLLOW_UP and row.follow_up_until is not None:
            follow_up_remaining = max(
                0,
                int((_aware(row.follow_up_until) - utcnow()).total_seconds()),
            )
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
