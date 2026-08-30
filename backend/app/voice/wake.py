"""Wake word engines.

Production intent is an ultra-low-power always-on front end (Sensory/AON1100
class) that wakes a burst processor only on a positive "EVIE" hit. The dev
engines here implement the same contract deterministically so the lifecycle,
privacy, and security logic is fully testable without hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import wave
from pathlib import Path

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

    WAKE_PHRASES = (
        "evie",
        "hey evie",
        "hi evie",
        "hello evie",
        "ok evie",
        "okay evie",
        "evie wake",
        "evie wake up",
        "evie here",
        "hey evie here",
        "hi evie here",
        "hello evie here",
        "ok evie here",
        "okay evie here",
        "evi",
        "eve",
        "hey eve",
        "hi eve",
        "hello eve",
        "ok eve",
        "okay eve",
        "eve here",
        "hey eve here",
    )
    WAKE_TOKEN = re.compile(r"\b(?:evie+|eevee|evi|eve)\b", re.IGNORECASE)

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
        if self.model_path:
            self.model_path = str(Path(self.model_path).expanduser())
        if self.verifier_path:
            self.verifier_path = str(Path(self.verifier_path).expanduser())
        self.verifier_threshold = verifier_threshold
        self._model_factory = model_factory
        self.chunk_samples = chunk_samples
        self.arbiter_name = arbiter_name
        self._model = None
        self._loaded = False

    def _load_model(self):
        if self._loaded:
            return self._model
        # Derive verifier key from model file stem (e.g., wake-openwakeword) so it matches Model's internal name.
        # The old hardcoded "evie" only works when the model file is named evie.onnx; the canonical path is wake-openwakeword.onnx.
        def _verifier_key() -> str:
            if not self.model_path:
                return "evie"
            return Path(self.model_path).stem

        if self._model_factory is not None:
            kwargs = {"wakeword_models": [self.model_path]}
            if self.verifier_path:
                kwargs["custom_verifier_models"] = {_verifier_key(): self.verifier_path}
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
                kwargs["custom_verifier_models"] = {_verifier_key(): self.verifier_path}
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


class WhisperPhraseWakeEngine:
    """Siri-style strict wake spotter — ONLY the owner's name activates EVIE.

    The always-on wake must be activated by saying the name itself ("Eve",
    "Evie"), exactly like Siri responds only to its name. Acoustically-near
    words that look/sound similar — "every", "even", "evil", "Stevie" — are
    NEVER wake candidates, with or without speech evidence.

    The name must appear at the head of the clip as a whole word (word
    boundary), so a conversational mention ("I think Evie is...") produces a
    wake *candidate* that the later directed-speech gate rejects, but it is
    never elevated to a full acceptance on its own. Whisper transcribes the
    local VAD segment (token-free, on-device) and the transcript is classified
    here. Bare silence hallucinations (no_speech_prob above result) never wake.
    """

    name = "whisper-phrase"
    power_state = "burst"
    WAKE_PHRASES = PhraseWakeEngine.WAKE_PHRASES
    # Siri-style: the name may optionally be preceded by a greeting, but the
    # wake token itself must be the name. The word-boundary lookahead rejects
    # "every"/"even"/"evil"/"Stevie" because the char after the name is not a
    # boundary — e.g. "eve"+"r" in "every" never matches.
    STRONG_HEAD = re.compile(
        r"^(?:hey |ok |okay |hi |hello )?"
        r"(?:eve|evie|ee vee|eevee|evy)"
        r"(?: here)?(?=[\s,!.?'-]|$)",
        re.IGNORECASE,
    )
    # Whole-clip name (with optional trailing "here") is also a strong hit.
    NAME_FULL = re.compile(
        r"^(?:hey |ok |okay |hi |hello )?"
        r"(?:eve|evie|ee vee|eevee|evy)(?: here)?$",
        re.IGNORECASE,
    )
    # Keep the historical names so tests and lifecycle still import them.
    STRONG_TOKEN = STRONG_HEAD
    WAKE_TOKEN = STRONG_HEAD
    HEAD_WAKE = STRONG_HEAD
    WEAK_HEAD = STRONG_HEAD
    WEAK_TOKEN = STRONG_HEAD
    # Never-wake confusable words (whole clip or anywhere): these are NOT the
    # owner addressing Evie and must never produce a candidate, even if a
    # substring happens to look like the name. Transcripts are normalized to
    # lower-case, so the set is lower-case too.
    CONFUSABLE = frozenset(
        {"every", "even", "evil", "evolve", "event", "everything", "everyone", "stevie"}
    )
    # Whisper's own no-speech gate: a segment with no_speech_prob above this is
    # a silence hallucination, not the owner saying the name.
    NO_SPEECH_CAP = 0.6

    def __init__(self, *, transcriber=None) -> None:
        self._transcriber = transcriber
        self._warmed = False

    async def warmup(self) -> None:
        """Preload the spotter so the first spoken EVIE is not a cold load.

        A fresh faster-whisper load is multi-second. Repeating "Evie" during
        that window reads as a missed wake. Warm once at process start.
        """

        if self._warmed:
            return
        transcriber = self._transcriber_or_default()
        load = getattr(transcriber, "_load_model", None)
        if load is None:
            self._warmed = True
            return

        def _load() -> None:
            model = load()
            if model is None:
                return
            import tempfile
            import wave

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                path = handle.name
            try:
                with wave.open(path, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(b"\x00\x00" * 8000)
                transcribe = getattr(model, "transcribe", None)
                if transcribe is None:
                    return
                list(
                    transcribe(
                        path,
                        language="en",
                        beam_size=1,
                        temperature=0.0,
                        vad_filter=False,
                    )
                )
            except Exception:  # noqa: BLE001 - warmup is best-effort
                return
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(path)

        await asyncio.to_thread(_load)
        self._warmed = True

    def _transcriber_or_default(self):
        if self._transcriber is not None:
            return self._transcriber
        from app.voice.asr import FasterWhisperTranscriber, get_transcriber

        try:
            shared = get_transcriber()
        except Exception:
            shared = None
        if isinstance(shared, FasterWhisperTranscriber):
            self._transcriber = shared
            return shared
        self._transcriber = FasterWhisperTranscriber(
            model=os.environ.get("EV_VOICE_WAKE_ASR_MODEL")
            or _wake_asr_model(),
            vad_filter=False,
        )
        return self._transcriber

    async def detect(
        self,
        *,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
        device_id: str | None = None,
        frames: bytes | None = None,
        text_hint: str | None = None,
    ) -> WakeDetection:
        if text_hint is not None and audio_ref is None and frames is None:
            normalized = normalize(text_hint)
            strong, weak = self._classify_transcript(normalized)
            # Siri-style strictness: a bare text hint wakes ONLY if it is the
            # owner's name at the head. No weak aliases, no confusables.
            triggered = strong
            return WakeDetection(
                triggered=triggered,
                wake_word="evie",
                confidence=0.98 if triggered else 0.0,
                device_id=device_id,
                stage="burst" if triggered else "low_power",
                power_state=self.power_state,
                details={"engine": self.name, "source": "text_hint"},
            )
        transcript = ""
        try:
            transcriber = self._transcriber_or_default()
            audio_b64 = None
            padded = frames
            if padded is not None:
                from app.audio.capture import pcm_to_wav_bytes

                padded = _pad_pcm16(padded, sample_rate, min_seconds=1.5)
                wav = pcm_to_wav_bytes(padded, sample_rate)
                import base64

                audio_b64 = base64.b64encode(wav).decode("ascii")
            transcribe = transcriber.transcribe
            try:
                result = await transcribe(
                    audio_b64=audio_b64,
                    audio_ref=audio_ref,
                    language="en",
                    wake_mode=True,
                )
            except TypeError:
                result = await transcribe(
                    audio_b64=audio_b64,
                    audio_ref=audio_ref,
                    language="en",
                )
            transcript = getattr(result, "text", "") or ""
        except Exception as exc:  # noqa: BLE001 - wake must not crash ears/API
            return WakeDetection(
                triggered=False,
                wake_word="evie",
                confidence=0.0,
                device_id=device_id,
                stage="low_power",
                power_state=self.power_state,
                details={"engine": self.name, "error": f"{type(exc).__name__}: {exc}"},
            )
        normalized = normalize(transcript)
        strong, weak = self._classify_transcript(normalized)
        # Strict Siri-style: only the name at the head AND confirmed real speech
        # wake. Weak aliases and confusables never wake; a name without speech
        # evidence (or with confirmed silence) never wakes. Real faster-whisper
        # wake-mode transcription always reports no_speech_prob, so this never
        # blocks a genuine owner wake while making silence hallucinations and
        # unknown-source transcripts cost nothing.
        triggered = strong and self._real_speech(result)
        degraded = bool(getattr(result, "degraded", False))
        details = {
            "engine": self.name,
            "transcript": transcript,
            "no_speech_prob": getattr(result, "details", {}).get("no_speech_prob"),
            "weak_alias": False,
            "sample_rate": sample_rate,
            "degraded": degraded,
        }
        if degraded:
            details["error"] = str(
                getattr(result, "details", {}).get("reason")
                or "wake ASR is degraded"
            )
        return WakeDetection(
            triggered=triggered,
            wake_word="evie",
            confidence=0.9 if triggered else 0.0,
            device_id=device_id,
            stage="burst" if triggered else "low_power",
            power_state=self.power_state,
            details=details,
        )

    @classmethod
    def _classify_transcript(cls, normalized: str) -> tuple[bool, bool]:
        """Siri-style: (strong_hit, weak_head_hit) where only the NAME hits.

        The name must be a whole word at the head of the clip (word boundary),
        optionally preceded by a greeting. Confusable words that are NOT the
        owner's name — every/even/evil/Stevie — never wake, even with real
        speech. The second return is always False (no weak aliases); it is
        kept for API compatibility.
        """

        if not normalized:
            return False, False
        # Siri-style: a confusable word at the WAKE POSITION (the first word,
        # optionally after a greeting) is never the owner's name. Words like
        # "every"/"even" later in the utterance (e.g. "Evie, even that") do not
        # block a name-headed wake — recall is preserved.
        head_token = normalized.split(None, 1)[0].strip(".,!?'\"-")
        if cls.CONFUSABLE and head_token in cls.CONFUSABLE:
            return False, False
        # Whole-clip name or greeting+name at the head (with boundary). This
        # covers "eve", "evie", "ee vee", "hey eve", "eve what's the weather",
        # and NEVER "every"/"even"/"Stevie".
        if cls.NAME_FULL.match(normalized):
            return True, False
        head = " ".join(normalized.split()[:6])
        if cls.STRONG_HEAD.match(head) or cls.STRONG_HEAD.match(normalized):
            return True, False
        return False, False

    @staticmethod
    def _real_speech(result) -> bool:
        """True when the transcribed clip is real speech, not a silence hallucination.

        Bare weak aliases ("Eve"/"evil") only wake when the model's own
        no_speech_prob says the clip contained speech. When the transcriber
        did not report a no_speech_prob, a weak alias stays non-triggering so
        unknown-source transcripts cannot false-wake the system.
        """

        no_speech_prob = getattr(result, "details", {}).get("no_speech_prob")
        if no_speech_prob is None:
            return False
        return float(no_speech_prob) <= WhisperPhraseWakeEngine.NO_SPEECH_CAP


def _wake_asr_model() -> str:
    model = (settings.voice_asr_model or "base").strip()
    if model in {"whisper-1", "gpt-4o-mini-transcribe", ""}:
        return "base"
    return model


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
_whisper_phrase_wake: WhisperPhraseWakeEngine | None = None


def configured_wake_engine() -> WakeWordEngine:
    """Return the override if set, else the config-driven default.

    When openWakeWord is selected but the custom EVIE ONNX is not on disk,
    fall through to the faster-whisper phrase spotter so saying "EVIE"
    actually works instead of crashing the ears/API loop.
    """

    global _whisper_phrase_wake
    if _default_override is not None:
        return _default_override
    engine = default_wake_engine()
    # Spoken EVIE never contains the ASCII bytes ``evie``. Phrase matching
    # and a missing openWakeWord ONNX head must not be the live detector —
    # fall through to the ASR spotter so stock config (echo ASR + phrase
    # wake) actually hears the name.
    if engine.name == "openwakeword":
        path = getattr(engine, "model_path", None)
        if path and Path(str(path)).expanduser().is_file():
            return engine
        if _whisper_phrase_wake is None:
            _whisper_phrase_wake = WhisperPhraseWakeEngine()
        return _whisper_phrase_wake
    if engine.name == "multi-stage":
        if _whisper_phrase_wake is None:
            _whisper_phrase_wake = WhisperPhraseWakeEngine()
        return _whisper_phrase_wake
    return engine


def _pad_pcm16(frames: bytes, sample_rate: int, *, min_seconds: float = 1.5) -> bytes:
    """Pad short VAD clips so faster-whisper does not treat them as no-speech."""

    import array

    samples = array.array("h")
    even = frames[: len(frames) - (len(frames) % 2)]
    samples.frombytes(even)
    need = int(min_seconds * sample_rate)
    if len(samples) < need:
        samples.extend([0] * (need - len(samples)))
    return samples.tobytes()
