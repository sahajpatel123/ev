"""Tune the runtime wake threshold against the human's 12 h ambient recording.

Goal (acceptance): false accepts <= 1 per 12 h on ambient audio while recall
is >= 90% on the 30 held-out "EVIE" clips. The tuned threshold is written as
JSON and should be copied into EV_EARS_WAKE_THRESHOLD /
EV_VOICE_WAKE_OPENWAKEWORD_THRESHOLD.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.audio.wake_eval import (
    build_engine,
    score_ambient,
    score_clips,
    sweep_thresholds,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ambient", required=True, help="12 h ambient WAV (16 kHz)")
    parser.add_argument("--held-out-dir", required=True, help="30 held-out EVIE clips")
    parser.add_argument("--model-path", required=True, help="exported EVIE .onnx head")
    parser.add_argument("--verifier-path", default=None, help="custom verifier .pkl")
    parser.add_argument("--output", default="data/wake/tuned-threshold.json")
    parser.add_argument("--target-fa-12h", type=float, default=1.0)
    parser.add_argument("--min-recall", type=float, default=0.90)
    parser.add_argument("--ambient-hours", type=float, default=12.0)
    args = parser.parse_args(argv)

    engine = build_engine(
        model_path=args.model_path,
        verifier_path=args.verifier_path,
        threshold=0.0,  # score everything; threshold picked below
    )
    print("scoring ambient...")
    ambient_scores, hours, replay_speed = score_ambient(engine, Path(args.ambient))
    print(f"scoring {args.held_out_dir}...")
    clips = sorted(Path(args.held_out_dir).rglob("*.wav"))
    clip_scores = score_clips(engine, clips)
    if not clip_scores:
        raise SystemExit("no held-out clips found")
    tuned, curve = sweep_thresholds(
        ambient_scores,
        clip_scores,
        hours_audio=args.ambient_hours,
        min_recall=args.min_recall,
        max_fa_per_12h=args.target_fa_12h,
    )
    at_tuned = next(e for e in curve if e["threshold"] == tuned)
    tuned_metrics = {
        "false_accepts_12h": at_tuned["false_accepts_per_12h"],
        "recall": at_tuned["recall"],
    }
    if not (
        at_tuned["false_accepts_per_12h"] <= args.target_fa_12h
        and at_tuned["recall"] >= args.min_recall
    ):
        tuned_metrics["warning"] = "no threshold met both gates; kept best available"
    result = {
        "tuned_threshold": tuned,
        **tuned_metrics,
        "ambient_chunks": len(ambient_scores),
        "held_out_clips": len(clip_scores),
        "ambient_hours_measured": round(hours, 3),
        "replay_speed_x": round(replay_speed, 2),
        "threshold_curve": curve,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(
        "copy the threshold into EV_EARS_WAKE_THRESHOLD / "
        "EV_VOICE_WAKE_OPENWAKEWORD_THRESHOLD"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
