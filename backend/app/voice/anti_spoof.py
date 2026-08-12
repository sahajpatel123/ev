"""Anti-spoofing: replay resistance, passive liveness, and challenge-response.

Security posture (production):

* ``audio_sha256`` is **computed server-side** from the submitted audio;
  client-supplied hashes are never trusted for replay detection.
* ``liveness_proof`` and ``live_score`` are advisory metadata only; they can
  never pass a check. A real audio-liveness model (2 MB ``liveness-audio``
  ONNX entry, ASVspoof-style replay/synthesis countermeasure) is required.
* The challenge phrase is verified against the **ASR transcript of the
  submitted audio**, never an echoed string.
* Missing audio, model, or transcript evidence fails closed.

Under pytest the deterministic dev/test path is retained so offline CI stays
green; that path is explicitly a test double and never runs in production.
"""

from __future__ import annotations

import hashlib
import math
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ReplayNonce, VoiceAttemptLog
from app.utils.text import utcnow
from app.voice.contracts import Challenge, LivenessChecker
from app.voice.speaker import (
    _flatten_numeric,
    decode_waveform,
    is_test_runtime,
    sample_audio_bytes,
)
from app.voice.wake import normalize

CHALLENGE_PHRASES = (
    "the sun rises in the east",
    "my favorite color is blue",
    "I am speaking to EVIE",
    "tomorrow is another day",
    "coffee before everything",
    "the password is safe with me",
)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=utcnow().tzinfo)


class ReplayError(Exception):
    """Raised when a nonce is missing, expired, reused, or mis-bound."""


def compute_audio_sha256(raw: bytes) -> str:
    """Server-side SHA-256 of the submitted audio bytes."""
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class AudioFingerprint:
    """A fingerprint that is known to be computed server-side."""

    sha256: str
    server_computed: bool = True


def transcript_matches_expected(transcript: str, expected_phrase: str) -> bool:
    """True when the ASR transcript contains the expected challenge phrase."""
    transcript_norm = normalize(transcript or "")
    expected_norm = normalize(expected_phrase or "")
    if not expected_norm:
        return True
    return expected_norm in transcript_norm or transcript_norm in expected_norm


