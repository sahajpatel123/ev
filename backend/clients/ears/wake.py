"""Offline wake engines for the ears process.

``PhraseWakeEngine`` (app/voice/wake.py) is the canonical dev double. The
ears process uses this lightweight mirror for the pure-offline path so that
importing ``app.voice`` (whose package init eagerly loads the full lifecycle
stack) does not inflate RSS when no real engine is configured.

When a real openWakeWord head is configured the ears process imports the real
engine instead (``app.voice.wake.OpenWakeWordEngine``). When the head is
missing but on-device spotting is enabled (``EV_EARS_WAKE_LOCAL_SPOTTER=true``,
the default), ``LocalWhisperWakeSpotter`` runs a small faster-whisper model in
the ears process so "EVIE" is actually heard — the byte-matching fallback below
can never match real speech, which is why wake silently failed.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field

# Siri-style strict wake: the owner's NAME is the only wake token. Acoustically
# near words (every/even/Stevie) are never candidates.
WAKE_PHRASES = (
    "eve",
    "evie",
    "hey eve",
    "hi eve",
    "hello eve",
    "ok eve",
    "okay eve",
    "hey evie",
    "hi evie",
    "hello evie",
    "ok evie",
    "okay evie",
    "evie wake",
    "evie wake up",
    "eve here",
    "evie here",
)
# Name as a whole word (word boundary) so "even"/"every"/"Stevie" never match.
WAKE_TOKEN = re.compile(r"\b(?:eve|evie|eevee|ee vee)\b", re.IGNORECASE)


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


@dataclass
class WakeDetectionLike:
    """Minimal mirror of app.voice.contracts.WakeDetection.

    Kept local so the ears default path never imports ``app.voice`` (whose
    package init eagerly loads the lifecycle stack, +42 MB RSS). Only the
    fields the ears loop consumes are defined; the real engine returns the
    canonical contract type.
    """

    triggered: bool
    confidence: float
    wake_word: str = "evie"
    device_id: str | None = None
    stage: str = "low_power"
    power_state: str = "low_power"
    details: dict = field(default_factory=dict)


class PhraseFallbackWake:
    """Deterministic text/frame wake double mirroring PhraseWakeEngine.

    The frame path is a byte search and never matches real speech; it exists
    only so the pure-offline test harness has a deterministic double. Live
    use should prefer ``LocalWhisperWakeSpotter`` (or a real openWakeWord head).
    """

    name = "phrase-fallback"
    power_state = "low_power"

    async def detect(
        self,
        *,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
        device_id: str | None = None,
        frames: bytes | None = None,
        text_hint: str | None = None,
    ) -> WakeDetectionLike:
        if text_hint is not None:
            normalized = normalize(text_hint)
            triggered = normalized in WAKE_PHRASES or bool(WAKE_TOKEN.search(normalized))
            confidence = 0.98 if normalized in WAKE_PHRASES else 0.55
        elif frames is not None:
            lowered = frames.lower()
            triggered = b"evie" in lowered or b"evi " in lowered
            confidence = 0.9 if triggered else 0.0
        else:
            triggered = False
            confidence = 0.0
        return WakeDetectionLike(
            triggered=triggered,
            wake_word="evie",
            confidence=confidence,
            device_id=device_id,
            stage="low_power" if not triggered else "burst",
            power_state="low_power",
            details={"engine": self.name, "sample_rate": sample_rate, "audio_ref": audio_ref},
        )


class LocalWhisperWakeSpotter:
    """On-device "EVIE" spotter via a small faster-whisper model.

    Loads lazily on the first loud VAD segment so the ears process starts
    light, then reuses the model for every later clip. Uses a dedicated tiny
    model (``EV_EARS_WAKE_ASR_MODEL``, default ``tiny``) rather than the ASR
    model so a wake check is a fast local pass, and returns the transcript so
    the server can extract a same-clip command without re-transcribing.
    """

    name = "whisper-spotter"
    power_state = "burst"

    def __init__(self, *, model: str = "tiny", threshold: float = 0.5) -> None:
        self._model = model or "tiny"
        self._threshold = threshold
        self._engine = None

    def _engine_or_default(self):
        if self._engine is not None:
            return self._engine
        from app.voice.asr import FasterWhisperTranscriber
        from app.voice.wake import WhisperPhraseWakeEngine

        transcriber = FasterWhisperTranscriber(model=self._model, vad_filter=False)
        self._engine = WhisperPhraseWakeEngine(transcriber=transcriber)
        return self._engine

    async def warmup(self) -> None:
        """Preload the spotter's model so the first real clip is not a cold start.

        The first ``detect`` on a fresh ears process pays a model download +
        load (measured ~15 s), which reads as "EVIE didn't hear me". Loading
        once here moves that cost to startup, so the first spoken wake is fast.
        """

        import asyncio

        engine = self._engine_or_default()
        transcriber = getattr(engine, "_transcriber", None)
        if transcriber is None:
            transcriber = engine._transcriber_or_default()

        def _load() -> None:
            model = transcriber._load_model()  # noqa: SLF001 - same-package seam
            if model is None:
                return
            # One silent inference forces CTranslate2 to finish lazy kernel
            # init, so even the very first clip is at steady-state latency.
            import os
            import tempfile
            import wave

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                path = handle.name
            try:
                with wave.open(path, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(b"\x00\x00" * 8000)  # 0.5 s silence
                list(
                    model.transcribe(
                        path,
                        language="en",
                        beam_size=1,
                        temperature=0.0,
                        vad_filter=False,
                    )
                )
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(path)

        await asyncio.to_thread(_load)

    async def detect(
        self,
        *,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
        device_id: str | None = None,
        frames: bytes | None = None,
        text_hint: str | None = None,
    ) -> WakeDetectionLike:
        if text_hint is not None and frames is None and audio_ref is None:
            normalized = normalize(text_hint)
            triggered = normalized in WAKE_PHRASES or bool(WAKE_TOKEN.search(normalized))
            confidence = 0.98 if triggered else 0.0
            return WakeDetectionLike(
                triggered=triggered,
                wake_word="evie",
                confidence=confidence,
                device_id=device_id,
                stage="burst" if triggered else "low_power",
                power_state=self.power_state,
                details={"engine": self.name, "source": "text_hint"},
            )
        try:
            engine = self._engine_or_default()
            detection = await engine.detect(
                audio_ref=audio_ref,
                sample_rate=sample_rate,
                device_id=device_id,
                frames=frames,
            )
        except Exception as exc:  # noqa: BLE001 - wake must never crash the ears loop
            return WakeDetectionLike(
                triggered=False,
                wake_word="evie",
                confidence=0.0,
                device_id=device_id,
                stage="low_power",
                power_state=self.power_state,
                details={
                    "engine": self.name,
                    "error": f"{type(exc).__name__}: {exc}",
                    "sample_rate": sample_rate,
                },
            )
        return WakeDetectionLike(
            triggered=detection.triggered,
            wake_word=detection.wake_word,
            confidence=detection.confidence,
            device_id=device_id,
            stage=detection.stage,
            power_state=detection.power_state,
            details=dict(detection.details or {}) | {"engine": self.name},
        )
