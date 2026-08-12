"""mAP-proxy spot-check harness for the on-device object detector.

The acceptance gate is ``detector >= 0.35 mAP-proxy on a 50-image spot
check``. This module provides the reproducible metric (COCO-style IoU=0.5
matching, per-class AP, mean AP) and a COCO-JSON loader. It runs against any
detector returned by ``create_detector``; with the offline double it reports
``degraded=True`` and mAP 0 rather than fabricating a score.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IOU_THRESHOLD = 0.5


@dataclass
class SpotCheckResult:
    map_score: float
    per_class: dict[str, dict[str, Any]] = field(default_factory=dict)
    degraded: bool = True
    engine: str = "deterministic"
    images: int = 0


def _iou(a: list[float], b: list[float]) -> float:
    ax1, ay1 = float(a[0]), float(a[1])
    aw, ah = float(a[2]), float(a[3])
    bx1, by1 = float(b[0]), float(b[1])
    bw, bh = float(b[2]), float(b[3])
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax1 + aw, bx1 + bw), min(ay1 + ah, by1 + bh)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _average_precision(scored: list[tuple[float, bool]], total_gt: int) -> float:
    if total_gt == 0:
        return 0.0
    if not scored:
        return 0.0
    tp = 0
    fp = 0
    best_precision_at_recall: dict[float, float] = {}
    for _, is_tp in sorted(scored, key=lambda item: item[0], reverse=True):
        tp += int(is_tp)
        fp += int(not is_tp)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / total_gt
        best_precision_at_recall[round(recall, 6)] = max(
            best_precision_at_recall.get(round(recall, 6), 0.0),
            precision,
        )
    # All-point interpolation: AP is the mean precision over recall steps.
    recall_points = sorted(best_precision_at_recall)
    if not recall_points:
        return 0.0
    total = 0.0
    previous_recall = 0.0
    for recall in recall_points:
        total += best_precision_at_recall[recall] * (recall - previous_recall)
        previous_recall = recall
    total += best_precision_at_recall[recall_points[-1]] * (1.0 - previous_recall)
    return total


async def run_spot_check(
    detector: Any,
    images: list[tuple[bytes, list[dict]]],
    *,
    iou_threshold: float = IOU_THRESHOLD,
) -> SpotCheckResult:
    """Run a detector over (image bytes, COCO-style GT) pairs and score mAP."""

    per_class: dict[int, dict[str, Any]] = {}
    for _, image_annotations in images:
        for annotation in image_annotations:
            class_id = int(annotation["class_id"])
            entry = per_class.setdefault(
                class_id,
                {"tp": 0, "fp": 0, "fn": 0, "scores": [], "gt_boxes": []},
            )
            entry["gt_boxes"].append(list(annotation["bbox"]))

    for data, image_annotations in images:
        result = await detector.detect(data, "image/png")
        predictions = result.objects
        gt_by_class: dict[int, list[list[float]]] = {}
        for annotation in image_annotations:
            class_id = int(annotation["class_id"])
            gt_by_class.setdefault(class_id, []).append(list(annotation["bbox"]))
        matched: set[tuple[int, int]] = set()
        for prediction in sorted(
            predictions,
            key=lambda item: float(item.get("confidence") or 0.0),
            reverse=True,
        ):
            class_id = int(prediction.get("class_id", -1))
            pred_box = [
                float(prediction["bounding_box"]["x"]),
                float(prediction["bounding_box"]["y"]),
                float(prediction["bounding_box"]["width"]),
                float(prediction["bounding_box"]["height"]),
            ]
            best_index = -1
            best_iou = iou_threshold
            for gt_index, gt_box in enumerate(gt_by_class.get(class_id, [])):
                if (class_id, gt_index) in matched:
                    continue
                overlap = _iou(pred_box, gt_box)
                if overlap >= best_iou:
                    best_iou = overlap
                    best_index = gt_index
            is_tp = best_index >= 0
            if is_tp:
                matched.add((class_id, best_index))
            entry = per_class.setdefault(
                class_id,
                {"tp": 0, "fp": 0, "fn": 0, "scores": [], "gt_boxes": []},
            )
            entry["scores"].append(
                (float(prediction.get("confidence") or 0.0), is_tp)
            )
            if is_tp:
                entry["tp"] += 1
            else:
                entry["fp"] += 1

    class_notes: dict[str, dict[str, Any]] = {}
    ap_values: list[float] = []
    for class_id, entry in sorted(per_class.items()):
        total_gt = len(entry["gt_boxes"])
        entry["fn"] = total_gt - entry["tp"]
        ap = _average_precision(entry["scores"], total_gt)
        ap_values.append(ap)
        class_notes[str(class_id)] = {
            "ap": round(ap, 4),
            "tp": entry["tp"],
            "fp": entry["fp"],
            "fn": entry["fn"],
            "gt": total_gt,
        }
    map_score = sum(ap_values) / len(ap_values) if ap_values else 0.0
    degraded = bool(getattr(detector, "name", "") == "deterministic")
    return SpotCheckResult(
        map_score=round(map_score, 4),
        per_class=class_notes,
        degraded=degraded,
        engine=getattr(detector, "name", "unknown"),
        images=len(images),
    )


def load_coco_spotcheck(directory: str | Path) -> list[tuple[bytes, list[dict]]]:
    """Load a COCO-style spot-check folder (annotations.json + images/)."""

    directory = Path(directory)
    with (directory / "annotations.json").open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    id_to_name: dict[int, str] = {
        int(category["id"]): category["name"]
        for category in payload.get("categories", [])
    }
    image_files = {
        int(image["id"]): directory / image["file_name"]
        for image in payload.get("images", [])
    }
    annotations_by_image: dict[int, list[dict]] = {}
    for annotation in payload.get("annotations", []):
        annotations_by_image.setdefault(int(annotation["image_id"]), []).append(
            {
                "class_id": int(annotation["category_id"]),
                "class_name": id_to_name.get(int(annotation["category_id"]), "unknown"),
                "bbox": annotation["bbox"],
            }
        )
    images: list[tuple[bytes, list[dict]]] = []
    for image_id, path in sorted(image_files.items()):
        if not path.exists():
            continue
        images.append((path.read_bytes(), annotations_by_image.get(image_id, [])))
    return images


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.vision.eval")
    parser.add_argument(
        "--dir",
        required=True,
        help="COCO-style spot-check folder (annotations.json + image files)",
    )
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    return parser


async def _run(directory: str) -> SpotCheckResult:
    images = load_coco_spotcheck(directory)
    if not images:
        raise ValueError(f"no spot-check images found under {directory}")
    from app.vision.detect import create_detector

    detector = create_detector()
    return await run_spot_check(detector, images)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = asyncio.run(_run(args.dir))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"spot-check failed: {exc}", file=sys.stderr)
        return 2
    if result.degraded:
        print(
            "spot-check skipped: detector is the deterministic double (weights "
            "not registered/downloaded). No mAP score was fabricated.",
            file=sys.stderr,
        )
        return 2
    if args.json:
        print(
            json.dumps(
                {
                    "map_score": result.map_score,
                    "per_class": result.per_class,
                    "degraded": result.degraded,
                    "engine": result.engine,
                    "images": result.images,
                },
                indent=2,
            )
        )
    else:
        print(f"mAP-proxy: {result.map_score:.4f} over {result.images} images")
        for class_id, note in sorted(result.per_class.items()):
            print(
                f"  class {class_id}: AP {note['ap']:.4f} "
                f"(tp={note['tp']} fp={note['fp']} fn={note['fn']} gt={note['gt']})"
            )
    return 0 if result.map_score >= 0.35 else 1


if __name__ == "__main__":
    sys.exit(main())
