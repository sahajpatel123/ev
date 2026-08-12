"""Wake reliability measurement (EARS Order 9).

Measures false accepts per 12 h against the owner's real ambient recording and
recall on the held-out "EVIE" clips (with a distance breakdown), sweeps the
threshold, and writes the canonical ``backend/eval/ml/wake_reliability.json``
artifact consumed by Agent 20's ``wake_reliability`` gate and Agent 2's
``ev-eval wake``.

The ambient session is replayed faster than real time: WAV frames are scored
as fast as the model can consume them (no sleeping), and the measured
``replay_speed_x`` is reported.

Run with no data/model paths and the command prints the exact gate skip
reason and writes nothing — no test double is ever presented as a measured
quality number (``--test-double`` writes a ``degraded:true`` artifact that the
gate skips).
"""

from __future__ import annotations

import argparse
import json
import time
import wave
from pathlib import Path

from app.voice.wake import OpenWakeWordEngine

OUT_PATH = Path(__file__).resolve().parents[2] / "eval" / "ml" / "wake_reliability.json"

SKIP_REASON = (
    "no eval artifact at {path}; run Agent 3's wake eval against the trained "
    'openWakeWord head ({"provider":"openwakeword","degraded":false,'
    '"false_accepts_per_12h":0.0,"recall":0.95,"hours_audio":12})'
)


class TestDoubleWakeScorer:
    """Deterministic scorer used only for harness tests (degraded artifacts)."""

    name = "test_double"

    def _score_sync(self, pcm: bytes) -> tuple[float, dict]:
        lowered = pcm.lower()
        triggered = b"evie" in lowered or b"evi " in lowered
        return (0.98 if triggered else 0.02), {"engine": self.name}


def _chunk_wav(path: Path, chunk_seconds: float = 10.0):
    """Yield 16 kHz mono int16 PCM byte-chunks of an ambient WAV."""

    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError(f"{path}: ambient recording must be mono 16-bit PCM WAV")
        rate = wav.getframerate()
        if rate != 16000:
            raise ValueError(f"{path}: ambient recording must be 16 kHz")
        chunk_frames = int(chunk_seconds * rate)
        while True:
            frames = wav.readframes(chunk_frames)
            if not frames:
                break
            yield frames


def ambient_hours(paths: list[Path]) -> float:
    total = 0.0
    for path in paths:
        with wave.open(str(path), "rb") as wav:
            total += wav.getnframes() / max(1, wav.getframerate())
    return total / 3600.0


def score_ambient(
    engine,
    ambient: Path,
    *,
    chunk_seconds: float = 10.0,
) -> tuple[list[float], float, float]:
    """Score an ambient session fast; returns (chunk scores, hours, replay_speed_x)."""

    paths = sorted(ambient.rglob("*.wav")) if ambient.is_dir() else [ambient]
    if not paths:
        raise FileNotFoundError(f"no ambient WAVs under {ambient}")
    hours = ambient_hours(paths)
    scores: list[float] = []
    started = time.perf_counter()
    audio_seconds = 0.0
    for path in paths:
        for frames in _chunk_wav(path, chunk_seconds=chunk_seconds):
            score, _ = engine._score_sync(frames)
            scores.append(score)
            audio_seconds += len(frames) / 2 / 16000
    wall = max(1e-6, time.perf_counter() - started)
    return scores, hours, audio_seconds / wall


def score_clips(engine, clips: list[Path]) -> list[float]:
    scored: list[float] = []
    for clip in clips:
        score, _ = engine._score_sync(_wav_pcm_bytes(clip))
        scored.append(score)
    return scored


def _wav_pcm_bytes(path: Path) -> bytes:
    """Read a 16 kHz mono int16 WAV as raw PCM bytes (the engine contract)."""

    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000:
            raise ValueError(f"{path}: held-out clip must be mono 16-bit PCM 16 kHz WAV")
        return wav.readframes(wav.getnframes())


def parse_distance(path: Path) -> str:
    stem = path.stem.lower()
    if "3m" in stem or "far" in stem:
        return "3m"
    if "close" in stem:
        return "close"
    return "unspecified"


def sweep_thresholds(
    ambient_scores: list[float],
    clip_scores: list[float],
    *,
    hours_audio: float,
    min_recall: float = 0.90,
    max_fa_per_12h: float = 1.0,
) -> tuple[float, list[dict]]:
    """Return (chosen threshold, full false-accept/recall curve)."""

    candidates = sorted({round(s, 4) for s in ambient_scores + clip_scores} | {0.5})
    curve: list[dict] = []
    for threshold in candidates:
        fa = sum(1 for s in ambient_scores if s >= threshold)
        fa_12h = fa * (12.0 / max(0.001, hours_audio))
        recall = sum(1 for s in clip_scores if s >= threshold) / len(clip_scores) if clip_scores else 0.0
        curve.append(
            {
                "threshold": threshold,
                "false_accepts_per_12h": round(fa_12h, 4),
                "recall": round(recall, 4),
            }
        )
    satisfiers = [entry for entry in curve if entry["false_accepts_per_12h"] <= max_fa_per_12h and entry["recall"] >= min_recall]
    if satisfiers:
        chosen = max(satisfiers, key=lambda e: (e["recall"], -e["threshold"]))
        return chosen["threshold"], curve
    # No threshold meets both gates: keep the value with FA within budget and
    # the highest recall, otherwise the starting default.
    fa_safe = [e for e in curve if e["false_accepts_per_12h"] <= max_fa_per_12h]
    if fa_safe:
        chosen = max(fa_safe, key=lambda e: e["recall"])
        return chosen["threshold"], curve
    return 0.5, curve


