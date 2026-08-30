"""Stuck-mic / self-echo loop guard tests."""

from __future__ import annotations

import array
import math
import random

from app.audio.vad import _cross_chunk_diff, looks_stuck_loop

SAMPLE_RATE = 16000


def _speech_signal(seconds: float, *, seed: int) -> array.array:
    """Deterministic speech-like audio: syllable-amplitude noise bursts."""

    rng = random.Random(seed)
    out: list[int] = []
    amp = 5000.0
    for i in range(int(seconds * SAMPLE_RATE)):
        if i % 1920 == 0:  # new syllable every ~120 ms
            amp = rng.uniform(2000, 9000)
        wave = amp * (
            0.3 * math.sin(2 * math.pi * 120 * i / SAMPLE_RATE + rng.uniform(0, 1))
        )
        out.append(int(wave + rng.gauss(0, amp * 0.5)))
    return array.array("h", out)


def _loop(phrase_seconds: float, *, repeats: int, seed: int = 2) -> array.array:
    phrase = _speech_signal(phrase_seconds, seed=seed)[
        : int(phrase_seconds * SAMPLE_RATE)
    ]
    return array.array("h", list(phrase) * repeats)


def test_real_speech_is_never_flagged() -> None:
    for seed in range(6):
        assert looks_stuck_loop(_speech_signal(2.5, seed=seed), sample_rate=SAMPLE_RATE) is False


def test_literal_three_loop_is_flagged() -> None:
    for phrase in (0.5, 0.7, 0.9, 1.1, 1.3):
        assert (
            looks_stuck_loop(_loop(phrase, repeats=3), sample_rate=SAMPLE_RATE) is True
        ), f"3x loop at {phrase}s should be dropped"


def test_literal_two_loop_is_flagged() -> None:
    assert looks_stuck_loop(_loop(1.0, repeats=2), sample_rate=SAMPLE_RATE) is True


def test_short_and_silent_segments_are_not_loops() -> None:
    assert looks_stuck_loop(_speech_signal(0.4, seed=1), sample_rate=SAMPLE_RATE) is False
    assert looks_stuck_loop(array.array("h", [0] * 32000), sample_rate=SAMPLE_RATE) is False


def test_cross_chunk_diff_is_normalized() -> None:
    samples = _speech_signal(2.0, seed=1)
    same = array.array("h", list(samples) + list(samples))
    # Exact lag matches to ~0; unrelated lags are large.
    assert _cross_chunk_diff(same, len(samples)) < 0.001
    assert _cross_chunk_diff(samples, 1) > 0.1