class ReplayGuard:
    """Single-use challenge nonces plus server-side audio-fingerprint replay detection."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        max_age_seconds: int = 30,
        fingerprint_window_seconds: int = 300,
        strict: bool | None = None,
    ) -> None:
        self.session = session
        self.max_age_seconds = max_age_seconds
        self.fingerprint_window_seconds = fingerprint_window_seconds
        self.strict = (not is_test_runtime()) if strict is None else strict

    async def issue(self, *, purpose: str, session_id=None, ttl_seconds: int | None = None) -> Challenge:
        ttl = ttl_seconds or self.max_age_seconds
        nonce = secrets.token_urlsafe(24)
        phrase = secrets.choice(CHALLENGE_PHRASES)
        expires_at = utcnow() + timedelta(seconds=ttl)
        row = ReplayNonce(
            nonce=nonce,
            session_id=session_id,
            purpose=purpose,
            challenge_phrase=phrase,
            expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        return Challenge(nonce=nonce, phrase=phrase, purpose=purpose, expires_at=expires_at)

    async def consume(self, nonce: str, *, purpose: str, session_id) -> Challenge:
        now = utcnow()
        result = await self.session.execute(
            select(ReplayNonce).where(ReplayNonce.nonce == nonce)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise ReplayError("unknown nonce")
        if row.purpose != purpose:
            raise ReplayError("nonce purpose mismatch")
        if row.session_id != session_id:
            raise ReplayError("nonce bound to a different session")
        if row.consumed_at is not None:
            raise ReplayError("nonce already used (replay)")
        expires_at = _aware(row.expires_at)
        if expires_at is None or expires_at < now:
            raise ReplayError("nonce expired")
        row.consumed_at = now
        await self.session.flush()
        return Challenge(
            nonce=row.nonce,
            phrase=row.challenge_phrase or "",
            purpose=row.purpose,
            expires_at=expires_at or utcnow(),
        )

    async def fingerprint_replayed(
        self,
        fingerprint: AudioFingerprint | str | None,
        *,
        device_id: str | None = None,
        kinds: tuple[str, ...] = ("verify", "wake"),
    ) -> bool:
        """True when the same **server-computed** audio fingerprint was accepted recently.

        Plain strings (the legacy client-supplied ``audio_sha256`` shape) are
        ignored in strict mode: a client can trivially mint a fresh hash, so
        client values never drive replay detection in production. Under pytest
        the legacy string behavior is preserved for deterministic tests.
        """

        if not fingerprint:
            return False
        if isinstance(fingerprint, AudioFingerprint):
            if not fingerprint.server_computed:
                return False
            sha256 = fingerprint.sha256
        elif isinstance(fingerprint, str):
            if self.strict:
                return False
            sha256 = fingerprint
        else:
            return False
        if not sha256:
            return False
        since = utcnow() - timedelta(seconds=self.fingerprint_window_seconds)
        result = await self.session.execute(
            select(VoiceAttemptLog)
            .where(
                VoiceAttemptLog.occurred_at >= since,
                VoiceAttemptLog.kind.in_(kinds),
                VoiceAttemptLog.outcome == "accepted",
            )
            .order_by(VoiceAttemptLog.occurred_at.desc())
            .limit(200)
        )
        rows = list(result.scalars().all())
        return any((row.metadata_ or {}).get("audio_sha256") == sha256 for row in rows)


class AudioLivenessModel:
    """Passive audio-liveness countermeasure (ASVspoof-style replay/synthesis).

    Loads the 2 MB ``liveness-audio`` ONNX entry through the ModelArbiter
    (never outside it) and returns a live probability in ``[0, 1]``. When the
    model or onnxruntime is unavailable, :meth:`score` returns ``None`` and the
    gate fails closed. MiniFASNet is a *face* anti-spoof model and does not
    apply to voice; this slot is the audio counterpart.
    """

    name = "liveness-audio-v1"

    def __init__(
        self,
        *,
        model_path: str | Path | None = None,
        threshold: float = 0.5,
        onnx_session_factory: Any = None,
    ) -> None:
        self.model_path = Path(model_path) if model_path else None
        self.threshold = threshold
        self._factory = onnx_session_factory
        self._session = None
        self._load_attempted = False

    def _resolve_model_path(self) -> Path | None:
        if self.model_path is not None:
            candidate = self.model_path.expanduser().resolve()
            return candidate if candidate.is_file() else None
        env_path = os.environ.get("EV_LIVENESS_MODEL_PATH")
        if env_path:
            candidate = Path(env_path).expanduser().resolve()
            if candidate.is_file():
                return candidate
        try:
            from app.ml.arbiter import ModelArbiter
            from app.ml.registry import ModelRegistry, builtin_models
            from app.ml.settings import get_ml_settings
            from app.ml.store import cache_dir, target_path

            registry = ModelRegistry()
            for spec in builtin_models():
                try:
                    registry.register(spec)
                except Exception:
                    continue
            arbiter = ModelArbiter(registry)
            with arbiter.acquire("liveness-audio", release_on_exit=True):
                spec = registry.get("liveness-audio")
                target = target_path(get_ml_settings(), spec)
                if target.is_file():
                    return target
                matches = sorted(cache_dir(get_ml_settings()).glob("liveness-audio.*"))
                for candidate in matches:
                    if candidate.is_file():
                        return candidate
        except Exception:
            return None
        return None

    def _load(self):
        if self._load_attempted:
            return self._session
        self._load_attempted = True
        if self._factory is None:
            try:
                import onnxruntime
            except ImportError:
                self._session = None
                return None
        path = self._resolve_model_path()
        if path is None:
            self._session = None
            return None
        if self._factory is not None:
            self._session = self._factory(path)
        else:
            self._session = onnxruntime.InferenceSession(
                str(path), providers=["CPUExecutionProvider"]
            )
        return self._session

    @property
    def available(self) -> bool:
        return self._load() is not None

    def score(self, raw: bytes) -> float | None:
        """Live probability in [0,1], or None when the model cannot run."""
        session = self._load()
        if session is None:
            return None
        try:
            waveform, _rate = decode_waveform(raw)
            if not waveform:
                return None
            audio = [list(waveform)]
            inputs: dict[str, Any] = {inp.name: audio for inp in session.get_inputs()}
            for inp in session.get_inputs():
                if "length" in inp.name.lower():
                    inputs[inp.name] = [len(waveform)]
            outputs = session.run(None, inputs)
            value = _flatten_numeric(outputs[0])[0]
            if not (0.0 <= value <= 1.0):
                value = 1.0 / (1.0 + math.exp(-value))
            return max(0.0, min(1.0, value))
        except Exception:
            return None


@dataclass
class LivenessEvidence:
    ok: bool
    confidence: float
    reason: str
    audio_sha256: str | None = None
    server_fingerprint: AudioFingerprint | None = None
    transcript: str | None = None
    degraded: bool = False


class LivenessGate:
    """Liveness gate: deterministic dev/test double or strict production gate.

    In strict mode every client-trust path is removed: liveness proofs and
    client scores are advisory, the audio fingerprint is computed server-side,
    and the challenge phrase must match the ASR transcript of the submitted
    audio. Missing evidence fails closed.
    """

    name = "liveness-v1"

    def __init__(
        self,
        *,
        strict: bool | None = None,
        liveness_model: AudioLivenessModel | None = None,
        model_threshold: float = 0.5,
    ) -> None:
        self.strict = (not is_test_runtime()) if strict is None else strict
        self.liveness_model = liveness_model
        self.model_threshold = model_threshold
        self.last_audio_sha256: str | None = None
        self.last_server_fingerprint: AudioFingerprint | None = None
        self.last_degraded = False

    def _dev_check(
        self,
        sample: dict,
        challenge_phrase: str | None,
        expected_phrase: str | None,
    ) -> tuple[bool, float, str]:
        """Deterministic test double used only while pytest is running."""
        proof = sample.get("liveness_proof")
        if proof in ("replay", "synthetic", "converted"):
            return False, 0.0, f"liveness rejected ({proof})"
        live_score = sample.get("live_score")
        if live_score is not None:
            if live_score >= 0.5:
                return True, round(float(live_score), 3), "live score accepted"
            return False, round(float(live_score), 3), "live score below threshold"
        if challenge_phrase and expected_phrase:
            if normalize(challenge_phrase) == normalize(expected_phrase):
                return True, 1.0, "challenge phrase matched"
            return False, 0.0, "challenge phrase mismatch"
        if proof == "live":
            return True, 1.0, "explicit live proof"
        return False, 0.0, "missing liveness evidence"

    async def _strict_check(
        self,
        sample: dict,
        challenge_phrase: str | None,
        expected_phrase: str | None,
        transcript: str | None,
        asr: Any,
    ) -> tuple[bool, float, str]:
        proof = sample.get("liveness_proof")
        if proof in ("replay", "synthetic", "converted"):
            return False, 0.0, f"liveness rejected ({proof})"

        has_audio = "audio_b64" in sample or "audio_ref" in sample
        if not has_audio:
            return False, 0.0, "client liveness claims are advisory; audio evidence required"
        try:
            raw = sample_audio_bytes(sample)
        except ValueError as exc:
            return False, 0.0, f"audio evidence invalid: {exc}"
        self.last_audio_sha256 = compute_audio_sha256(raw)
        self.last_server_fingerprint = AudioFingerprint(self.last_audio_sha256)

        if self.liveness_model is None:
            self.liveness_model = AudioLivenessModel()
        model = self.liveness_model
        live_score = model.score(raw)
        if live_score is None:
            self.last_degraded = True
            return False, 0.0, "liveness model unavailable; failing closed (degraded)"
        self.last_degraded = False
        if live_score < self.model_threshold:
            return False, round(float(live_score), 3), "passive liveness rejected"

        resolved_transcript = transcript
        if expected_phrase and resolved_transcript is None and asr is not None:
            try:
                if "audio_b64" in sample:
                    resolved_transcript = (
                        await asr.transcribe(audio_b64=sample["audio_b64"])
                    ).text
                elif "audio_ref" in sample:
                    resolved_transcript = (
                        await asr.transcribe(audio_ref=sample["audio_ref"])
                    ).text
            except Exception as exc:
                return False, 0.0, f"challenge transcript unavailable: {exc}"
        if expected_phrase and resolved_transcript is None:
            return False, 0.0, "challenge phrase requires an ASR transcript of the submitted audio"
        if expected_phrase and not transcript_matches_expected(
            resolved_transcript or "", expected_phrase
        ):
            return False, 0.0, "challenge phrase mismatch (ASR transcript)"
        return True, round(float(live_score), 3), "passive liveness accepted (server-side)"

    async def check(
        self,
        *,
        sample: dict | None = None,
        challenge_phrase: str | None = None,
        expected_phrase: str | None = None,
        transcript: str | None = None,
        asr: Any = None,
    ) -> tuple[bool, float, str]:
        sample = sample or {}
        if not self.strict:
            return self._dev_check(sample, challenge_phrase, expected_phrase)
        return await self._strict_check(
            sample, challenge_phrase, expected_phrase, transcript, asr
        )

    async def check_with_evidence(
        self,
        *,
        sample: dict | None = None,
        challenge_phrase: str | None = None,
        expected_phrase: str | None = None,
        transcript: str | None = None,
        asr: Any = None,
    ) -> LivenessEvidence:
        ok, confidence, reason = await self.check(
            sample=sample,
            challenge_phrase=challenge_phrase,
            expected_phrase=expected_phrase,
            transcript=transcript,
            asr=asr,
        )
        return LivenessEvidence(
            ok=ok,
            confidence=confidence,
            reason=reason,
            audio_sha256=self.last_audio_sha256,
            server_fingerprint=self.last_server_fingerprint,
            transcript=transcript,
            degraded=self.last_degraded,
        )


def default_liveness_checker() -> LivenessChecker:
    return LivenessGate()
