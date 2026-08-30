"""Voice activity detection behind a small protocol.

Two implementations share the same interface:

* ``SileroVadOnnx`` — the real v5 ONNX model (2 MB, MIT) loaded through the
  ModelArbiter; the default when a model path is configured.
* ``EnergyVad`` — RMS energy heuristic, kept as the zero-dependency offline
  double so CI and degraded runs stay deterministic.

Segmentation (pre-roll, post-roll, gap merging, minimum duration) is shared so
the two engines produce comparable utterance boundaries.
"""

from __future__ import annotations

import array
import math
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class VadFrame:
    """One decision frame (30 ms of audio)."""

    start_sample: int
    end_sample: int
    speech: bool
    probability: float


@dataclass
class VadSegment:
    """A speech utterance with pre/post roll applied (samples included)."""

    start_sample: int
    end_sample: int
    samples: array.array = field(repr=False)
    mean_probability: float = 0.0
    engine: str = ""

    @property
    def duration_s(self, sample_rate: int = 16000) -> float:
        return (self.end_sample - self.start_sample) / max(1, sample_rate)


class VadEngine(Protocol):
    name: str

    async def frame_probabilities(
        self, samples: array.array | list[int], sample_rate: int
    ) -> list[float]: ...

    async def block_probability(
        self, samples: array.array | list[int], sample_rate: int
    ) -> float | None: ...


def _frame_slice(samples, start: int, end: int) -> array.array:
    if isinstance(samples, array.array):
        return array.array("h", samples[start:end])
    return array.array("h", samples[start:end])


def _rms(samples) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(int(s) * int(s) for s in samples) / len(samples))


def _cross_chunk_diff(samples, period: int) -> float:
    """Normalized mean absolute difference between a signal and itself +``period``.

    Near 0.0 when the audio literally repeats at that lag (a stuck-mic or
    self-echo loop); higher for real speech, which is aperiodic. Comparison is
    bounded to one period of overlap so a loop needs only a single clean repeat
    to be caught.
    """

    n = len(samples)
    if period <= 0 or period > n - period:
        return 1.0
    window = min(n - period, period)
    if window <= 0:
        return 1.0
    denom = 0.0
    acc = 0.0
    for i in range(window):
        a = int(samples[i])
        b = int(samples[i + period])
        acc += abs(a - b)
        denom += abs(a) + abs(b)
    if denom <= 0:
        return 1.0
    return acc / denom


