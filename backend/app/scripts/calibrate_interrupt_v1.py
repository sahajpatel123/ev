"""Generate and calibrate EVIE INTERRUPTION V1 address templates.

Templates: on-device synthesized "Evie" / "Hey Evie" variants across macOS
voices (plus the owner's real clip when present). Calibration: score a bank
of negative phrases and same-voice "Evie" positives, pick the threshold that
separates them with margin, and persist interrupt_v1_calibration.json.

Run:  cd backend && uv run python -m app.scripts.calibrate_interrupt_v1
"""
# ⚠️ DEAD / LEGACY / UNWIRED (2026-08-23 closure): spoken interruption CLOSED.

from __future__ import annotations

import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

from app.voice.live.interrupt_v1 import (
    SAMPLE_RATE,
    WINDOW_S,
    TEMPLATES_DIR,
    EvieAddressSpotter,
    mel_energies,
)

POSITIVE_PHRASES = [
    "Evie.",
    "Evie?",
    "Hey Evie.",
    "Hey Evie?",
    "Evie, wait.",
    "Evie, stop.",
]
NEGATIVE_PHRASES = [
    "The weather is lovely today.",
    "Maybe later.",
    "I was thinking about the meeting.",
    "The solar system formed long ago.",
    "Absolutely, that sounds good.",
    "Let me think about that for a second.",
    "The report is on your desk.",
    "Sure, whenever you're ready.",
    "Okay then, let's move on to the next topic.",
    "I really like this song.",
]
VOICES = None  # auto: first N en voices that render


def _voices(limit: int = 8) -> list[str]:
    global VOICES
    if VOICES is not None:
        return VOICES
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    names = [
        line.split()[0]
        for line in out.splitlines()
        if line.strip() and "en_" in line
    ]
    VOICES = names[:limit]
    return VOICES


def _render(phrase: str, voice: str, rate: int | None = None) -> bytes | None:
    aiff = Path("/tmp/intv1_render.aiff")
    wav = Path("/tmp/intv1_render.wav")
    cmd = ["say", "-v", voice, "-o", str(aiff), phrase]
    if rate:
        cmd[-2:-1] = ["-r", str(rate)]
    if subprocess.run(cmd, capture_output=True).returncode != 0:
        return None
    if subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
        capture_output=True,
    ).returncode != 0:
        return None
    with wave.open(str(wav)) as w:
        return w.readframes(w.getnframes())


def main() -> int:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    templates = 0
    for voice in _voices():
        for phrase in POSITIVE_PHRASES[:4]:
            pcm = _render(phrase, voice)
            if not pcm:
                continue
            pcm = pcm[: int(WINDOW_S * SAMPLE_RATE) * 2]
            feats = mel_energies(pcm)
            np.save(TEMPLATES_DIR / f"template_{voice.replace(' ', '_')}_{templates:03d}.npy", feats)
            templates += 1
    # The owner's real clip, when present, becomes a template too.
    real = Path("data/wake/clips/evie-001-close.wav")
    if real.is_file():
        with wave.open(str(real)) as w:
            pcm = w.readframes(w.getnframes())
        np.save(TEMPLATES_DIR / "template_owner_real_000.npy", mel_energies(pcm))
        templates += 1
    print(f"templates: {templates}")

    # Calibration: positives (held-out phrases/voices) vs negatives.
    spotter = EvieAddressSpotter(threshold=0.0)  # raw scores
    pos_scores: list[float] = []
    for voice in _voices():
        for phrase in POSITIVE_PHRASES:
            pcm = _render(phrase, voice)
            if pcm:
                pcm = pcm[: int(WINDOW_S * SAMPLE_RATE) * 2]
                pos_scores.append(spotter.score(pcm).score)
    neg_scores: list[float] = []
    neg_voice = _voices(3)[-1]
    for phrase in NEGATIVE_PHRASES:
        pcm = _render(phrase, neg_voice)
        if pcm:
            pcm = pcm[: int(WINDOW_S * SAMPLE_RATE) * 2]
            neg_scores.append(spotter.score(pcm).score)
        pcm = _render(phrase, _voices(3)[0])
        if pcm:
            pcm = pcm[: int(WINDOW_S * SAMPLE_RATE) * 2]
            neg_scores.append(spotter.score(pcm).score)
    pos_scores = [s for s in pos_scores if s > 0]
    neg_scores = [s for s in neg_scores if s > 0]
    if not pos_scores or not neg_scores:
        print("calibration incomplete: missing scores", pos_scores, neg_scores)
        return 1
    lo = float(np.percentile(pos_scores, 8))   # accept most positives
    hi = float(np.percentile(neg_scores, 99))  # reject essentially all negatives
    threshold = round((max(hi, lo - 0.04) + lo) / 2, 3) if hi < lo else round(lo, 3)
    calibration = {
        "threshold": threshold,
        "pos_p8": round(lo, 4),
        "pos_median": round(float(np.median(pos_scores)), 4),
        "neg_p99": round(hi, 4),
        "neg_max": round(float(np.max(neg_scores)), 4),
        "pos_n": len(pos_scores),
        "neg_n": len(neg_scores),
        "sample_rate": SAMPLE_RATE,
    }
    (TEMPLATES_DIR / "interrupt_v1_calibration.json").write_text(json.dumps(calibration, indent=2))
    print(json.dumps(calibration, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
