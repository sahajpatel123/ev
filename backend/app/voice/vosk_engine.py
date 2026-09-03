"""Real always-on wake spotting and streaming ASR on the Vosk (Kaldi) runtime.

Why two recognizers over one shared acoustic model:

* **Wake stage** — a *grammar-restricted* recognizer whose language model only
  contains the EVIE wake phrases plus the ``[unk]`` sink. A custom name is
  hopeless for a full-vocabulary decoder ("Evie" decodes as "eddie", "heavy",
  "of he"), but the restricted graph spots it reliably and costs ~2% of one CPU
  core, so it can run continuously like Siri's always-on front end.
* **Command stage** — the same model with its full vocabulary, used only after a
  wake hit, to transcribe what the human actually asked.

Both stages share one ``vosk.Model`` instance (one copy of the weights in RSS,
reserved once through the ModelArbiter).

The Vosk import and the model directory are both optional: when either is
missing every entry point reports *why* through :func:`vosk_status` instead of
pretending to listen. Nothing here fabricates a detection or a transcript.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import settings
from app.voice.contracts import ModelUnavailableError, WakeDetection, acquire_model

LOGGER = logging.getLogger("ev.voice.vosk")

# Registry name shared by the wake and command stages (one resident copy).
MODEL_REGISTRY_NAME = "asr-vosk-small-en-us"
DEFAULT_MODEL_DIRNAME = "vosk-model-small-en-us-0.15"

# Phrases the always-on stage listens for. Every word must exist in the model
# lexicon; "evie" does in the small en-US model. ``[unk]`` is the sink that lets
# the decoder say "that was not a wake phrase" instead of forcing a match.
DEFAULT_WAKE_PHRASES: tuple[str, ...] = (
    "evie",
    "hey evie",
    "hi evie",
    "hello evie",
    # "okay evie" only — "ok evie" is a homophone in the same grammar and
    # splits the posterior so neither phrase clears the confirmation threshold.
    "okay evie",
    "yo evie",
)
UNKNOWN_TOKEN = "[unk]"

_WAKE_TOKEN = re.compile(r"\bevie\b")
_MODEL_CACHE: dict[str, Any] = {}
_MODEL_LOCK = threading.Lock()

SETUP_HINT = (
    "Install the speech runtime and the EVIE wake model: "
    "`uv sync --extra voice` then `uv run python -m app.voice.models_setup`"
)


# --------------------------------------------------------------------------- #
# Availability / diagnostics
# --------------------------------------------------------------------------- #


def default_model_path() -> str:
    """Configured model directory, else the standard ``~/.ev/models`` location."""

    configured = settings.voice_vosk_model_path
    if configured:
        return str(Path(configured).expanduser())
    return str(Path.home() / ".ev" / "models" / DEFAULT_MODEL_DIRNAME)


def _module_available() -> bool:
    try:
        import vosk  # noqa: F401
    except ImportError:
        return False
    return True


def model_installed(path: str | None = None) -> bool:
    """True when a usable Vosk model directory exists at ``path``."""

    directory = Path(path or default_model_path()).expanduser()
    # A Vosk model is a directory; `am` (acoustic model) is present in every
    # layout, `graph` in the small/HCLG ones.
    return directory.is_dir() and (directory / "am").is_dir()


def vosk_available(path: str | None = None) -> bool:
    return _module_available() and model_installed(path)


def vosk_status(path: str | None = None) -> dict:
    """Machine-readable readiness report for the diagnostics endpoint."""

    resolved = path or default_model_path()
    module = _module_available()
    installed = model_installed(resolved)
    if module and installed:
        detail = "ready"
    elif not module and not installed:
        detail = f"vosk runtime and model missing. {SETUP_HINT}"
    elif not module:
        detail = "vosk runtime missing: `uv sync --extra voice`"
    else:
        detail = (
            f"model directory not found at {resolved}. "
            "Download it with `uv run python -m app.voice.models_setup`"
        )
    return {
        "engine": "vosk",
        "ready": module and installed,
        "runtime_installed": module,
        "model_installed": installed,
        "model_path": resolved,
        "detail": detail,
    }


def _require(path: str | None = None) -> str:
    resolved = path or default_model_path()
    if not _module_available():
        raise ModelUnavailableError(
            f"the vosk speech runtime is not installed. {SETUP_HINT}"
        )
    if not model_installed(resolved):
        raise ModelUnavailableError(
            f"no Vosk model directory at {resolved}. {SETUP_HINT}"
        )
    return resolved


def load_model(path: str | None = None) -> Any:
    """Load (and cache) the shared acoustic model under the ModelArbiter."""

    resolved = _require(path)
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(resolved)
        if cached is not None:
            return cached
        import vosk

        vosk.SetLogLevel(-1)
        with acquire_model(MODEL_REGISTRY_NAME):
            model = vosk.Model(resolved)
        _MODEL_CACHE[resolved] = model
        LOGGER.info("vosk model loaded from %s", resolved)
        return model


def reset_model_cache() -> None:
    """Drop cached models (tests, model swaps)."""

    with _MODEL_LOCK:
        _MODEL_CACHE.clear()


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9\[\] ]+", "", (text or "").lower()).strip()


# --------------------------------------------------------------------------- #
# Wake stage
# --------------------------------------------------------------------------- #


@dataclass
class WakeSignal:
    """One wake-stage decision.

    ``kind`` is:

    * ``pending`` — the wake phrase appeared in a partial hypothesis. Good
      enough to light up the UI and start capturing, never enough to act on.
    * ``confirmed`` — the decoder closed the segment and the phrase survived
      with word confidence at or above threshold.
    * ``rejected`` — a pending hit did not survive segment finalization, so the
      captured audio must be discarded.
    """

    kind: str
    phrase: str = ""
    confidence: float = 0.0
    end_offset: int = 0  # stream sample offset just after the wake phrase
    text: str = ""

    @property
    def triggered(self) -> bool:
        return self.kind == "confirmed"


def _wake_phrase_in(
    text: str, phrases: tuple[str, ...] | list[str] = DEFAULT_WAKE_PHRASES
) -> str:
    """Return the longest configured phrase contained in ``text``, else ""."""

    normalized = normalize(text)
    if not _WAKE_TOKEN.search(normalized):
        return ""
    for phrase in sorted(phrases, key=len, reverse=True):
        if phrase in normalized:
            return phrase
    return "evie"


class VoskWakeSpotter:
    """Streaming grammar-restricted spotter for the EVIE wake phrase.

    Feed mono 16-bit PCM at ``sample_rate``; every call returns the signals that
    the audio produced (usually none). The spotter is stateful and tracks the
    absolute stream offset so callers can slice the command audio that follows
    the wake phrase.
    """

    name = "vosk-wake"

    def __init__(
        self,
        *,
        model_path: str | None = None,
        phrases: tuple[str, ...] | list[str] | None = None,
        threshold: float | None = None,
        sample_rate: int = 16000,
        max_segment_s: float = 12.0,
        model: Any = None,
        recognizer_factory=None,
    ) -> None:
        self.sample_rate = sample_rate
        self.threshold = (
            settings.voice_wake_vosk_threshold if threshold is None else threshold
        )
        self.phrases = tuple(phrases or settings.voice_wake_phrases or DEFAULT_WAKE_PHRASES)
        self.max_segment_samples = int(max_segment_s * sample_rate)
        self._model_path = model_path
        self._model = model
        self._recognizer_factory = recognizer_factory
        self._recognizer: Any = None
        self._offset = 0
        self._segment_start = 0
        self._pending: WakeSignal | None = None

    # -- internals ------------------------------------------------------- #

    def _grammar(self) -> str:
        return json.dumps([*self.phrases, UNKNOWN_TOKEN])

    def _new_recognizer(self) -> Any:
        if self._recognizer_factory is not None:
            return self._recognizer_factory(self._grammar())
        import vosk

        model = self._model or load_model(self._model_path)
        self._model = model
        recognizer = vosk.KaldiRecognizer(model, float(self.sample_rate), self._grammar())
        recognizer.SetWords(True)
        return recognizer

    def _recognizer_or_new(self) -> Any:
        if self._recognizer is None:
            self._recognizer = self._new_recognizer()
        return self._recognizer

    def _rotate(self) -> None:
        self._recognizer = None
        self._segment_start = self._offset
        self._pending = None

    def _word_end_offset(self, payload: dict, phrase: str) -> int:
        """Absolute sample offset just after the last word of the wake phrase."""

        words = payload.get("result") or []
        target = phrase.split()[-1] if phrase else "evie"
        for word in reversed(words):
            if word.get("word") == target and word.get("end") is not None:
                return self._segment_start + int(float(word["end"]) * self.sample_rate)
        return self._offset

    def _phrase_confidence(self, payload: dict, phrase: str) -> float:
        words = payload.get("result") or []
        needed = set(phrase.split()) or {"evie"}
        scores = [
            float(word.get("conf", 0.0)) for word in words if word.get("word") in needed
        ]
        if not scores:
            # No word-level detail (older builds): trust the grammar match only
            # as far as the threshold, never higher.
            return self.threshold
        return round(min(scores), 4)

    # -- public API ------------------------------------------------------ #

    @property
    def consumed_samples(self) -> int:
        return self._offset

    def reset(self) -> None:
        self._rotate()

    def flush(self) -> list[WakeSignal]:
        """Close the open segment (end of a clip / end of capture)."""

        if self._recognizer is None:
            return []
        payload = json.loads(self._recognizer.FinalResult())
        signals = self._close_segment(payload)
        self._rotate()
        return signals

    def feed(self, pcm: bytes) -> list[WakeSignal]:
        if not pcm:
            return []
        recognizer = self._recognizer_or_new()
        signals: list[WakeSignal] = []
        finalized = recognizer.AcceptWaveform(pcm)
        self._offset += len(pcm) // 2
        if finalized:
            payload = json.loads(recognizer.Result())
            signals.extend(self._close_segment(payload))
            self._rotate()
            return signals
        if self._offset - self._segment_start >= self.max_segment_samples:
            # Bound decoder state (and latency) on a monologue with no pause.
            payload = json.loads(recognizer.FinalResult())
            signals.extend(self._close_segment(payload))
            self._rotate()
            return signals
        if self._pending is None:
            partial = json.loads(recognizer.PartialResult()).get("partial", "")
            phrase = _wake_phrase_in(partial, self.phrases)
            if phrase:
                self._pending = WakeSignal(
                    kind="pending",
                    phrase=phrase,
                    confidence=0.0,
                    end_offset=self._offset,
                    text=normalize(partial),
                )
                signals.append(self._pending)
        return signals

    def _close_segment(self, payload: dict) -> list[WakeSignal]:
        text = normalize(payload.get("text", ""))
        phrase = _wake_phrase_in(text, self.phrases)
        if phrase:
            confidence = self._phrase_confidence(payload, phrase)
            if confidence >= self.threshold:
                return [
                    WakeSignal(
                        kind="confirmed",
                        phrase=phrase,
                        confidence=confidence,
                        end_offset=self._word_end_offset(payload, phrase),
                        text=text,
                    )
                ]
            if self._pending is not None:
                return [
                    WakeSignal(
                        kind="rejected",
                        phrase=phrase,
                        confidence=confidence,
                        end_offset=self._offset,
                        text=text,
                    )
                ]
            return []
        if self._pending is not None:
            return [WakeSignal(kind="rejected", end_offset=self._offset, text=text)]
        return []


class VoskWakeEngine:
    """Batch :class:`~app.voice.contracts.WakeWordEngine` over the spotter.

    Used by the request/response wake paths (``POST /v1/voice/wake``, the ears
    process). A real engine never triggers on a text hint.
    """

    name = "vosk"
    power_state = "low_power"

    def __init__(
        self,
        *,
        model_path: str | None = None,
        phrases: tuple[str, ...] | list[str] | None = None,
        threshold: float | None = None,
        spotter_factory=None,
    ) -> None:
        self.model_path = model_path
        self.phrases = tuple(phrases) if phrases else None
        self.threshold = threshold
        self._spotter_factory = spotter_factory

    def spotter(self, sample_rate: int = 16000) -> VoskWakeSpotter:
        if self._spotter_factory is not None:
            return self._spotter_factory(sample_rate)
        return VoskWakeSpotter(
            model_path=self.model_path,
            phrases=self.phrases,
            threshold=self.threshold,
            sample_rate=sample_rate,
        )

    async def detect(
        self,
        *,
        audio_ref: str | None = None,
        sample_rate: int = 16000,
        device_id: str | None = None,
        frames: bytes | None = None,
        text_hint: str | None = None,
    ) -> WakeDetection:
        import asyncio

        from app.voice.wake import _pcm_bytes

        pcm = _pcm_bytes(frames=frames, audio_ref=audio_ref, sample_rate=sample_rate)
        try:
            signal = await asyncio.to_thread(self._scan_sync, pcm, sample_rate)
        except ModelUnavailableError as exc:
            return WakeDetection(
                triggered=False,
                wake_word="evie",
                confidence=0.0,
                device_id=device_id,
                details={"engine": self.name, "degraded": True, "reason": str(exc)},
            )
        triggered = signal is not None and signal.triggered
        return WakeDetection(
            triggered=triggered,
            wake_word="evie",
            confidence=signal.confidence if signal is not None else 0.0,
            device_id=device_id,
            stage="burst" if triggered else "low_power",
            power_state="low_power",
            details={
                "engine": self.name,
                "phrase": signal.phrase if signal is not None else None,
                "wake_end_sample": signal.end_offset if signal is not None else None,
                "heard": signal.text if signal is not None else "",
                "sample_rate": sample_rate,
                "audio_ref": audio_ref,
                "text_hint_present": text_hint is not None,
            },
        )

    def _scan_sync(self, pcm: bytes, sample_rate: int) -> WakeSignal | None:
        """Scan a complete clip; only a confirmed segment counts as a trigger."""

        spotter = self.spotter(sample_rate)
        block = int(sample_rate * 0.1) * 2
        for offset in range(0, len(pcm), block):
            for signal in spotter.feed(pcm[offset : offset + block]):
                if signal.kind == "confirmed":
                    return signal
        for signal in spotter.flush():
            if signal.kind == "confirmed":
                return signal
        return None


# --------------------------------------------------------------------------- #
# Command stage
# --------------------------------------------------------------------------- #


@dataclass
class RecognizerResult:
    text: str
    confidence: float
    words: list[dict] = field(default_factory=list)


class VoskStreamingRecognizer:
    """Full-vocabulary streaming recognizer for one utterance.

    ``feed`` returns the current partial hypothesis (or ``None`` when it has not
    changed); ``final`` closes the utterance and returns the transcript with a
    word-confidence average.
    """

    name = "vosk"

    def __init__(
        self,
        *,
        model_path: str | None = None,
        sample_rate: int = 16000,
        model: Any = None,
        recognizer=None,
    ) -> None:
        self.sample_rate = sample_rate
        self._model_path = model_path
        self._model = model
        self._recognizer = recognizer
        self._segments: list[RecognizerResult] = []
        self._partial = ""

    def _recognizer_or_new(self):
        if self._recognizer is None:
            import vosk

            model = self._model or load_model(self._model_path)
            self._model = model
            self._recognizer = vosk.KaldiRecognizer(model, float(self.sample_rate))
            self._recognizer.SetWords(True)
        return self._recognizer

    @staticmethod
    def _result(payload: dict) -> RecognizerResult:
        words = payload.get("result") or []
        scores = [float(word.get("conf", 0.0)) for word in words]
        return RecognizerResult(
            text=(payload.get("text") or "").strip(),
            confidence=round(sum(scores) / len(scores), 4) if scores else 0.0,
            words=list(words),
        )

    def feed(self, pcm: bytes) -> str | None:
        """Consume audio; return a new partial hypothesis when it changed."""

        if not pcm:
            return None
        recognizer = self._recognizer_or_new()
        if recognizer.AcceptWaveform(pcm):
            segment = self._result(json.loads(recognizer.Result()))
            if segment.text:
                self._segments.append(segment)
            self._partial = ""
            return self.hypothesis or None
        partial = (json.loads(recognizer.PartialResult()).get("partial") or "").strip()
        if partial and partial != self._partial:
            self._partial = partial
            return self.hypothesis
        return None

    @property
    def hypothesis(self) -> str:
        parts = [segment.text for segment in self._segments if segment.text]
        if self._partial:
            parts.append(self._partial)
        return " ".join(parts).strip()

    def final(self) -> RecognizerResult:
        recognizer = self._recognizer_or_new()
        tail = self._result(json.loads(recognizer.FinalResult()))
        if tail.text:
            self._segments.append(tail)
        self._partial = ""
        text = " ".join(segment.text for segment in self._segments if segment.text).strip()
        scores = [segment.confidence for segment in self._segments if segment.text]
        words: list[dict] = []
        for segment in self._segments:
            words.extend(segment.words)
        return RecognizerResult(
            text=text,
            confidence=round(sum(scores) / len(scores), 4) if scores else 0.0,
            words=words,
        )
