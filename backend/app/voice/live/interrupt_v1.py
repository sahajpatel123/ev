"""EVIE INTERRUPTION V1 — explicit-address natural barge-in.

Owner product decision 2026-08-23: while Evie is speaking, the owner can
take the floor by addressing her — "Evie...", "Hey Evie..." — at normal
conversational volume.

Composition law (PROJECT DIRECTIVE): when EV_EXPLICIT_INTERRUPT_ENABLED is
off, nothing in this module is constructed, attached, fed, or runnable.

Detection branch (OPTION A — explicit address + local evidence):

    mic tap copy (client, feature-gated)
        -> analysis_audio side channel (NOT provider input)
        -> EvieAddressSpotter   (local streaming DTW over mel templates)
        -> ownership fusion     (delay-aware correlation vs emitted audio)
        -> SELF / OWNER / AMBIGUOUS
        -> only OWNER_CONFIRMED may interrupt.

Laws frozen from prior incidents:
- provider mic forwarding stays BLOCKED during playback; the analysis
  channel is a copy, never the provider input path;
- not-confidently-SELF does not mean OWNER: AMBIGUOUS never interrupts;
- no fixed-RMS primary gate (energy is a sanity feature only);
- exactly-once execution is delegated to GrokVoiceBridge.interrupt_for_user
  (latch, one response.cancel, heard-position truncate with the zero-audio
  guard, stale-PCM discard);
- local speaker silence happens first (client ``interrupt_v1`` event) before
  provider cancellation.
"""
# -----------------------------------------------------------------------------
# ⚠️ DEAD / LEGACY / UNWIRED (2026-08-23 closure): spoken interruption CLOSED.
# Never wired into transport/session. Retained for reference only.
# -----------------------------------------------------------------------------

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger("ev.voice.live.interrupt_v1")

SAMPLE_RATE = 16_000
#: Analysis window fed to the spotter (seconds). "Evie" ≈ 0.4–0.7 s.
WINDOW_S = 0.9
#: Mic preroll kept while armed; forwarded through the provider input path on
#: confirmation so the owner's whole utterance ("Evie, I have another task.")
#: reaches the model, not its tail.
PREROLL_S = 1.6
#: Emitted-speech reference ring: speaker→room→mic has delay; correlation
#: searches lags up to this bound.
REFERENCE_S = 3.0
MAX_CORR_LAG_S = 0.45
#: Ownership fusion bounds (calibrated in
#: app/scripts/calibrate_interrupt_v1.py; see interrupt_v1_calibration.json).
SELF_CORR_REJECT = 0.5
SELF_CORR_CLEAR = 0.35
SPOT_THRESHOLD_DEFAULT = 0.62
CONFIRM_PERSISTENCE = 2  # consecutive windows
SANITY_MIC_RMS_RANGE = (0.004, 0.6)

TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "data" / "interrupt_v1"
TEMPLATE_GLOB = "template_*.npy"
CALIBRATION_JSON = "interrupt_v1_calibration.json"


# --------------------------------------------------------------------------
# Mel filterbank (pure numpy; runs off the realtime path in the backend)
# --------------------------------------------------------------------------

_FRAME_MS = 25
_HOP_MS = 10


