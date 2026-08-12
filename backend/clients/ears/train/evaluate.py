"""Acceptance evaluation: wake recall/FA, VAD frame accuracy, scene confusion."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path

from app.audio.scene import classify_wav
from app.audio.vad import EnergyVad
from app.voice.wake import OpenWakeWordEngine
from clients.ears.train.common import load_wav_pcm16
from clients.ears.train.tune_threshold import score_clips


def evaluate_scene(clips: list[Path], labels: dict[str, str]) -> dict:
    matrix: dict[tuple[str, str], int] = Counter()
    correct = 0
    for clip in clips:
        result = classify_wav(clip.read_bytes())
        predicted = result["scene"]
        expected = labels.get(clip.name, "unknown")
        matrix[(expected, predicted)] += 1
        if predicted == expected:
            correct += 1
    return {
        "top1_accuracy": correct / len(clips) if clips else 0.0,
        "confusion_matrix": {f"{a}->{b}": n for (a, b), n in sorted(matrix.items())},
    }


def evaluate_vad(labels: list[tuple[str, int, int, bool]]) -> dict:
    engine = EnergyVad()
    correct = 0
    total = 0
    for clip_name, start_ms, end_ms, expected in labels:
        clip = Path(clip_name)
        if not clip.is_file():
            continue
        samples, rate = load_wav_pcm16(clip)
        import asyncio

        probabilities = asyncio.run(engine.frame_probabilities(samples, rate))
        frame_size = max(1, int(rate * 30 / 1000))
        start = int(start_ms * rate / 1000)
        end = int(end_ms * rate / 1000)
        for i in range(start, min(end, len(samples)), frame_size):
            frame_prob = probabilities[i // frame_size] if i // frame_size < len(probabilities) else 0.0
            predicted = frame_prob >= 0.5
            total += 1
            correct += predicted == expected
    return {"frame_accuracy": correct / total if total else 0.0, "frames": total}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--held-out-dir", required=True)
    parser.add_argument("--ambient", default=None, help="12 h ambient for FA rate")
    parser.add_argument("--model-path", default=None)
    parser.add_argument("--verifier-path", default=None)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--scene-labels", default=None, help="CSV: clip,label")
    parser.add_argument("--vad-labels", default=None, help="CSV: clip,start_ms,end_ms,speech(0/1)")
    args = parser.parse_args(argv)
    held_out = sorted(Path(args.held_out_dir).glob("*.wav"))
    if not held_out:
        raise SystemExit(f"no clips in {args.held_out_dir}")

    print(f"held-out clips: {len(held_out)}")
    if args.model_path:
        engine = OpenWakeWordEngine(
            model_path=args.model_path,
            verifier_path=args.verifier_path,
            threshold=args.threshold,
        )
        clip_scores = score_clips(engine, held_out)
        recall = sum(1 for s in clip_scores if s >= args.threshold) / len(clip_scores)
        print(f"wake recall @ {args.threshold}: {recall:.3f}")
        if args.ambient:
            from clients.ears.train.tune_threshold import score_ambient

            ambient_scores = score_ambient(engine, Path(args.ambient))
            fa = sum(1 for s in ambient_scores if s >= args.threshold)
            print(f"false accepts (assumed 12 h ambient): {fa}")
    else:
        print("no --model-path; skipping wake gates (train the head first)")

    if args.scene_labels:
        labels = {}
        with Path(args.scene_labels).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                labels[row["clip"]] = row["label"]
        result = evaluate_scene(held_out, labels)
        print(f"scene top-1: {result['top1_accuracy']:.3f}")
        print("confusion:", result["confusion_matrix"])
    else:
        print("no --scene-labels; skipping scene gate (20 labeled clips per class)")

    if args.vad_labels:
        rows: list[tuple[str, int, int, bool]] = []
        with Path(args.vad_labels).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                rows.append((row["clip"], int(row["start_ms"]), int(row["end_ms"]), row["speech"] == "1"))
        result = evaluate_vad(rows)
        print(f"VAD frame accuracy: {result['frame_accuracy']:.3f} over {result['frames']} frames")
    else:
        print("no --vad-labels; skipping VAD gate (20 hand-labeled clips)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
