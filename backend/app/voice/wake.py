"""Wake word engines.

Production intent is an ultra-low-power always-on front end (Sensory/AON1100
class) that wakes a burst processor only on a positive "EVIE" hit. The dev
engines here implement the same contract deterministically so the lifecycle,
privacy, and security logic is fully testable without hardware.
"""

from __future__ import annotations

import asyncio
import re
import wave

from app.config import settings
from app.voice.contracts import WakeDetection, WakeWordEngine


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


class PhraseWakeEngine:
    """Deterministic wake engine for dev/test and on-device text hints.

    A real engine receives raw audio frames; this one accepts an optional
    ``text_hint`` (e.g. a lightweight on-device phrase hypothesis) or raw frames
    containing the wake phrase bytes, and reports the multi-stage power intent.
    """

    name = "phrase"
    power_state = "low_power"

    WAKE_PHRASES = ("evie", "hey evie", "ok evie", "evie wake", "evie wake up", "evi")
    WAKE_TOKEN = re.compile(r"\bevi(?:e|)?\b", re.IGNORECASE)

    async def detect(
        self,
        *,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
        device_id: str | None = None,
        frames: bytes | None = None,
        text_hint: str | None = None,
    ) -> WakeDetection:
        if text_hint is not None:
            normalized = normalize(text_hint)
            triggered = normalized in self.WAKE_PHRASES or bool(self.WAKE_TOKEN.search(normalized))
            confidence = 0.98 if normalized in self.WAKE_PHRASES else 0.55
        elif frames is not None:
            lowered = frames.lower()
            triggered = b"evie" in lowered or b"evi " in lowered
            confidence = 0.9 if triggered else 0.0
        else:
            triggered = False
            confidence = 0.0
        return WakeDetection(
            triggered=triggered,
            wake_word="evie",
            confidence=confidence,
            device_id=device_id,
            stage="low_power" if not triggered else "burst",
            power_state="low_power",
            details={"engine": self.name, "sample_rate": sample_rate, "audio_ref": audio_ref},
        )


class MultiStageWakeEngine:
    """Composes an always-on front end with a burst classifier.

    The front end runs continuously in a low-power state; only a positive front
    end triggers the burst stage, which is where heavier ASR-grade models run.
    """

    name = "multi-stage"
    power_state = "low_power"

    def __init__(self, front_end: WakeWordEngine, burst: WakeWordEngine) -> None:
        self.front_end = front_end
        self.burst = burst

    async def detect(
        self,
        *,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
        device_id: str | None = None,
        frames: bytes | None = None,
        text_hint: str | None = None,
    ) -> WakeDetection:
        low = await self.front_end.detect(
            audio_ref=audio_ref,
            sample_rate=sample_rate,
            device_id=device_id,
            frames=frames,
            text_hint=text_hint,
        )
        if not low.triggered:
            return low
        burst = await self.burst.detect(
            audio_ref=audio_ref,
            sample_rate=sample_rate,
            device_id=device_id,
            frames=frames,
            text_hint=text_hint,
        )
        return WakeDetection(
            triggered=burst.triggered,
            wake_word=burst.wake_word,
            confidence=burst.confidence,
            device_id=device_id,
            stage="burst",
            power_state="burst",
            details={"front_end": low.details, "burst": burst.details},
        )


