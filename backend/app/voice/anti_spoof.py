"""Anti-spoofing: replay resistance, liveness, and challenge-response."""

from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ReplayNonce, VoiceAttemptLog
from app.utils.text import utcnow
from app.voice.contracts import Challenge, LivenessChecker
from app.voice.wake import normalize

CHALLENGE_PHRASES = (
    "the sun rises in the east",
    "my favorite color is blue",
    "I am speaking to EVIE",
    "tomorrow is another day",
    "coffee before everything",
    "the password is safe with me",
)


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=utcnow().tzinfo)


class ReplayError(Exception):
    """Raised when a nonce is missing, expired, reused, or mis-bound."""


class ReplayGuard:
    """Single-use challenge nonces plus audio-fingerprint replay detection."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        max_age_seconds: int = 30,
        fingerprint_window_seconds: int = 300,
    ) -> None:
        self.session = session
        self.max_age_seconds = max_age_seconds
        self.fingerprint_window_seconds = fingerprint_window_seconds

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
        expires_at = row.expires_at
        if _aware(expires_at) is None or _aware(expires_at) < now:
            raise ReplayError("nonce expired")
        row.consumed_at = now
        await self.session.flush()
        return Challenge(
            nonce=row.nonce,
            phrase=row.challenge_phrase or "",
            purpose=row.purpose,
            expires_at=expires_at,
        )

    async def fingerprint_replayed(
        self,
        fingerprint: str | None,
        *,
        device_id: str | None = None,
        kinds: tuple[str, ...] = ("verify", "wake"),
    ) -> bool:
        """True if the same audio fingerprint was already accepted recently."""
        if not fingerprint:
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
        return any((row.metadata_ or {}).get("audio_sha256") == fingerprint for row in rows)


class LivenessGate:
    """Deterministic liveness gate for dev/test and challenge-response flows."""

    name = "liveness-v1"

    async def check(
        self,
        *,
        sample: dict,
        challenge_phrase: str | None = None,
        expected_phrase: str | None = None,
    ) -> tuple[bool, float, str]:
        proof = (sample or {}).get("liveness_proof")
        if proof in ("replay", "synthetic", "converted"):
            return False, 0.0, f"liveness rejected ({proof})"
        live_score = (sample or {}).get("live_score")
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


def default_liveness_checker() -> LivenessChecker:
    return LivenessGate()
