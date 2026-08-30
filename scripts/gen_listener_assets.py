"""Generate Evie listener-presence backchannel assets.

Uses the backend's own configured TTS stack (the same voice owners hear in
Talk replies) to pre-render a small set of soft micro-utterances with
prosodic variants, then converts them to mono 16 kHz PCM16, trims silence,
applies the soft listening gain, and writes them into the Mac app's cache:

    ~/Library/Application Support/EV/listener/manifest.json + *.pcm16

Run:  cd backend && uv run python ../scripts/gen_listener_assets.py [--force]
"""
from __future__ import annotations

import argparse
import asyncio
import audioop  # noqa: F401  (verified available in the project venv)
import json
import math
import struct
import sys
import wave
from io import BytesIO
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

OUT_DIR = Path.home() / "Library" / "Application Support" / "EV" / "listener"
TARGET_RATE = 16_000
# SINGLE-GAIN LAW: assets are normalized to SOURCE_RMS_TARGET at generation;
# the ONLY listening attenuation is the per-family playback gain in the
# manifest, applied once by TTSPlayer.scaledPCM16. The previous chain applied
# SOFT_GAIN=0.30 here AND ~0.30-0.36 at playback (net ~x0.10, about -20 dB),
# which is exactly why Round One nods were "unclear". Never pre-attenuate.
SOURCE_RMS_TARGET = 0.28  # healthy natural render; peak clamped below clip
PEAK_CEILING = 0.89

# (variant_id, class, intended_family, text, warmth, urgency)
# Round-two vocabulary: nonlexical-dominant with duration families.
# Longer ≠ louder — elongated ships at LOWER playback gain.
SPEC = [
    # MICRO (~250–460 ms target) — very subtle
    ("mm-micro-a", "neutralContinuer", "micro", "Mm.", 0.85, 0.05),
    ("mhm-micro-a", "neutralContinuer", "micro", "Mhm.", 0.8, 0.07),
    # NORMAL (~450–820 ms) — default listener nod
    ("mhm-normal-a", "neutralContinuer", "normal", "Mhm-hm.", 0.88, 0.06),
    ("mhm-normal-b", "neutralContinuer", "normal", "Mm-hm.", 0.84, 0.09),
    ("mmhm-rising-a", "neutralContinuer", "normal", "Mm-hm?", 0.88, 0.12),
    ("hmm-normal-a", "neutralContinuer", "normal", "Hmmm.", 0.85, 0.05),
    ("uhhuh-soft-a", "lightAcknowledgment", "normal", "Uh-huh.", 0.85, 0.08),
    # ELONGATED (~700–1200 ms) — warm continuation nods, lower gain.
    # Trailing ellipsis + max warmth push the TTS toward a drawn-out,
    # soft "mhmmm…" rather than a clipped syllable.
    ("mhmmm-warm-long-a", "neutralContinuer", "elongated", "Mhmmm…", 1.0, 0.02),
    ("hmmm-warm-long-a", "neutralContinuer", "elongated", "Hmmm…", 0.98, 0.01),
    ("mmhm-warm-long-a", "neutralContinuer", "elongated", "Mm-hmm…", 0.96, 0.03),
]

# Playback-side A/B candidates (applied ONCE at playback). Elongated stays
# lowest: LONGER ≠ LOUDER. If the owner reports gestures still too quiet in
# round two, raise these together — never re-add a generation-side gain.
FAMILY_GAIN = {"micro": 0.42, "normal": 0.40, "elongated": 0.36}
FAMILY_WINDOW_MS = {"micro": (230, 500), "normal": (430, 900), "elongated": (650, 1300)}


def decode_to_pcm16(audio: bytes, content_type: str | None) -> tuple[bytes, int]:
    """Return (mono int16 LE bytes, sample_rate). MP3 goes through ffmpeg."""
    if audio[:4] == b"RIFF":
        with wave.open(BytesIO(audio), "rb") as w:
            rate = w.getframerate()
            channels = w.getnchannels()
            width = w.getsampwidth()
            frames = w.readframes(w.getnframes())
        if width != 2:
            frames = audioop.lin2lin(frames, width, 2)
        if channels > 1:
            frames = audioop.tomono(frames, 2, 0.5, 0.5)
        return frames, rate
    kind = (content_type or "").lower()
    if "mpeg" in kind or "mp3" in kind or audio[:3] == b"ID3" or (len(audio) > 2 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0):
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(audio)
            src = tmp.name
        try:
            proc = subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-i", src,
                    "-f", "s16le", "-acodec", "pcm_s16le",
                    "-ac", "1", "-ar", str(TARGET_RATE), "-",
                ],
                capture_output=True, check=True,
            )
            return proc.stdout, TARGET_RATE
        finally:
            Path(src).unlink(missing_ok=True)
    raise SystemExit(f"unsupported synthesis content type: {content_type!r}")


def resample(pcm: bytes, src_rate: int) -> bytes:
    if src_rate == TARGET_RATE:
        return pcm
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    out_len = int(n * TARGET_RATE / src_rate)
    out = []
    for i in range(out_len):
        pos = i * (n - 1) / max(1, out_len - 1)
        i0 = int(pos)
        i1 = min(n - 1, i0 + 1)
        frac = pos - i0
        value = samples[i0] * (1 - frac) + samples[i1] * frac
        out.append(int(max(-32767, min(32767, value))))
    return struct.pack(f"<{len(out)}h", *out)


