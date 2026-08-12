"""CLI entry point for the 50-image detector mAP-proxy acceptance gate.

Usage (after Agent 2 pins ``detect-rtdetr-nano`` and ``app.ml.cli pull`` has
downloaded it):

    cd backend
    uv run python -m app.vision.spotcheck [CORPUS_DIR] [--engine auto|onnx|double]

Without a pinned model the honest deterministic double runs and reports
``degraded: true`` with mAP 0.0 — never a fabricated score.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from app.vision.corpus import generate_corpus
from app.vision.detect import create_detector
from app.vision.eval import load_coco_spotcheck, run_spot_check


async def _run(directory: Path, engine: str) -> dict:
    if not (directory / "annotations.json").exists():
        generate_corpus(directory)
    images = load_coco_spotcheck(directory)
    if not images:
        raise SystemExit(f"no corpus images found in {directory}")
    detector = create_detector(engine=engine)
    result = await run_spot_check(detector, images)
    return {
        "engine": result.engine,
        "degraded": result.degraded,
        "images": result.images,
        "map_proxy": result.map_score,
        "gate": "detector >= 0.35 mAP-proxy",
        "gate_met": (not result.degraded) and result.map_score >= 0.35,
        "per_class": result.per_class,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus_dir", nargs="?", type=Path, default=None)
    parser.add_argument(
        "--engine",
        choices=("auto", "onnx", "double"),
        default="auto",
    )
    args = parser.parse_args()
    directory = args.corpus_dir
    if directory is None:
        directory = Path(tempfile.gettempdir()) / "evvision-spotcheck-corpus"
    directory = directory.resolve()
    payload = asyncio.run(_run(directory, args.engine))
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
