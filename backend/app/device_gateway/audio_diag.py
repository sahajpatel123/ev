"""Test-only known-clean audio for the phone diagnostic ladder. Never owner speech."""

from __future__ import annotations

import math
import struct
from pathlib import Path

PWA_ROOT = Path(__file__).resolve().parents[2] / "clients" / "pwa"
SAMPLE_RATE = 16000
DURATION_S = 2.0


def known_clean_pcm16(*, seconds: float = DURATION_S, rate: int = SAMPLE_RATE) -> bytes:
    """Deterministic 'speech-like' AM tone. Clean by construction. No Realtime. No owner audio."""

    n = int(rate * seconds)
    out = bytearray()
    for i in range(n):
        t = i / rate
        formant = 0.55 * math.sin(2 * math.pi * 220 * t) + 0.28 * math.sin(2 * math.pi * 440 * t)
        envelope = 0.35 + 0.65 * abs(math.sin(2 * math.pi * 3.5 * t))
        sample = max(-0.9, min(0.9, formant * envelope))
        out.extend(struct.pack("<h", int(sample * 30000)))
    return bytes(out)


def wav_from_pcm16(pcm: bytes, *, rate: int = SAMPLE_RATE) -> bytes:
    data_size = len(pcm)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        rate,
        rate * 2,
        2,
        16,
        b"data",
        data_size,
    )
    return header + pcm


def format_truth(pcm: bytes, *, rate: int = SAMPLE_RATE) -> dict:
    samples = len(pcm) // 2
    duration = samples / float(rate) if rate else 0.0
    return {
        "declared_codec": "pcm16le",
        "declared_rate": rate,
        "byte_size": len(pcm),
        "sample_count": samples,
        "odd_trailing_byte": len(pcm) % 2,
        "duration_s": round(duration, 4),
        "endian": "little",
        "matches_declared": len(pcm) % 2 == 0 and samples == int(round(duration * rate)),
    }


def write_pwa_assets() -> None:
    pcm = known_clean_pcm16()
    PWA_ROOT.mkdir(parents=True, exist_ok=True)
    (PWA_ROOT / "diag-speech.pcm").write_bytes(pcm)
    (PWA_ROOT / "diag-speech.wav").write_bytes(wav_from_pcm16(pcm))