def trim_silence(pcm: bytes, threshold: int = 220, pad_ms: int = 40) -> bytes:
    n = len(pcm) // 2
    samples = list(struct.unpack(f"<{n}h", pcm[: n * 2]))
    limit = threshold
    start, end = 0, len(samples) - 1
    while start < len(samples) and abs(samples[start]) < limit:
        start += 1
    while end > start and abs(samples[end]) < limit:
        end -= 1
    start = max(0, start - TARGET_RATE * pad_ms // 1000)
    end = min(len(samples), end + TARGET_RATE * pad_ms // 1000)
    kept = samples[start:end]
    # Fade edges to kill clicks.
    fade = TARGET_RATE * 15 // 1000
    for i in range(min(fade, len(kept))):
        scale = i / max(1, fade)
        kept[i] = int(kept[i] * scale)
        kept[-1 - i] = int(kept[-1 - i] * scale)
    return struct.pack(f"<{len(kept)}h", *kept)


def apply_gain(pcm: bytes, gain: float) -> bytes:
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    out = [int(max(-32767, min(32767, s * gain))) for s in samples]
    return struct.pack(f"<{len(out)}h", *out)


def rms_of(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    return math.sqrt(sum(s * s for s in samples) / n) / 32768.0


def peak_of(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    return max(abs(s) for s in samples) / 32768.0


def normalize_loudness(pcm: bytes) -> tuple[bytes, float, float]:
    """Bring every clip to ONE source RMS so the manifest playback gain is the
    single loudness knob. If hitting that RMS would clip, fall back to a safe
    peak ceiling and keep whatever RMS resulted."""
    current_rms = rms_of(pcm)
    if current_rms < 1e-4:
        return pcm, current_rms, peak_of(pcm)
    gain = SOURCE_RMS_TARGET / current_rms
    peak = peak_of(pcm)
    if peak * gain > PEAK_CEILING:
        gain = PEAK_CEILING / max(peak, 1e-6)
    scaled = apply_gain(pcm, gain)
    return scaled, rms_of(scaled), peak_of(scaled)


async def main(force: bool) -> None:
    from app.voice.tts import get_synthesizer
    from app.voice.contracts import SpeechStyle

    synth = get_synthesizer()
    print(f"synthesizer={type(synth).__name__}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT_DIR / "manifest.json"
    if manifest_path.exists() and not force:
        print("manifest exists; pass --force to regenerate")
        return

    entries: list[dict] = []
    seen_hashes: set[str] = set()
    for variant_id, kind, family, text, warmth, urgency in SPEC:
        style = SpeechStyle(warmth=warmth, urgency=urgency, brevity=0.98, mode="casual")
        try:
            result = await synth.synthesize(text, style=style)
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP {variant_id}: {exc}")
            continue
        audio = result.audio
        if not audio:
            print(f"SKIP {variant_id}: no inline audio (ref={result.audio_ref!r})")
            continue
        try:
            pcm, rate = decode_to_pcm16(audio, result.content_type)
            pcm = resample(pcm, rate)
            pcm = trim_silence(pcm)
            # SINGLE-GAIN LAW: normalize to a common source loudness; the
            # per-family playback gain in the manifest is the only knob.
            pcm, out_rms, out_peak = normalize_loudness(pcm)
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP {variant_id}: post-process failed: {exc}")
            continue
        duration_ms = len(pcm) * 1000 // (TARGET_RATE * 2)
        if duration_ms < 120 or duration_ms > 1600:
            print(f"SKIP {variant_id}: unnatural duration {duration_ms}ms")
            continue
        digest = f"{duration_ms}:{out_rms:.4f}:{hash(pcm) & 0xFFFFFFFF:08x}"
        if digest in seen_hashes:
            print(f"SKIP {variant_id}: duplicate of an earlier render ({digest})")
            continue
        seen_hashes.add(digest)
        lo, hi = FAMILY_WINDOW_MS[family]
        verdict = "OK"
        # TRUTH WINS: the manifest family must describe the audio that
        # actually exists, because policy uses it to pick duration shapes.
        # If the render drifted into another window, relabel honestly.
        if not (lo <= duration_ms <= hi):
            measured = min(
                FAMILY_WINDOW_MS,
                key=lambda f: abs(duration_ms - sum(FAMILY_WINDOW_MS[f]) / 2),
            )
            if measured != family and (
                FAMILY_WINDOW_MS[measured][0] <= duration_ms <= FAMILY_WINDOW_MS[measured][1]
            ):
                verdict = f"RELABEL {family}->{measured} (rendered {duration_ms}ms)"
                family = measured
            else:
                verdict = f"OFF-WINDOW kept={family} (rendered {duration_ms}ms)"
        gain = FAMILY_GAIN[family]
        file_name = f"{variant_id}.pcm16"
        (OUT_DIR / file_name).write_bytes(pcm)
        entries.append({
            "id": variant_id, "class": kind, "file": file_name,
            "family": family, "gain": gain,
        })
        print(
            f"{verdict} {variant_id}: {duration_ms}ms family={family} "
            f"gain={gain} src_rms={out_rms:.3f} peak={out_peak:.2f}"
        )

    families = {}
    for e in entries:
        families[e["family"]] = families.get(e["family"], 0) + 1
    missing = [f for f in ("micro", "normal", "elongated") if families.get(f, 0) == 0]
    if missing:
        print(f"WARN: no variants rendered for family/families: {missing}")
    manifest_path.write_text(json.dumps(entries, indent=2))
    print(f"wrote {len(entries)} variants → {manifest_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    asyncio.run(main(parser.parse_args().force))
