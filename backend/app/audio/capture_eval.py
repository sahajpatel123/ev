"""Guided capture wizard for the wake-reliability datasets (EARS Order 9).

Run ``python -m app.audio.capture_eval`` and follow the prompts — no docs
required. The wizard records:

* 30 "EVIE" utterances (10 from roughly 3 m) as 16 kHz mono WAVs
* 10 non-wake speech negatives for the custom verifier
* a long ambient session (recorded in chunks, or ingested from an existing
  file) for false-accept measurement

It ends with an exact "what is still missing" report. All recordings stay
local under the output directory (default ``backend/data/wake``) and are
owner-consented by the act of running the wizard.
"""

from __future__ import annotations

import argparse
import array
import json
import shutil
import subprocess
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from app.audio.capture import (
    MicrophoneDeniedError,
    MicrophoneStream,
    MicrophoneUnavailableError,
    list_input_devices,
    pcm_to_wav_bytes,
)

DEFAULT_FAR_SLOTS = (3, 6, 9, 12, 15, 18, 21, 24, 27, 30)
NEGATIVE_PROMPTS = (
    "Remind me to call mom tomorrow",
    "What is the weather going to be like this weekend",
    "Set a timer for ten minutes please",
    "Add oat milk to the shopping list",
    "Play something quiet in the background",
    "What time is my next meeting",
    "Send a message to Alex saying I will be late",
    "Turn off the lights in the living room",
    "Tell me a fun fact about space",
    "Read back my notes from today",
)


@dataclass
class CapturePlan:
    out_dir: Path
    clips_total: int = 30
    far_slots: tuple[int, ...] = DEFAULT_FAR_SLOTS
    negatives: int = 10
    seconds_per_clip: float = 2.5
    ambient_minutes: float = 0.0
    ambient_chunk_minutes: int = 10
    device: str | None = None
    sample_rate: int = 16000

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)

    def is_far(self, index: int) -> bool:
        return index in self.far_slots

    def clip_path(self, index: int) -> Path:
        tag = "3m" if self.is_far(index) else "close"
        return self.out_dir / "clips" / f"evie-{index:03d}-{tag}.wav"

    def negative_path(self, index: int) -> Path:
        return self.out_dir / "negatives" / f"negative-{index:02d}.wav"

    def ambient_dir(self) -> Path:
        return self.out_dir / "ambient"


def _require_sounddevice():
    try:
        import sounddevice  # noqa: F401
    except ImportError as exc:
        raise MicrophoneUnavailableError(
            "sounddevice is not installed, so live recording is unavailable. "
            "You can still ingest an existing ambient recording with "
            "--ingest-ambient, or install sounddevice (Agent 2 dependency)."
        ) from exc


def record_seconds(
    stream: MicrophoneStream,
    seconds: float,
    *,
    sample_rate: int = 16000,
) -> array.array:
    """Record ``seconds`` from the running stream into an in-memory buffer."""

    collected = array.array("h")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        collected.extend(stream.ring.read_new())
        time.sleep(0.05)
    return collected


def record_guided_clips(plan: CapturePlan, stream: MicrophoneStream) -> None:
    """Prompt and record the 30 EVIE clips + 10 negatives."""

    (plan.out_dir / "clips").mkdir(parents=True, exist_ok=True)
    (plan.out_dir / "negatives").mkdir(parents=True, exist_ok=True)
    for index in range(1, plan.clips_total + 1):
        distance = "from about 3 metres away" if plan.is_far(index) else "at normal speaking distance"
        print(f"\nClip {index}/{plan.clips_total} — say \"EVIE\" {distance}.")
        print("Recording in 3…2…1…", flush=True)
        time.sleep(1.0)
        samples = record_seconds(stream, plan.seconds_per_clip, sample_rate=plan.sample_rate)
        target = plan.clip_path(index)
        target.write_bytes(pcm_to_wav_bytes(samples, plan.sample_rate))
        print(f"Saved {target.relative_to(plan.out_dir)} "
              f"({len(samples) / plan.sample_rate:.1f}s)")
    for index in range(1, plan.negatives + 1):
        prompt = NEGATIVE_PROMPTS[(index - 1) % len(NEGATIVE_PROMPTS)]
        print(f"\nNegative {index}/{plan.negatives} — say: \"{prompt}\"")
        print("Recording in 3…2…1…", flush=True)
        time.sleep(1.0)
        samples = record_seconds(stream, plan.seconds_per_clip, sample_rate=plan.sample_rate)
        target = plan.negative_path(index)
        target.write_bytes(pcm_to_wav_bytes(samples, plan.sample_rate))
        print(f"Saved {target.name}")