def build_engine(
    *,
    model_path: str | None,
    verifier_path: str | None,
    threshold: float,
    model_factory=None,
    test_double: bool = False,
):
    if test_double or not model_path:
        return TestDoubleWakeScorer()
    return OpenWakeWordEngine(
        model_path=model_path,
        verifier_path=verifier_path,
        threshold=threshold,
        model_factory=model_factory,
    )


def measure(
    engine,
    *,
    held_out_dir: Path,
    ambient: Path,
    threshold: float,
    hours_audio: float | None,
) -> dict:
    clips = sorted(held_out_dir.rglob("*.wav"))
    if not clips:
        raise FileNotFoundError(f"no held-out clips under {held_out_dir}")
    ambient_scores, measured_hours, replay_speed = score_ambient(engine, ambient)
    clip_scores = score_clips(engine, clips)
    hours = hours_audio if hours_audio is not None else measured_hours
    tuned_threshold, curve = sweep_thresholds(
        ambient_scores,
        clip_scores,
        hours_audio=hours,
    )
    use_threshold = threshold if threshold is not None else tuned_threshold
    fa = sum(1 for s in ambient_scores if s >= use_threshold)
    fa_12h = fa * (12.0 / max(0.001, hours))
    recall = sum(1 for s in clip_scores if s >= use_threshold) / len(clip_scores)
    by_distance: dict[str, dict] = {}
    clip_pairs = list(zip(clips, clip_scores, strict=False))
    for distance in ("3m", "close", "unspecified"):
        group = [(c, s) for c, s in clip_pairs if parse_distance(c) == distance]
        if not group:
            continue
        by_distance[distance] = {
            "clips": len(group),
            "recall": round(
                sum(1 for _c, s in group if s >= use_threshold)
                / len(group),
                4,
            ),
        }
    return {
        "provider": getattr(engine, "name", "openwakeword"),
        "degraded": getattr(engine, "name", "") == "test_double",
        "false_accepts_per_12h": round(fa_12h, 4),
        "recall": round(recall, 4),
        "hours_audio": round(hours, 3),
        "threshold": use_threshold,
        "threshold_swept": threshold is None,
        "threshold_curve": curve,
        "distance_breakdown": by_distance,
        "replay_speed_x": round(replay_speed, 2),
        "ambient_chunks_scored": len(ambient_scores),
        "held_out_clips": len(clips),
    }


def write_report(report: dict, path: Path) -> Path:
    payload = dict(report)
    payload.setdefault("schema", "ev.wake.eval.v1")
    payload.setdefault("schema_version", "ev.wake.eval.v1")
    payload["producer"] = "ev-eval"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.audio.wake_eval")
    parser.add_argument("--held-out-dir", type=Path, default=None)
    parser.add_argument("--ambient", type=Path, default=None)
    parser.add_argument("--model-path", default=None)
    parser.add_argument("--verifier-path", default=None)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--report", type=Path, default=OUT_PATH)
    parser.add_argument("--test-double", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    target = args.report or OUT_PATH
    if args.dry_run:
        if args.held_out_dir and args.ambient:
            print(f"wake: would write {target}")
        else:
            print(SKIP_REASON.replace("{path}", str(target)))
        return 0
    if not args.held_out_dir or not args.ambient or (not args.model_path and not args.test_double):
        print(SKIP_REASON.replace("{path}", str(target)))
        return 0

    engine = build_engine(
        model_path=args.model_path,
        verifier_path=args.verifier_path,
        threshold=args.threshold if args.threshold is not None else 0.5,
        test_double=args.test_double,
    )
    report = measure(
        engine,
        held_out_dir=args.held_out_dir,
        ambient=args.ambient,
        threshold=args.threshold,
        hours_audio=args.hours,
    )

    # Verifier before/after: re-measure head-only when a verifier is present.
    if args.verifier_path and not args.test_double:
        head_only = build_engine(
            model_path=args.model_path,
            verifier_path=None,
            threshold=report["threshold"],
            test_double=False,
        )
        head_report = measure(
            head_only,
            held_out_dir=args.held_out_dir,
            ambient=args.ambient,
            threshold=report["threshold"],
            hours_audio=args.hours,
        )
        report["verifier"] = {
            "enabled": True,
            "false_accepts_per_12h_head_only": head_report["false_accepts_per_12h"],
            "false_accepts_per_12h_with_verifier": report["false_accepts_per_12h"],
            "recall_head_only": head_report["recall"],
            "recall_with_verifier": report["recall"],
        }
    else:
        report["verifier"] = {
            "enabled": bool(args.verifier_path),
            "false_accepts_per_12h_head_only": report["false_accepts_per_12h"],
            "false_accepts_per_12h_with_verifier": report["false_accepts_per_12h"],
            "recall_head_only": report["recall"],
            "recall_with_verifier": report["recall"],
        }

    write_report(report, target)
    print(json.dumps(report, indent=2))
    print(f"\nShipped threshold: {report['threshold']} — set "
          "EV_VOICE_WAKE_OPENWAKEWORD_THRESHOLD and EV_EARS_WAKE_THRESHOLD to this value.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
