"""Shared audio/data helpers for wake training and evaluation."""

from __future__ import annotations

import array
import csv
import wave
from collections.abc import Iterable
from pathlib import Path


def require_numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "numpy is required for wake training; install the ml extra "
            "(Agent 2 dependency request)"
        ) from exc
    return np


def load_wav_pcm16(path: str | Path, target_rate: int = 16000) -> tuple[array.array, int]:
    """Load WAV as mono int16 at the target rate (linear resample when needed)."""

    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"{path}: wake training requires 16-bit PCM WAV, got width={width}")
    samples = array.array("h", raw)
    if channels > 1:
        mono = array.array("h")
        for i in range(0, len(samples) - channels + 1, channels):
            mono.append(sum(samples[i : i + channels]) // channels)
        samples = mono
    if rate != target_rate:
        samples = _resample_linear(samples, rate, target_rate)
    return samples, target_rate


def _resample_linear(samples: array.array, from_rate: int, to_rate: int) -> array.array:
    if not samples:
        return samples
    np = require_numpy()
    source = np.asarray(samples, dtype=np.float64)
    positions = np.arange(0, len(source), from_rate / to_rate)
    indices = np.floor(positions).astype(np.int64)
    indices = np.clip(indices, 0, len(source) - 2)
    frac = (positions - indices).astype(np.float64)
    out = source[indices] * (1 - frac) + source[indices + 1] * frac
    return array.array("h", np.clip(np.rint(out), -32768, 32767).astype(np.int16))


def iter_clips(directory: str | Path, suffixes=(".wav",)) -> list[Path]:
    """List WAV clips in a directory, sorted for reproducibility."""

    root = Path(directory)
    if not root.is_dir():
        raise FileNotFoundError(f"clip directory not found: {root}")
    return sorted(p for p in root.rglob("*") if p.suffix.lower() in suffixes)


def split_clips(clips: list[Path], *, held_out: int = 30, seed: int = 7):
    """Deterministic split: first 30 clips held out (10 at distance when named)."""

    np = require_numpy()
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(clips)).tolist()
    held = [clips[i] for i in order[:held_out]]
    train = [clips[i] for i in order[held_out:]]
    return train, held


def write_labels_csv(path: str | Path, rows: Iterable[tuple[str, str]]) -> None:
    """Write clip→label rows (used by the VAD hand-label and scene gates)."""

    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("path", "label"))
        writer.writerows(rows)