def record_ambient(plan: CapturePlan, stream: MicrophoneStream) -> list[Path]:
    """Record the ambient session in bounded chunks (Ctrl-C stops early)."""

    ambient_dir = plan.ambient_dir()
    ambient_dir.mkdir(parents=True, exist_ok=True)
    chunk_seconds = plan.ambient_chunk_minutes * 60
    total_target = plan.ambient_minutes * 60
    recorded = 0.0
    chunks: list[Path] = []
    chunk_index = 1
    print(f"\nAmbient session: recording up to {plan.ambient_minutes:.0f} minutes "
          f"in {plan.ambient_chunk_minutes}-minute chunks. Press Ctrl-C to stop early.")
    try:
        while total_target <= 0 or recorded < total_target:
            seconds = min(chunk_seconds, total_target - recorded) if total_target > 0 else chunk_seconds
            print(f"Recording ambient chunk {chunk_index} ({seconds / 60:.0f} min)…")
            samples = record_seconds(stream, seconds, sample_rate=plan.sample_rate)
            target = ambient_dir / f"ambient-{chunk_index:03d}.wav"
            target.write_bytes(pcm_to_wav_bytes(samples, plan.sample_rate))
            chunks.append(target)
            recorded += seconds
            chunk_index += 1
    except KeyboardInterrupt:
        print("\nAmbient recording stopped early.")
    return chunks


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / max(1, wav.getframerate())


def ingest_ambient(source: str | Path, plan: CapturePlan) -> list[Path]:
    """Copy an existing recording (file or directory) into the ambient dir."""

    src = Path(source)
    ambient_dir = plan.ambient_dir()
    ambient_dir.mkdir(parents=True, exist_ok=True)
    suffixes = (".wav", ".m4a", ".mp3", ".flac")
    sources = sorted(p for p in src.rglob("*") if p.suffix.lower() in suffixes) if src.is_dir() else [src]
    if not sources:
        raise FileNotFoundError(f"no WAV files found under {src}")
    chunks: list[Path] = []
    for index, item in enumerate(sources, start=1):
        target = ambient_dir / f"ambient-{index:03d}.wav"
        _convert_to_wav(item, target)
        chunks.append(target)
    return chunks


