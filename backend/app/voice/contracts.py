"""Provider contracts for the voice & speech layer.

The runtime depends only on these protocols. Concrete engines (Sensory/AON1100
wake, ECAPA-TDNN verification, Whisper-class ASR, natural TTS) can be swapped
without touching lifecycle or API code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class VoiceError(Exception):
    """Domain error with an HTTP-ish status for the API layer.

    Lives in the contracts module so every provider (ASR, TTS, lifecycle) can
    raise it without importing the lifecycle orchestrator.
    """

    def __init__(self, message: str, *, status: int = 400, code: str = "voice_error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class ModelUnavailableError(RuntimeError):
    """A real engine cannot run because weights/dependencies are missing.

    Providers translate this into a ``degraded=True`` result when that is the
    safe behavior, or let it propagate as a typed VoiceError when the caller
    must fail closed.
    """


def _model_arbiter():
    # Reuse the ears stack's process-wide arbiter so ASR/TTS and wake/VAD share
    # one resident-memory policy (docs/MODEL_BUDGET.md §2).
    from app.audio.models import model_arbiter

    return model_arbiter()


@contextmanager
def acquire_model(name: str):
    """Reserve ``name`` in the process-wide ModelArbiter (on_demand slot)."""

    from app.ml.arbiter import ModelLoadRefused
    from app.ml.registry import ModelNotFoundError

    try:
        with _model_arbiter().acquire(name):
            yield
    except ModelNotFoundError as exc:
        raise ModelUnavailableError(
            f"model {name!r} is not registered with the ModelArbiter; "
            "Agent 2 (Foundry) must add the registry entry"
        ) from exc
    except ModelLoadRefused as exc:
        raise ModelUnavailableError(str(exc)) from exc


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
    """One final transcript from an ASR engine.

    ``degraded=True`` means the real provider could not run (weights missing,
    runtime unavailable) and the text must never be treated as a real
    transcription; confidence is then 0.0 by contract.
    """

    text: str
    confidence: float
    language: str = "en"
    provider: str = "dev"
    duration_ms: int | None = None
    audio_ref: str | None = None
    degraded: bool = False
    details: dict = field(default_factory=dict)


@dataclass
class TranscriptPartial:
    """Incremental hypothesis emitted while the human is still speaking."""

    text: str
    provider: str
    sequence: int
    stable: bool = False
    confidence: float = 0.0
    language: str = "en"
    degraded: bool = False
    timestamp_ms: int | None = None


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
    degraded: bool = False
    details: dict = field(default_factory=dict)


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

    def stream(
        self,
        *,
        audio_ref: str | None = None,
        audio_b64: str | None = None,
        text_hint: str | None = None,
        language: str = "en",
    ) -> AsyncIterator[Transcript | TranscriptPartial]:
        """Incremental hypotheses (partials) followed by the final transcript.

        Engines without streaming support simply yield their final transcript.
        """

        async def _default() -> AsyncIterator[Transcript | TranscriptPartial]:
            yield await self.transcribe(
                audio_ref=audio_ref,
                audio_b64=audio_b64,
                text_hint=text_hint,
                language=language,
            )

        return _default()


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