def _wav_pcm16(audio_ref: str, sample_rate: int) -> bytes:
    """Read a local WAV file and return 16-bit mono PCM at the target rate."""
    import array

    with wave.open(audio_ref, "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError("Wake audio must be 16-bit PCM WAV")
    samples = array.array("h", frames)
    if channels > 1:
        samples = array.array(
            "h",
            (
                sum(samples[i : i + channels]) // channels
                for i in range(0, len(samples), channels)
            ),
        )
    if rate != sample_rate:
        # Simple linear resample is out of scope here; Porcupine requires 16 kHz.
        raise ValueError(f"Wake audio must be {sample_rate} Hz, got {rate}")
    return samples.tobytes()


def _pcm_bytes(*, frames: bytes | None, audio_ref: str | None, sample_rate: int) -> bytes:
    if frames is not None:
        return frames
    if audio_ref is not None:
        return _wav_pcm16(audio_ref, sample_rate)
    raise ValueError("Wake engine requires 'frames' or a local 'audio_ref'")


class PorcupineWakeEngine:
    """Picovoice Porcupine wake engine for a custom "EVIE" model.

    Porcupine consumes 16 kHz mono 16-bit PCM in fixed-size frames. The
    ``pvporcupine`` import is lazy and the create call can be injected for
    tests, so offline CI stays green without the binary library or access key.
    """

    name = "porcupine"
    power_state = "low_power"

    def __init__(
        self,
        *,
        access_key: str,
        model_path: str | None = None,
        sensitivity: float = 0.6,
        library_path: str | None = None,
        porcupine_factory=None,
        frame_length: int = 512,
    ) -> None:
        self.access_key = access_key
        self.model_path = model_path
        self.sensitivity = sensitivity
        self.library_path = library_path
        self._factory = porcupine_factory
        self.frame_length = frame_length
        self._porcupine = None

    def _create(self):
        if self._porcupine is not None:
            return self._porcupine
        if self._factory is not None:
            self._porcupine = self._factory(
                access_key=self.access_key,
                keyword_paths=[self.model_path] if self.model_path else None,
                sensitivities=[self.sensitivity],
                library_path=self.library_path,
            )
            return self._porcupine
        if not self.access_key:
            raise RuntimeError("EV_VOICE_WAKE_ACCESS_KEY is required for porcupine")
        if not self.model_path:
            raise RuntimeError("EV_VOICE_WAKE_MODEL_PATH is required for the EVIE model")
        try:
            import pvporcupine
        except ImportError as exc:
            raise RuntimeError(
                "pvporcupine is not installed; run: uv pip install pvporcupine"
            ) from exc
        self._porcupine = pvporcupine.create(
            access_key=self.access_key,
            keyword_paths=[self.model_path],
            sensitivities=[self.sensitivity],
            library_path=self.library_path,
        )
        return self._porcupine

    def _scan_sync(self, pcm: bytes) -> int:
        porcupine = self._create()
        frame_bytes = self.frame_length * 2
        for offset in range(0, max(0, len(pcm) - frame_bytes + 1), frame_bytes):
            keyword_index = porcupine.process(pcm[offset : offset + frame_bytes])
            if keyword_index >= 0:
                return int(keyword_index)
        return -1

    async def detect(
        self,
        *,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
        device_id: str | None = None,
        frames: bytes | None = None,
        text_hint: str | None = None,
    ) -> WakeDetection:
        # A real engine never delegates to the string matcher when a text hint
        # is present: text hints are dev/test conveniences and must not gate a
        # production wake path (docs/FLEET_LAW.md §8).
        pcm = _pcm_bytes(frames=frames, audio_ref=audio_ref, sample_rate=sample_rate)
        keyword_index = await asyncio.to_thread(self._scan_sync, pcm)
        triggered = keyword_index >= 0
        return WakeDetection(
            triggered=triggered,
            wake_word="evie",
            confidence=0.98 if triggered else 0.0,
            device_id=device_id,
            stage="low_power" if not triggered else "burst",
            power_state="low_power",
            details={
                "engine": self.name,
                "keyword_index": keyword_index if triggered else None,
                "sensitivity": self.sensitivity,
                "sample_rate": sample_rate,
                "audio_ref": audio_ref,
                "text_hint_present": text_hint is not None,
            },
        )


class OpenWakeWordEngine:
    """Custom "EVIE" openWakeWord head (ONNX) with an optional speaker verifier.

    This wraps the documented openWakeWord inference API:

    * base head: ``openwakeword.Model(wakeword_models=[model_path])``
    * custom verifier: ``openwakeword.Model(
        custom_verifier_models={"evie": verifier_path},
        custom_verifier_threshold=...)`` — the logistic regression trained on
        the human's own wake clips (docs: custom_verifier_models.md).

    Scores are real model outputs; the runtime threshold (default 0.5) is
    tuned against the human's ambient recording by
    ``clients/ears/train/tune_threshold.py``.
    """

    name = "openwakeword"
    power_state = "low_power"

    def __init__(
        self,
        *,
        model_path: str | None = None,
        threshold: float = 0.5,
        verifier_path: str | None = None,
        verifier_threshold: float = 0.3,
        model_factory=None,
        chunk_samples: int = 1280,
        arbiter_name: str = "wake-evie-porcupine",
    ) -> None:
        self.model_path = model_path
        self.threshold = threshold
        self.verifier_path = verifier_path
        self.verifier_threshold = verifier_threshold
        self._model_factory = model_factory
        self.chunk_samples = chunk_samples
        self.arbiter_name = arbiter_name
        self._model = None
        self._loaded = False

    def _load_model(self):
        if self._loaded:
            return self._model
        if self._model_factory is not None:
            kwargs = {"wakeword_models": [self.model_path]}
            if self.verifier_path:
                kwargs["custom_verifier_models"] = {"evie": self.verifier_path}
                kwargs["custom_verifier_threshold"] = self.verifier_threshold
            self._model = self._model_factory(**kwargs)
            self._loaded = True
            return self._model
        if not self.model_path:
            raise RuntimeError(
                "openWakeWord model not configured; set "
                "EV_VOICE_WAKE_OPENWAKEWORD_MODEL_PATH"
            )
        from app.audio.models import acquire_model

        with acquire_model(self.arbiter_name):
            try:
                from openwakeword import Model
            except ImportError as exc:
                raise RuntimeError(
                    "openwakeword is not installed (Agent 2 dependency "
                    "request); add it to use the custom EVIE head"
                ) from exc
            kwargs: dict = {"wakeword_models": [self.model_path]}
            if self.verifier_path:
                kwargs["custom_verifier_models"] = {"evie": self.verifier_path}
                kwargs["custom_verifier_threshold"] = self.verifier_threshold
            self._model = Model(**kwargs)
        self._loaded = True
        return self._model

    def _score_sync(self, pcm: bytes) -> tuple[float, dict]:
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError(
                "numpy is required for the openWakeWord engine; install the "
                "ml extra (Agent 2 dependency request)"
            ) from exc

        import array

        model = self._load_model()
        samples = array.array("h", pcm)
        best_prob = 0.0
        chunks_scored = 0
        for start in range(0, len(samples), self.chunk_samples):
            chunk = samples[start : start + self.chunk_samples]
            if len(chunk) < self.chunk_samples:
                chunk = array.array("h", chunk) + array.array(
                    "h", [0] * (self.chunk_samples - len(chunk))
                )
            tensor = np.asarray(chunk, dtype=np.int16).reshape(-1)
            scores = model.predict(tensor)
            if not scores:
                continue
            prob = max(float(v) for v in scores.values())
            chunks_scored += 1
            if prob > best_prob:
                best_prob = prob
        return best_prob, {
            "engine": self.name,
            "threshold": self.threshold,
            "score": round(best_prob, 4),
            "chunks_scored": max(1, chunks_scored),
            "verifier_enabled": self.verifier_path is not None,
        }

    async def detect(
        self,
        *,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
        device_id: str | None = None,
        frames: bytes | None = None,
        text_hint: str | None = None,
    ) -> WakeDetection:
        # Real engine: text hints are never used to trigger.
        pcm = _pcm_bytes(frames=frames, audio_ref=audio_ref, sample_rate=sample_rate)
        confidence, details = await asyncio.to_thread(self._score_sync, pcm)
        triggered = confidence >= self.threshold
        return WakeDetection(
            triggered=triggered,
            wake_word="evie",
            confidence=round(confidence, 4) if triggered else 0.0,
            device_id=device_id,
            stage="low_power" if not triggered else "burst",
            power_state="low_power",
            details={
                **details,
                "text_hint_present": text_hint is not None,
                "sample_rate": sample_rate,
                "audio_ref": audio_ref,
            },
        )


class SileroVadWakeEngine:
    """Local keyword spotting gated by a Silero VAD speech check.

    The base engine (Porcupine or the deterministic phrase engine) decides the
    keyword; Silero VAD then confirms the trigger came from live speech rather
    than a long non-speech match. Model loading is lazy and injectable, so CI
    without the model simply uses the test-safe fallback via the factory.
    """

    name = "silero-vad"
    power_state = "low_power"

    def __init__(
        self,
        base: WakeWordEngine,
        *,
        vad_model_path: str | None = None,
        threshold: float = 0.5,
        vad_factory=None,
        probability_fn=None,
        sample_rate: int = 16000,
    ) -> None:
        self.base = base
        self.vad_model_path = vad_model_path
        self.threshold = threshold
        self._vad_factory = vad_factory
        self._probability_fn = probability_fn
        self.sample_rate = sample_rate
        self._vad = None

    def _load_vad(self):
        if self._vad is not None:
            return self._vad
        if self._vad_factory is not None:
            self._vad = self._vad_factory()
            return self._vad
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("Silero VAD requires torch; install torch") from exc
        if self.vad_model_path:
            self._vad = torch.jit.load(self.vad_model_path)
        else:
            try:
                self._vad, _ = torch.hub.load(
                    repo_or_dir="snakers4/silero-vad", model="silero_vad"
                )
            except Exception as exc:  # network/hub failures
                raise RuntimeError(
                    "Silero VAD model unavailable; set EV_VOICE_WAKE_VAD_MODEL_PATH"
                ) from exc
        return self._vad

    def _speech_probability_sync(self, pcm: bytes) -> float:
        if self._probability_fn is not None:
            return float(self._probability_fn(pcm))
        import array

        import torch

        vad = self._load_vad()
        samples = array.array("h", pcm)
        if len(samples) == 0:
            return 0.0
        tensor = torch.frombuffer(samples, dtype=torch.int16).float() / 32768.0
        probabilities: list[float] = []
        with torch.no_grad():
            for offset in range(0, len(tensor) - 511, 512):
                probability = vad(tensor[offset : offset + 512], self.sample_rate)
                probabilities.append(float(probability))
        return sum(probabilities) / len(probabilities) if probabilities else 0.0

    async def detect(
        self,
        *,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
        device_id: str | None = None,
        frames: bytes | None = None,
        text_hint: str | None = None,
    ) -> WakeDetection:
        detection = await self.base.detect(
            audio_ref=audio_ref,
            sample_rate=sample_rate,
            device_id=device_id,
            frames=frames,
            text_hint=text_hint,
        )
        if not detection.triggered or text_hint is not None:
            return detection
        try:
            pcm = _pcm_bytes(frames=frames, audio_ref=audio_ref, sample_rate=sample_rate)
            probability = await asyncio.to_thread(self._speech_probability_sync, pcm)
        except (ValueError, RuntimeError) as exc:
            return WakeDetection(
                triggered=False,
                wake_word=detection.wake_word,
                confidence=0.0,
                device_id=device_id,
                stage="low_power",
                power_state="low_power",
                details={"engine": self.name, "vad_error": str(exc), **detection.details},
            )
        if probability < self.threshold:
            return WakeDetection(
                triggered=False,
                wake_word=detection.wake_word,
                confidence=0.0,
                device_id=device_id,
                stage="low_power",
                power_state="low_power",
                details={
                    "engine": self.name,
                    "vad_rejected": True,
                    "speech_probability": round(probability, 4),
                    **detection.details,
                },
            )
        return WakeDetection(
            triggered=True,
            wake_word=detection.wake_word,
            confidence=detection.confidence,
            device_id=device_id,
            stage=detection.stage,
            power_state=detection.power_state,
            details={
                "engine": self.name,
                "speech_probability": round(probability, 4),
                **detection.details,
            },
        )


def default_wake_engine() -> WakeWordEngine:
    """Config-driven wake engine selection.

    Returns a test-safe deterministic engine whenever Porcupine credentials /
    model or a Silero model are not configured.
    """
    fallback = MultiStageWakeEngine(PhraseWakeEngine(), PhraseWakeEngine())
    provider = settings.voice_wake_provider
    if provider == "porcupine":
        if not (settings.voice_wake_access_key and settings.voice_wake_model_path):
            return fallback
        return PorcupineWakeEngine(
            access_key=settings.voice_wake_access_key,
            model_path=settings.voice_wake_model_path,
            sensitivity=settings.voice_wake_sensitivity,
            library_path=settings.voice_wake_porcupine_library_path,
        )
    if provider == "silero_vad":
        if settings.voice_wake_access_key and settings.voice_wake_model_path:
            base: WakeWordEngine = PorcupineWakeEngine(
                access_key=settings.voice_wake_access_key,
                model_path=settings.voice_wake_model_path,
                sensitivity=settings.voice_wake_sensitivity,
                library_path=settings.voice_wake_porcupine_library_path,
            )
        else:
            base = PhraseWakeEngine()
        if not settings.voice_wake_vad_model_path:
            return fallback
        return SileroVadWakeEngine(
            base,
            vad_model_path=settings.voice_wake_vad_model_path,
            threshold=settings.voice_wake_vad_threshold,
        )
    if provider == "openwakeword":
        if not settings.voice_wake_openwakeword_model_path:
            return fallback
        return OpenWakeWordEngine(
            model_path=settings.voice_wake_openwakeword_model_path,
            verifier_path=settings.voice_wake_openwakeword_verifier_path,
            threshold=settings.voice_wake_openwakeword_threshold,
            verifier_threshold=settings.voice_wake_openwakeword_verifier_threshold,
        )
    return fallback


def set_default_wake_engine(engine: WakeWordEngine | None) -> None:
    """Override the process-wide engine (ears process, tests)."""

    global _default_override
    _default_override = engine


_default_override: WakeWordEngine | None = None


def configured_wake_engine() -> WakeWordEngine:
    """Return the override if set, else the config-driven default."""

    return _default_override if _default_override is not None else default_wake_engine()
