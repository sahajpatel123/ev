"""Provider contracts for the voice & speech layer.

The runtime depends only on these protocols. Concrete engines (Sensory/AON1100
wake, ECAPA-TDNN verification, Whisper-class ASR, natural TTS) can be swapped
without touching lifecycle or API code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class WakeDetection:
    triggered: bool
    wake_word: str = "evie"
    confidence: float = 0.0
    device_id: str | None = None
    stage: str = "low_power"  # low_power | burst
    power_state: str = "low_power"  # mW-class always-on front end
    details: dict = field(default_factory=dict)


@dataclass
class SpeakerDecision:
    verified: bool
    confidence: float
    threshold: float
    algorithm: str
    speaker_id: str | None = None
    reason: str = ""


@dataclass
class Transcript:
    text: str
    confidence: float
    language: str = "en"
    provider: str = "dev"
    duration_ms: int | None = None
    audio_ref: str | None = None


@dataclass
class SpeechStyle:
    """Urgency/warmth/brevity controls passed to the TTS layer."""

    urgency: float = 0.0
    warmth: float = 0.6
    brevity: float = 0.4
    mode: str = "casual"
    length_target: str = "one to two sentences"
    directness: str = "low to medium"


@dataclass
class SynthesisResult:
    text: str
    provider: str
    audio_ref: str | None = None
    audio: bytes | None = None
    content_type: str | None = None
    ssml: str | None = None
    duration_ms: int | None = None
    style: SpeechStyle = field(default_factory=SpeechStyle)


class WakeWordEngine(Protocol):
    """Always-on low-power wake listener (Sensory/AON1100-class)."""

    name: str
    power_state: str = "low_power"

    async def detect(
        self,
        *,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
        device_id: str | None = None,
        frames: bytes | None = None,
        text_hint: str | None = None,
    ) -> WakeDetection: ...


class SpeakerVerifier(Protocol):
    """Owner-only speaker verification over enrolled voiceprints."""

    name: str
    embedding_dim: int = 0

    async def enroll(self, samples: list[dict], *, reason: str | None = None) -> dict:
        """Return the versioned voiceprint payload (embedding, threshold, dim)."""
        ...

    async def verify(
        self,
        sample: dict,
        *,
        enrolled_payload: dict,
        threshold: float | None = None,
    ) -> SpeakerDecision: ...


class Transcriber(Protocol):
    """Whisper-class ASR. The runtime never couples to a specific provider."""

    name: str

    async def transcribe(
        self,
        *,
        audio_ref: str | None = None,
        audio_b64: str | None = None,
        text_hint: str | None = None,
        language: str = "en",
    ) -> Transcript: ...


class Synthesizer(Protocol):
    """Natural TTS with urgency/warmth/brevity controls."""

    name: str

    async def synthesize(self, text: str, *, style: SpeechStyle) -> SynthesisResult: ...


class LivenessChecker(Protocol):
    """Anti-spoofing/liveness gate for verification samples."""

    name: str

    async def check(
        self,
        *,
        sample: dict,
        challenge_phrase: str | None = None,
        expected_phrase: str | None = None,
    ) -> tuple[bool, float, str]: ...


@dataclass
class Challenge:
    nonce: str
    phrase: str
    purpose: str
    expires_at: datetime