def looks_stuck_loop(
    samples,
    *,
    sample_rate: int = 16000,
    min_period_ms: float = 300.0,
    max_period_ms: float = 1500.0,
    loop_threshold: float = 0.10,
    min_seconds: float = 1.5,
) -> bool:
    """True when the segment is a repeating audio loop (stuck mic / echo).

    A stuck microphone or a playback self-loop produces audio whose content
    repeats nearly identically, so some lag L re-matches the signal with a
    normalized difference near zero. Real speech has no such lag.

    The scan is coarse-to-fine: a coarse grid (~96 lags) finds the best
    period, then a fine scan around it resolves misaligned loop lengths so a
    loop is caught even when its period falls between grid points. The check is
    deliberately conservative (only obvious loops are dropped) because a false
    positive would discard the owner's actual speech.
    """

    if not samples or len(samples) < int(min_seconds * sample_rate):
        return False
    n = len(samples)
    p0 = max(1, int(min_period_ms * sample_rate / 1000.0))
    p1 = min(int(max_period_ms * sample_rate / 1000.0), n // 2)
    if p0 > p1:
        return False
    # A literal loop always re-matches at a whole-number fraction of the
    # segment (n/2, n/3, ...): the same phrase repeated. Check those exact
    # lags first so clean repeats are caught even when the coarse grid lands
    # a step away from the true period.
    for k in range(2, max(2, n // p0) + 1):
        lag = n // k
        if lag < p0 or lag > p1:
            continue
        if _cross_chunk_diff(samples, lag) <= loop_threshold:
            return True
    best_ratio = 1.0
    best_period = p0
    coarse_step = max(1, (p1 - p0) // 96)
    for period in range(p0, p1 + 1, coarse_step):
        ratio = _cross_chunk_diff(samples, period)
        if ratio < best_ratio:
            best_ratio = ratio
            best_period = period
            if ratio <= loop_threshold:
                return True
    # The coarse grid can land a whole step off the true loop length; refine
    # only around the best coarse lag (still bounded, and early-exits).
    lo = max(p0, best_period - coarse_step)
    hi = min(p1, best_period + coarse_step)
    for period in range(lo, hi + 1):
        ratio = _cross_chunk_diff(samples, period)
        if ratio < best_ratio:
            best_ratio = ratio
            if ratio <= loop_threshold:
                return True
    return best_ratio <= loop_threshold


class EnergyVad:
    """Deterministic energy VAD double (offline default)."""

    name = "energy"

    def __init__(
        self,
        *,
        frame_ms: int = 30,
        rms_speech_floor: float = 80.0,
        zcr_floor: float = 0.008,
        speech_probability: float = 0.9,
        silence_probability: float = 0.05,
    ) -> None:
        self.frame_ms = frame_ms
        self.rms_speech_floor = rms_speech_floor
        self.zcr_floor = zcr_floor
        self.speech_probability = speech_probability
        self.silence_probability = silence_probability

    async def frame_probabilities(
        self, samples: array.array | list[int], sample_rate: int
    ) -> list[float]:
        frame_size = max(1, int(sample_rate * self.frame_ms / 1000))
        probabilities: list[float] = []
        for start in range(0, len(samples), frame_size):
            frame = _frame_slice(samples, start, start + frame_size)
            rms = _rms(frame)
            # RMS-only: voiced vowels (especially far-field "EE-vee") sit at or
            # below a 0.015 ZCR floor on 20 ms frames, so ANDing ZCR dropped them.
            speech = rms >= self.rms_speech_floor
            probabilities.append(self.speech_probability if speech else self.silence_probability)
        return probabilities

    async def block_probability(
        self, samples: array.array | list[int], sample_rate: int
    ) -> float | None:
        """One deterministic decision per pushed capture block."""

        frame = _frame_slice(samples, 0, len(samples))
        rms = _rms(frame)
        speech = rms >= self.rms_speech_floor
        return self.speech_probability if speech else self.silence_probability


class SileroVadOnnx:
    """Silero VAD v5 ONNX (16 kHz) loaded through the ModelArbiter.

    ``session_factory`` is injectable for tests; the real factory uses
    onnxruntime and the model path from ``EV_EARS_VAD_MODEL_PATH``.
    """

    name = "silero-v5-onnx"

    def __init__(
        self,
        *,
        model_path: str | None = None,
        threshold: float = 0.5,
        session_factory=None,
        sample_rate: int = 16000,
    ) -> None:
        self.model_path = model_path
        self.threshold = threshold
        self._session_factory = session_factory
        self.sample_rate = sample_rate
        self._session = None
        self._loaded = False
        self._pending: array.array = array.array("h")

    def _load_session(self):
        if self._loaded:
            return self._session
        if self._session_factory is not None:
            self._session = self._session_factory()
            self._loaded = True
            return self._session
        if not self.model_path:
            raise RuntimeError(
                "Silero VAD model not configured; set EV_EARS_VAD_MODEL_PATH "
                "or use the energy VAD double"
            )
        from app.audio.models import acquire_model

        with acquire_model("vad-silero"):
            try:
                import onnxruntime as ort
            except ImportError as exc:
                raise RuntimeError(
                    "onnxruntime is not installed; install the ml extra "
                    "(Agent 2 dependency request)"
                ) from exc
            self._session = ort.InferenceSession(self.model_path, providers=["CPUExecutionProvider"])
        self._loaded = True
        return self._session

    async def frame_probabilities(
        self, samples: array.array | list[int], sample_rate: int
    ) -> list[float]:
        if sample_rate != 16000:
            raise ValueError("Silero VAD requires 16 kHz audio")
        session = await asyncio_to_thread(self._load_session)
        frame = 512
        probabilities: list[float] = []
        values = samples if isinstance(samples, array.array) else array.array("h", samples)
        for start in range(0, len(values) - frame + 1, frame):
            chunk = array.array("f", (int(s) / 32768.0 for s in values[start : start + frame]))
            prob = await asyncio_to_thread(self._run_session, session, chunk)
            probabilities.append(prob)
        return probabilities

    async def block_probability(
        self, samples: array.array | list[int], sample_rate: int
    ) -> float | None:
        """Streaming decision: buffer to 512-sample frames; None while partial."""

        if sample_rate != 16000:
            raise ValueError("Silero VAD requires 16 kHz audio")
        session = await asyncio_to_thread(self._load_session)
        values = samples if isinstance(samples, array.array) else array.array("h", samples)
        self._pending.extend(values)
        frame = 512
        last_probability: float | None = None
        while len(self._pending) >= frame:
            chunk = array.array("f", (int(s) / 32768.0 for s in self._pending[:frame]))
            del self._pending[:frame]
            last_probability = await asyncio_to_thread(self._run_session, session, chunk)
        return last_probability

    def _run_session(self, session, chunk: array.array) -> float:
        import numpy as np

        result = session.run(None, {"input": np.asarray(chunk, dtype=np.float32).reshape(1, -1)})
        return float(result[0].reshape(-1)[0])


async def asyncio_to_thread(fn, *args):
    import asyncio

    return await asyncio.to_thread(fn, *args)


def frames_to_segments(
    frames: list[VadFrame],
    *,
    sample_rate: int,
    pre_roll_s: float = 0.25,
    post_roll_s: float = 0.75,
    min_speech_s: float = 0.2,
    engine: str = "",
) -> list[tuple[int, int]]:
    """Merge VAD frames into [start, end) sample ranges with pre/post roll."""

    if not frames:
        return []
    pre = int(pre_roll_s * sample_rate)
    post = int(post_roll_s * sample_rate)
    min_len = int(min_speech_s * sample_rate)
    ranges: list[tuple[int, int]] = []
    current: tuple[int, int] | None = None
    for frame in frames:
        if frame.speech:
            if current is None:
                current = (frame.start_sample, frame.end_sample)
            else:
                current = (current[0], frame.end_sample)
        elif current is not None:
            if frame.start_sample - current[1] <= post:
                current = (current[0], frame.end_sample)
            else:
                ranges.append(current)
                current = None
    if current is not None:
        ranges.append(current)
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        start = max(0, start - pre)
        end = end + post
        if end - start >= min_len:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
    return merged


class StreamingSegmenter:
    """Incremental VAD utterance builder for the always-on ears loop.

    The ears process pushes one capture block at a time together with the
    VAD's block decision. The segmenter keeps pre-roll from the ring at the
    moment speech starts, extends through post-roll silence, and emits a
    finalized utterance (never raw audio by default).
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        pre_roll_s: float = 0.25,
        post_roll_s: float = 0.75,
        min_speech_s: float = 0.2,
        speech_threshold: float = 0.5,
        max_segment_s: float = 60.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.pre_roll = int(pre_roll_s * sample_rate)
        self.post_roll = int(post_roll_s * sample_rate)
        self.min_speech = int(min_speech_s * sample_rate)
        self.speech_threshold = speech_threshold
        self.max_samples = int(max_segment_s * sample_rate)
        self._active: array.array = array.array("h")
        self._active_speech_samples = 0
        self._post_tail = 0
        self._prob_sum = 0.0
        self._prob_count = 0

    def _finalize(self, *, max_samples: int | None = None) -> VadSegment | None:
        if not self._active or self._active_speech_samples < self.min_speech:
            self._reset()
            return None
        # Trim trailing silence beyond post_roll.
        excess = max(0, self._post_tail - self.post_roll)
        end = len(self._active) - excess if excess else len(self._active)
        if max_samples is not None:
            end = min(end, max_samples)
        samples = self._active[:end] if end < len(self._active) else self._active
        mean = self._prob_sum / self._prob_count if self._prob_count else 0.0
        segment = VadSegment(
            start_sample=0,
            end_sample=end,
            samples=array.array("h", samples),
            mean_probability=mean,
            engine="streaming",
        )
        self._reset()
        return segment

    def _reset(self) -> None:
        self._active = array.array("h")
        self._active_speech_samples = 0
        self._post_tail = 0
        self._prob_sum = 0.0
        self._prob_count = 0

    def push(
        self,
        samples: array.array | list[int],
        probability: float | None,
        *,
        pre_roll_samples: array.array | list[int] | None = None,
    ) -> VadSegment | None:
        """Push one capture block; returns a finalized segment when one ends."""

        block = _frame_slice(samples, 0, len(samples))
        if not len(block):
            return None
        speech = probability is not None and probability >= self.speech_threshold
        if speech and self._prob_count == 0:
            # Start: include pre-roll supplied by the caller (from the ring).
            self._active = array.array("h", pre_roll_samples or [])
            self._active.extend(block)
            self._active_speech_samples += len(block)
            self._post_tail = 0
            self._prob_sum += float(probability or 0.0)
            self._prob_count += 1
            return None
        if self._prob_count == 0:
            return None  # idle; nothing retained
        self._active.extend(block)
        if speech:
            self._active_speech_samples += len(block)
            self._post_tail = 0
            self._prob_sum += float(probability or 0.0)
            self._prob_count += 1
            if self.max_samples and len(self._active) >= self.max_samples:
                return self._finalize(max_samples=self.max_samples)
        else:
            if probability is not None:
                self._post_tail += len(block)
                self._prob_sum += float(probability)
                self._prob_count += 1
                if self._post_tail > self.post_roll:
                    return self._finalize()
        return None

    def flush(self) -> VadSegment | None:
        if self._prob_count == 0:
            self._reset()
            return None
        return self._finalize()

    @property
    def active(self) -> bool:
        return self._prob_count > 0


async def segment_utterances(
    engine: VadEngine,
    samples: array.array | list[int],
    *,
    sample_rate: int = 16000,
    pre_roll_s: float = 0.25,
    post_roll_s: float = 0.75,
    min_speech_s: float = 0.2,
    speech_threshold: float = 0.5,
) -> list[VadSegment]:
    """Run a VAD engine and return merged utterance segments (samples included)."""

    probabilities = await engine.frame_probabilities(samples, sample_rate)
    frame_size = max(1, int(sample_rate * 30 / 1000))
    frames = [
        VadFrame(
            start_sample=i * frame_size,
            end_sample=min(len(samples), (i + 1) * frame_size),
            speech=prob >= speech_threshold,
            probability=prob,
        )
        for i, prob in enumerate(probabilities)
    ]
    ranges = frames_to_segments(
        frames,
        sample_rate=sample_rate,
        pre_roll_s=pre_roll_s,
        post_roll_s=post_roll_s,
        min_speech_s=min_speech_s,
        engine=engine.name,
    )
    segments: list[VadSegment] = []
    for start, end in ranges:
        clip = _frame_slice(samples, start, end)
        relevant = [f.probability for f in frames if f.end_sample > start and f.start_sample < end]
        segments.append(
            VadSegment(
                start_sample=start,
                end_sample=end,
                samples=clip,
                mean_probability=sum(relevant) / len(relevant) if relevant else 0.0,
                engine=engine.name,
            )
        )
    return segments


def default_vad_engine() -> VadEngine:
    """Config-driven VAD: Silero ONNX when configured, energy double otherwise."""

    from app.config import settings

    if settings.ears_vad_model_path:
        return SileroVadOnnx(
            model_path=settings.ears_vad_model_path,
            threshold=settings.ears_vad_threshold,
        )
    return EnergyVad()
