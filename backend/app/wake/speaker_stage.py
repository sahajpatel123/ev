"""Owner speaker stage (W2): fast wake-phrase confidence → full-utterance recheck.

Contract (directive §9):

 - Initial wake-phrase speaker score provides FAST OWNER CONFIDENCE.
 - Do NOT make a fragile one-word embedding the entire identity architecture.
 - Continue accumulating the early owner command, then perform FULL-UTTERANCE
   SPEAKER RE-CHECK for stronger evidence.

Enrollment: Use several "EVIE" + "Evie, <short command>" examples; keep the
initial profile controlled; do NOT auto-learn from every accepted wake
initially (false accepts would poison the profile). Implicit adaptation only
after trustworthy acceptance evidence.

This module is the server-side stage that follows Stage-2. The ears process
itself stays tiny; CAM++/SpeechBrain runs after Stage-1 candidate, not on idle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpeakerFastResult:
    confidence: float
    threshold: float
    verified: bool
    diagnostics: dict


@dataclass(frozen=True)
class SpeakerFullResult:
    confidence: float
    threshold: float
    verified: bool
    diagnostics: dict


class SpeakerStage:
    """Two-pass owner check: fast wake-phrase then full-utterance accumulation."""

    def __init__(
        self,
        *,
        wake_threshold: float | None = None,
        full_threshold: float | None = None,
    ) -> None:
        from app.config import settings

        # Thresholds are NOT magic numbers — they are the operating point
        # chosen from measured owner/impostor curves (see calibrate_operating_point
        # → eval/ml/speaker_security.json, FAR=0, TAR at threshold). The code
        # holds the *current* operating point, not a law.
        self.wake_threshold = (
            float(wake_threshold)
            if wake_threshold is not None
            else float(settings.voiceprint_wake_threshold)
        )
        self.full_threshold = (
            float(full_threshold)
            if full_threshold is not None
            else float(settings.voiceprint_threshold)
        )

    async def fast_wake_phrase_check(
        self,
        *,
        enrollment,
        sample: dict,
    ) -> SpeakerFastResult:
        """One-word wake-phrase embedding — fast, deliberately fragile signal.

        Caller should NOT treat this as the entire identity architecture; it is
        the fast owner confidence before the early command is accumulated.
        """
        from app.voice.speaker import default_speaker_verifier

        verifier = default_speaker_verifier()
        # Decrypt enrollment lazily — mirrors lifecycle._decrypt_enrollment.
        # For the scaffold we assume enrollment payload is already available;
        # in production VoiceRuntime handles decryption.
        raise NotImplementedError  # placeholder for direct-call path — see lifecycle integration

    async def full_utterance_recheck(
        self,
        *,
        enrollment_payload: dict,
        accumulated_audio_b64: str | None,
        verifier=None,
    ) -> SpeakerFullResult:
        """Accumulate the early owner command and re-score for stronger evidence."""
        if verifier is None:
            from app.voice.speaker import default_speaker_verifier

            verifier = default_speaker_verifier()
        sample = {"audio_b64": accumulated_audio_b64} if accumulated_audio_b64 else {}
        # The actual verify is done via verifier.verify(sample, payload, threshold=full);
        # scaffold exposes the contract without hardcoding wake-host decoding.
        return SpeakerFullResult(
            confidence=0.0,
            threshold=self.full_threshold,
            verified=False,
            diagnostics={"stage": "full_utterance", "threshold": self.full_threshold},
        )

    def diagnose(self, fast: SpeakerFastResult, full: SpeakerFullResult) -> dict:
        """Bounded diagnostics for the two-pass check (no raw audio)."""
        return {
            "fast_confidence": fast.confidence,
            "full_confidence": full.confidence,
            "wake_threshold": self.wake_threshold,
            "full_threshold": self.full_threshold,
            "fast_verified": fast.verified,
            "full_verified": full.verified,
        }
