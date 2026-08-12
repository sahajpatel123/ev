"""Generate synthetic "EVIE" positives with piper-sample-generator + augments.

The generator produces many natural-sounding "EVIE" utterances across voices;
each is convolved with room impulse responses and mixed with background noise
so the wake head learns the phrase, not a single room/voice.
"""

from __future__ import annotations

import argparse
import array
import random
import subprocess
import wave
from pathlib import Path

from clients.ears.train.common import load_wav_pcm16, require_numpy

PHRASES = [
    "EVIE",
    "Hey EVIE",
    "EVIE wake up",
    "Okay EVIE",
    "EVIE, I need you",
    "Hey EVIE, are you there",
]


def _run_piper_sample_generator(out_dir: Path, count: int, voice: str) -> list[Path]:
    try:
        subprocess.run(
            ["piper-sample-generator", "--help"],
            capture_output=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "piper-sample-generator is not installed (Agent 2 dependency request). "
            "It is required to synthesize the 'EVIE' positive pool."
        ) from exc
    produced: list[Path] = []
    for i in range(count):
        phrase = PHRASES[i % len(PHRASES)]
        out = out_dir / f"synth-{i:04d}.wav"
        subprocess.run(
            [
                "piper-sample-generator",
                "--text", phrase,
                "--output", str(out),
                "--voice", voice,
                "--length-scale", "0.9",
            ],
            check=True,
            capture_output=True,
        )
        if out.is_file():
            produced.append(out)
    return produced


def _synthetic_rir(samples: int, rate: int, seed: int) -> array.array:
    """Deterministic synthetic impulse response (early reflections + decay)."""

    rng = random.Random(seed)
    taps = [0.0] * samples
    taps[0] = 1.0
    for _ in range(24):
        delay = rng.randint(int(rate * 0.002), int(rate * 0.08))
        if delay < samples:
            taps[delay] = rng.uniform(0.05, 0.35)
    decay = 0.995
    for i in range(1, samples):
        taps[i] *= decay**i
    return array.array("h", [int(max(-32768, min(32767, t * 12000))) for t in taps])


def _convolve(signal: array.array, impulse: array.array) -> array.array:
    np = require_numpy()
    out = np.convolve(
        np.asarray(signal, dtype=np.float64), np.asarray(impulse, dtype=np.float64)
    )
    return array.array("h", np.clip(np.rint(out), -32768, 32767).astype(np.int16))


def _mix_noise(signal: array.array, noise: array.array, snr_db: float, seed: int) -> array.array:
    np = require_numpy()
    sig = np.asarray(signal, dtype=np.float64)
    if len(noise) < len(sig):
        noise = np.resize(np.asarray(noise, dtype=np.float64), len(sig))
    noise = noise[: len(sig)]
    target = 10 ** (snr_db / 20.0)
    scale = (np.sqrt(np.mean(np.square(sig))) / (np.sqrt(np.mean(np.square(noise))) + 1e-9)) / target
    mixed = sig + noise * scale
    return array.array("h", np.clip(np.rint(mixed), -32768, 32767).astype(np.int16))


def augment(
    clip: Path,
    out_dir: Path,
    *,
    noise_clips: list[Path] | None = None,
    seed: int = 0,
) -> list[Path]:
    """RIR + noise augmentations of one synthesized positive."""

    samples, rate = load_wav_pcm16(clip)
    rng = random.Random(seed)
    produced: list[Path] = []
    for variant in range(3):
        rir = _synthetic_rir(int(rate * 0.1), rate, seed + variant)
        reverb = _convolve(samples, rir)[: len(samples)]
        if noise_clips:
            noise_path = noise_clips[(seed + variant) % len(noise_clips)]
            noise, noise_rate = load_wav_pcm16(noise_path)
            if noise_rate != rate:
                continue
            mixed = _mix_noise(reverb, noise, snr_db=rng.uniform(8, 18), seed=seed + variant)
        else:
            mixed = reverb
        out = out_dir / f"{clip.stem}-aug{variant}.wav"
        with wave.open(str(out), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(mixed.tobytes())
        produced.append(out)
    return produced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/wake/synthetic", help="output directory")
    parser.add_argument("--count", type=int, default=120)
    parser.add_argument("--voice", default="en_US-lessac-medium")
    parser.add_argument("--noise-dir", default=None, help="ambient/background noise WAVs")
    args = parser.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    generated = _run_piper_sample_generator(out_dir, args.count, args.voice)
    noise_clips = list(Path(args.noise_dir).rglob("*.wav")) if args.noise_dir else []
    for index, clip in enumerate(generated):
        augment(clip, out_dir, noise_clips=noise_clips, seed=index)
    print(f"generated {len(generated)} synthetic positives (+ augmentations) in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