def _mel_filterbank(n_filters: int = 40, n_fft: int = 400, floor_hz: float = 80.0):
    low, high = 2595 * math.log10(1 + floor_hz / 700), 2595 * math.log10(1 + SAMPLE_RATE / 2 / 700)
    mels = np.linspace(low, high, n_filters + 2)
    hzs = 700 * (10 ** (mels / 2595) - 1)
    bins = np.floor((n_fft + 1) * hzs / SAMPLE_RATE).astype(int)
    bank = np.zeros((n_filters, n_fft // 2 + 1))
    for i in range(n_filters):
        left, center, right = bins[i], bins[i + 1], bins[i + 2]
        for k in range(left, center):
            if center != left:
                bank[i, k] = (k - left) / (center - left)
        for k in range(center, right):
            if right != center:
                bank[i, k] = (right - k) / (right - center)
    return bank


_MEL_BANK = _mel_filterbank()
_WINDOW = np.hanning(400)


def mel_energies(pcm16: bytes) -> np.ndarray:
    """Log-mel rows for a PCM16 mono 16 kHz buffer (column-normalized)."""

    samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
    if samples.size < 400:
        samples = np.pad(samples, (0, 400 - samples.size))
    frames = np.lib.stride_tricks.sliding_window_view(samples, 400)[::160]
    spec = np.abs(np.fft.rfft(frames * _WINDOW, axis=1)) ** 2
    mels = _MEL_BANK @ spec.T
    mels = np.log(mels + 1e-8)
    mels = mels - mels.mean(axis=0, keepdims=True)
    norm = np.linalg.norm(mels, axis=0, keepdims=True) + 1e-8
    return mels / norm


# --------------------------------------------------------------------------
# Address spotter — streaming normalized DTW over mel templates
# --------------------------------------------------------------------------


@dataclass
class SpotResult:
    hit: bool
    score: float  # 1 - normalized DTW distance: higher = more "Evie"-like
    best_template: str | None = None


class EvieAddressSpotter:
    """Local "Evie" / "Hey Evie" template spotter (streaming, per-window).

    A window scores 1 - min_k NDTW(window, template_k). A hit requires the
    score over the calibrated threshold — energy is NOT the gate.
    """

    def __init__(self, templates_dir: Path | None = None, threshold: float | None = None):
        self.threshold = SPOT_THRESHOLD_DEFAULT if threshold is None else threshold
        self.templates: list[tuple[str, np.ndarray]] = []
        loaded = 0
        directory = templates_dir or TEMPLATES_DIR
        with contextlib.suppress(FileNotFoundError):
            for path in sorted(directory.glob(TEMPLATE_GLOB)):
                arr = np.load(path)
                if arr.ndim == 2 and arr.shape[1] >= 8:
                    self.templates.append((path.stem, arr))
                    loaded += 1
        if threshold is None:
            calib = directory / CALIBRATION_JSON
            if calib.is_file():
                with contextlib.suppress(json.JSONDecodeError):
                    data = json.loads(calib.read_text())
                    self.threshold = float(data.get("threshold", self.threshold))
        self.available = loaded > 0
        if self.available:
            logger.info(
                "interrupt_v1 spotter armed templates=%s threshold=%.3f",
                loaded,
                self.threshold,
            )

    def score(self, pcm16: bytes) -> SpotResult:
        if not self.templates:
            return SpotResult(hit=False, score=0.0)
        feats = mel_energies(pcm16)
        if feats.shape[1] < 8:
            return SpotResult(hit=False, score=0.0)
        best_name, best = None, 0.0
        for name, template in self.templates:
            d = _ndtw(feats, template)
            s = 1.0 - d
            if s > best:
                best, best_name = s, name
        return SpotResult(hit=best >= self.threshold, score=round(best, 4), best_template=best_name)


def _ndtw(a: np.ndarray, b: np.ndarray) -> float:
    """Normalized-DTW distance between two column-normalized mel matrices."""

    n, m = a.shape[1], b.shape[1]
    cost = np.full((n + 1, m + 1), np.inf)
    cost[0, 0] = 0.0
    diff = a[:, :, None] - b[:, None, :] if n * m <= 120 * 120 else None
    for i in range(1, n + 1):
        a_col = a[:, i - 1]
        for j in range(1, m + 1):
            local = (
                float(np.linalg.norm(diff[:, i - 1, j - 1]))
                if diff is not None
                else float(np.linalg.norm(a_col - b[:, j - 1]))
            )
            cost[i, j] = local + min(cost[i - 1, j], cost[i, j - 1], cost[i - 1, j - 1])
    return cost[n, m] / (n + m)


# --------------------------------------------------------------------------
# Ownership fusion + detector
# --------------------------------------------------------------------------


@dataclass
class Decision:
    classification: str  # SELF | OWNER | AMBIGUOUS
    reason: str


class ReferenceRing:
    """Bounded ring of the assistant's own emitted PCM (the SELF reference)."""

    def __init__(self, seconds: float = REFERENCE_S):
        self._buf = np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)
        self._n = 0

    def push(self, pcm16: bytes) -> None:
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        keep = max(0, self._buf.size - samples.size)
        self._buf = np.concatenate((self._buf[-keep:], samples))[- self._buf.size :]
        self._n = min(self._n + samples.size, self._buf.size)

    def tail(self, seconds: float) -> np.ndarray | None:
        if self._n == 0:
            return None
        return self._buf[-int(seconds * SAMPLE_RATE) :]

    def correlation(self, mic_pcm16: bytes) -> float:
        """Max normalized correlation of mic vs reference over plausible lags.

        Echo has delay; same-frame correlation alone was a past failure mode.
        """

        ref = self.tail(REFERENCE_S)
        if ref is None or ref.size < 1600:
            return 0.0
        mic = np.frombuffer(mic_pcm16, dtype="<i2").astype(np.float32) / 32768.0
        if mic.size < 1600:
            return 0.0
        mic = mic - mic.mean()
        best = 0.0
        mic_norm = np.linalg.norm(mic) + 1e-9
        max_lag = int(MAX_CORR_LAG_S * SAMPLE_RATE)
        for lag in range(0, max_lag, 320):  # 20 ms steps
            end = ref.size - lag
            if end < mic.size:
                break
            seg = ref[end - mic.size : end] - ref[end - mic.size : end].mean()
            denom = mic_norm * (np.linalg.norm(seg) + 1e-9)
            if denom <= 1e-12:
                continue
            corr = float(abs(np.dot(mic, seg)) / denom)
            best = max(best, corr)
        return round(min(best, 1.0), 4)


class ExplicitInterruptDetector:
    """Fuses the address spotter with self/owner ownership classification.

    Constructed ONLY when the feature flag is on. ``feed_analysis`` receives
    the client's mic copies during assistant playback; ``feed_reference``
    receives the assistant's emitted PCM. Confirmation is delivered through
    ``on_confirm`` on the detector's own task (never an audio callback).
    """

    def __init__(
        self,
        *,
        on_confirm,
        spotter: EvieAddressSpotter | None = None,
        reference: ReferenceRing | None = None,
    ):
        self._on_confirm = on_confirm
        self.spotter = spotter or EvieAddressSpotter()
        self.reference = reference or ReferenceRing()
        self.armed = False
        self._persistence = 0
        self._latched = False
        self._window = bytearray()
        self._preroll = bytearray()
        self._pending: bytes | None = None
        self._task: asyncio.Task | None = None
        self._wakeup = asyncio.Event()
        self.stats = {"windows": 0, "self_rejected": 0, "ambiguous": 0, "confirmed": 0, "no_wake": 0}

    # -- reference (assistant speech) ------------------------------------
    def feed_reference(self, pcm16: bytes) -> None:
        self.reference.push(pcm16)

    # -- analysis (mic copies during playback) ---------------------------
    def feed_analysis(self, pcm16: bytes) -> None:
        if not self.armed or self._latched:
            return
        self._window += pcm16
        self._preroll += pcm16
        max_window = int(WINDOW_S * SAMPLE_RATE * 2)
        max_preroll = int(PREROLL_S * SAMPLE_RATE * 2)
        if len(self._window) > max_window:
            del self._window[: len(self._window) - max_window]
        if len(self._preroll) > max_preroll:
            del self._preroll[: len(self._preroll) - max_preroll]
        if len(self._window) >= max_window:
            self._pending = bytes(self._window)
            self._wakeup.set()

    def latch_reset(self) -> None:
        """New response/floor epoch: a fresh interruption may be confirmed."""

        self._latched = False
        self._persistence = 0
        self._window.clear()

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    # -- classification ---------------------------------------------------
    def classify(self, mic_pcm16: bytes) -> Decision:
        spot = self.spotter.score(mic_pcm16)
        if not spot.hit:
            self.stats["no_wake"] += 1
            return Decision("AMBIGUOUS", f"NO_ADDRESS score={spot.score}")
        corr = self.reference.correlation(mic_pcm16)
        samples = np.frombuffer(mic_pcm16, dtype="<i2").astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
        if corr >= SELF_CORR_REJECT:
            self.stats["self_rejected"] += 1
            return Decision("SELF", f"SELF_ECHO corr={corr} score={spot.score}")
        if not (SANITY_MIC_RMS_RANGE[0] <= rms <= SANITY_MIC_RMS_RANGE[1]):
            self.stats["ambiguous"] += 1
            return Decision("AMBIGUOUS", f"ENERGY_SANITY rms={rms:.4f}")
        if corr >= SELF_CORR_CLEAR:
            self.stats["ambiguous"] += 1
            return Decision("AMBIGUOUS", f"AMBIGUOUS_OWNERSHIP corr={corr} score={spot.score}")
        self.stats["confirmed"] += 1
        return Decision("OWNER", f"OWNER_CONFIRMED corr={corr} score={spot.score}")

    async def _run(self) -> None:
        while True:
            await self._wakeup.wait()
            self._wakeup.clear()
            pending = self._pending
            self._pending = None
            if pending is None or self._latched or not self.armed:
                continue
            self.stats["windows"] += 1
            decision = self.classify(pending)
            if decision.classification == "OWNER":
                self._persistence += 1
                if self._persistence >= CONFIRM_PERSISTENCE:
                    self._latched = True
                    preroll = bytes(self._preroll)
                    logger.warning(
                        "interrupt_v1 event=INT05_OWNER_CONFIRMED %s persistence=%d",
                        decision.reason,
                        self._persistence,
                    )
                    await self._on_confirm(preroll)
            elif decision.classification == "SELF":
                logger.info("interrupt_v1 event=INT03_SELF_ECHO %s", decision.reason)
                self._persistence = 0
            else:
                self._persistence = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._wakeup.clear()
            self._task = asyncio.create_task(self._run(), name="ev-interrupt-v1")