def _convert_to_wav(source: Path, target: Path) -> None:
    """Convert any ffmpeg-readable audio to 16 kHz mono PCM16 WAV."""

    if source.suffix.lower() == ".wav":
        with wave.open(str(source), "rb") as wav:
            if wav.getnchannels() == 1 and wav.getsampwidth() == 2 and wav.getframerate() == 16000:
                target.write_bytes(source.read_bytes())
                return
    ffmpeg = shutil.which("ffmpeg") or shutil.which("afconvert")
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg/afconvert is required to convert non-16k mono WAV audio; "
            "install ffmpeg or provide 16 kHz mono WAV files"
        )
    if Path(ffmpeg).name == "ffmpeg":
        subprocess.run(
            [ffmpeg, "-y", "-i", str(source), "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(target)],
            check=True,
            capture_output=True,
        )
    else:
        subprocess.run(
            [ffmpeg, "-f", "WAVE", "-d", "LEI16@16000", str(source), str(target)],
            check=True,
            capture_output=True,
        )


def ingest_clips(source: str | Path, plan: CapturePlan) -> list[Path]:
    """Convert existing wake clips (m4a/wav/…) into the CapturePlan layout.

    Files whose names contain ``3m`` (or ``far``) are tagged as far clips;
    everything else is tagged close. Numbering continues after existing clips.
    """

    src = Path(source)
    suffixes = (".wav", ".m4a", ".mp3", ".flac")
    sources = sorted(p for p in src.rglob("*") if p.suffix.lower() in suffixes) if src.is_dir() else [src]
    if not sources:
        raise FileNotFoundError(f"no audio files found under {src}")
    clips_dir = plan.out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(clips_dir.glob("evie-*.wav"))
    next_index = 1 + max((int(p.stem.split("-")[1]) for p in existing), default=0)
    ingested: list[Path] = []
    for item in sources:
        tag = "3m" if any(t in item.stem.lower() for t in ("3m", "far")) else "close"
        target = clips_dir / f"evie-{next_index:03d}-{tag}.wav"
        _convert_to_wav(item, target)
        ingested.append(target)
        next_index += 1
    return ingested


def ingest_negatives(source: str | Path, plan: CapturePlan) -> list[Path]:
    """Convert non-wake speech recordings into negatives/."""

    src = Path(source)
    suffixes = (".wav", ".m4a", ".mp3", ".flac")
    sources = sorted(p for p in src.rglob("*") if p.suffix.lower() in suffixes) if src.is_dir() else [src]
    if not sources:
        raise FileNotFoundError(f"no audio files found under {src}")
    negatives_dir = plan.out_dir / "negatives"
    negatives_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(negatives_dir.glob("negative-*.wav"))
    next_index = 1 + max((int(p.stem.split("-")[1]) for p in existing), default=0)
    ingested: list[Path] = []
    for item in sources:
        target = negatives_dir / f"negative-{next_index:02d}.wav"
        _convert_to_wav(item, target)
        ingested.append(target)
        next_index += 1
    return ingested


def missing_report(plan: CapturePlan) -> dict:
    """Return exact counts and the human-readable 'still missing' list."""

    clips_dir = plan.out_dir / "clips"
    negatives_dir = plan.out_dir / "negatives"
    ambient_dir = plan.ambient_dir()
    clips = sorted(clips_dir.glob("evie-*.wav")) if clips_dir.is_dir() else []
    negatives = sorted(negatives_dir.glob("negative-*.wav")) if negatives_dir.is_dir() else []
    ambient = sorted(ambient_dir.glob("ambient-*.wav")) if ambient_dir.is_dir() else []
    close = [p for p in clips if "-3m" not in p.name]
    far = [p for p in clips if "-3m" in p.name]
    ambient_seconds = sum(wav_duration(p) for p in ambient)

    missing: list[str] = []
    if len(clips) < plan.clips_total:
        missing.append(
            f"{plan.clips_total - len(clips)} more EVIE clips "
            f"({plan.clips_total - len(clips) - max(0, 10 - len(far))} close, "
            f"{max(0, 10 - len(far))} from 3 m)"
        )
    elif len(far) < 10:
        missing.append(f"{10 - len(far)} EVIE clips from 3 m")
    if len(negatives) < plan.negatives:
        missing.append(f"{plan.negatives - len(negatives)} non-wake negatives")
    target_seconds = plan.ambient_minutes * 60
    if ambient_seconds < target_seconds:
        missing.append(
            f"{(target_seconds - ambient_seconds) / 3600:.1f} more hours of ambient audio "
            f"(have {ambient_seconds / 3600:.2f}h of {target_seconds / 3600:.1f}h)"
        )
    return {
        "clips": {"present": len(clips), "required": plan.clips_total},
        "far_clips": {"present": len(far), "required": 10},
        "close_clips": {"present": len(close), "required": plan.clips_total - 10},
        "negatives": {"present": len(negatives), "required": plan.negatives},
        "ambient_seconds": round(ambient_seconds, 1),
        "ambient_target_seconds": target_seconds,
        "still_missing": missing,
    }


def write_ambient_manifest(plan: CapturePlan, chunks: list[Path]) -> None:
    manifest = {
        "chunks": [str(p.relative_to(plan.out_dir)) for p in chunks],
        "total_seconds": round(sum(wav_duration(p) for p in chunks), 1),
    }
    (plan.ambient_dir() / "ambient.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.audio.capture_eval",
        description="Guided recording wizard for the EVIE wake-reliability datasets.",
    )
    parser.add_argument("--out-dir", default="backend/data/wake")
    parser.add_argument("--device", default=None)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--seconds-per-clip", type=float, default=2.5)
    parser.add_argument("--clips", type=int, default=30)
    parser.add_argument("--negatives", type=int, default=10)
    parser.add_argument("--far-slots", default="3,6,9,12,15,18,21,24,27,30")
    parser.add_argument("--ambient-minutes", type=float, default=0.0)
    parser.add_argument("--ambient-chunk-minutes", type=int, default=10)
    parser.add_argument("--ingest-ambient", default=None, help="existing WAV file/dir to ingest")
    parser.add_argument("--ingest-clips", default=None, help="existing wake-clip file/dir to ingest")
    parser.add_argument("--ingest-negatives", default=None, help="non-wake speech file/dir to ingest")
    parser.add_argument("--ingest-only", action="store_true", help="skip live clip recording")
    args = parser.parse_args(argv)

    if args.list_devices:
        try:
            for device in list_input_devices():
                print(f"[{device['index']}] {device['name']} "
                      f"(default {device['default_samplerate']} Hz)")
        except (MicrophoneDeniedError, MicrophoneUnavailableError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        return 0

    far_slots = tuple(int(v) for v in args.far_slots.split(",") if v.strip())
    plan = CapturePlan(
        out_dir=Path(args.out_dir),
        clips_total=args.clips,
        far_slots=far_slots,
        negatives=args.negatives,
        seconds_per_clip=args.seconds_per_clip,
        ambient_minutes=args.ambient_minutes,
        ambient_chunk_minutes=args.ambient_chunk_minutes,
        device=args.device,
    )
    print("EVIE wake-data capture wizard")
    print(f"Data will be saved under {plan.out_dir.resolve()} (local only, owner-consented).")

    ambient_chunks: list[Path] = []
    if args.ingest_clips:
        ingested = ingest_clips(args.ingest_clips, plan)
        print(f"Ingested {len(ingested)} wake clip(s).")
    if args.ingest_negatives:
        ingested = ingest_negatives(args.ingest_negatives, plan)
        print(f"Ingested {len(ingested)} negative(s).")
    if args.ingest_ambient:
        ambient_chunks = ingest_ambient(args.ingest_ambient, plan)
        write_ambient_manifest(plan, ambient_chunks)
        print(f"Ingested {len(ambient_chunks)} ambient WAV(s).")

    if not args.ingest_only:
        _require_sounddevice()
        stream = MicrophoneStream(
            sample_rate=plan.sample_rate,
            block_ms=20,
            device=plan.device,
        )
        try:
            stream.open()
        except MicrophoneDeniedError as exc:
            print(f"ERROR: Microphone permission denied — enable it in System Settings "
                  f"> Privacy & Security > Microphone. ({exc})", file=sys.stderr)
            return 3
        except MicrophoneUnavailableError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        try:
            record_guided_clips(plan, stream)
            if args.ambient_minutes > 0:
                ambient_chunks = record_ambient(plan, stream)
                write_ambient_manifest(plan, ambient_chunks)
        finally:
            stream.close()

    report = missing_report(plan)
    print("\n=== Capture summary ===")
    print(json.dumps(report, indent=2))
    if report["still_missing"]:
        print("\nStill missing:")
        for item in report["still_missing"]:
            print(f"  - {item}")
        return 0
    print("\nAll wake-reliability data captured. "
          "Next: python -m app.audio.wake_eval --held-out-dir ... --ambient ...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
