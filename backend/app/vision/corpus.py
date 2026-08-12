"""Deterministic 50-image detection spot-check corpus (license-free).

The detector acceptance gate (``detector >= 0.35 mAP-proxy on a 50-image
spot check, with per-class notes``) needs a reproducible, self-contained
corpus that works on a laptop with no downloads. This module renders 50
synthetic COCO-style images from a fixed seed: abstract filled shapes on
noisy backgrounds, each with ground-truth bounding boxes in normalized
coordinates (the same space the detector returns).

The corpus is deliberately abstract and license-free (no third-party photos,
no celebrity/stranger pixels). It measures detection matching/geometry, not
photorealism; a real-photo COCO spot check can be dropped into the same
harness later by pointing ``run_spot_check`` at any COCO-JSON folder.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

CORPUS_SIZE = 50
DEFAULT_IMAGE_SIZE = 320
CORPUS_CLASSES = [
    "person",
    "car",
    "dog",
    "chair",
    "bottle",
    "laptop",
    "book",
    "cup",
    "potted plant",
    "tv",
]


def _coerce_rgb(value: Any) -> tuple[int, int, int]:
    """Accept either (r, g, b) or a 24-bit int and return a valid RGB tuple."""

    if isinstance(value, tuple):
        r, g, b = (int(channel) & 0xFF for channel in value)
    else:
        value = int(value) & 0xFFFFFF
        r = (value >> 16) & 0xFF
        g = (value >> 8) & 0xFF
        b = value & 0xFF
    return (r, g, b)


def _region_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    return inter / min(area_a, area_b) if min(area_a, area_b) > 0 else 0.0


def _fill_mask(
    kind: str,
    cx: float,
    cy: float,
    w: float,
    h: float,
    image_size: int,
) -> Any:
    """Boolean pixel mask for one abstract shape (numpy vectorized)."""

    import numpy as np

    x0 = max(0.0, cx - w / 2)
    y0 = max(0.0, cy - h / 2)
    x1 = min(1.0, cx + w / 2)
    y1 = min(1.0, cy + h / 2)
    xs = (np.arange(image_size) + 0.5) / image_size
    ys = (np.arange(image_size) + 0.5) / image_size
    px, py = np.meshgrid(xs, ys)
    inside_box = (px >= x0) & (px <= x1) & (py >= y0) & (py <= y1)
    if kind == "rect":
        return inside_box
    if kind == "circle":
        radius = min(w, h) / 2
        center_x = x0 + w / 2
        center_y = y0 + h / 2
        return (px - center_x) ** 2 + (py - center_y) ** 2 <= radius * radius
    if kind == "triangle":
        return _inside_triangle_mask(px, py, cx, y0, x0, y1, x1, y1)
    if kind == "diamond":
        dx = np.abs(px - cx) / max(w / 2, 1e-6)
        dy = np.abs(py - cy) / max(h / 2, 1e-6)
        return (dx + dy <= 1.0) & inside_box
    return inside_box


def _inside_triangle_mask(
    px: Any,
    py: Any,
    ax: float,
    ay: float,
    bx: float,
    by: float,
    vx: float,
    vy: float,
) -> Any:
    """Vectorized barycentric point-in-triangle test."""

    def sign(x1: Any, y1: Any, x2: float, y2: float, x3: float, y3: float) -> Any:
        return (x1 - x3) * (y2 - y3) - (x2 - x3) * (y1 - y3)

    d1 = sign(px, py, ax, ay, bx, by)
    d2 = sign(px, py, bx, by, vx, vy)
    d3 = sign(px, py, vx, vy, ax, ay)
    has_negative = (d1 < 0) | (d2 < 0) | (d3 < 0)
    has_positive = (d1 > 0) | (d2 > 0) | (d3 > 0)
    return ~(has_negative & has_positive)


def generate_corpus(
    directory: str | Path,
    *,
    size: int = CORPUS_SIZE,
    image_size: int = DEFAULT_IMAGE_SIZE,
    seed: int = 0xE7E5,
) -> dict[str, Any]:
    """Render the spot-check corpus and its COCO JSON.

    Returns the same payload that is written to ``annotations.json`` so tests
    can assert on it without re-reading the filesystem.
    """

    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - pillow is Agent 2's dep
        raise RuntimeError(
            "pillow is required to generate the spot-check corpus (DEP REQUEST: Agent 2)"
        ) from exc
    try:
        import numpy as np
    except Exception as exc:  # pragma: no cover - numpy is the ml extra
        raise RuntimeError(
            "numpy is required to generate the spot-check corpus (DEP REQUEST: Agent 2)"
        ) from exc

    directory = Path(directory)
    images_dir = directory / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    images: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    annotation_id = 1
    class_ids = {name: index for index, name in enumerate(CORPUS_CLASSES)}

    for image_id in range(1, size + 1):
        noise_rng = np.random.default_rng((seed, image_id))
        base = _coerce_rgb(rng.randint(0x101010, 0x2A2A2A))
        image_array = np.clip(
            np.asarray(base, dtype=np.int16)
            + noise_rng.integers(-12, 13, size=(image_size, image_size, 3)),
            0,
            255,
        ).astype(np.uint8)

        object_count = rng.randint(1, 3)
        used_regions: list[tuple[float, float, float, float]] = []
        for _ in range(object_count):
            class_name = rng.choice(CORPUS_CLASSES)
            w = rng.uniform(0.12, 0.34)
            h = rng.uniform(0.12, 0.34)
            for _attempt in range(24):
                cx = rng.uniform(0.16, 0.84)
                cy = rng.uniform(0.16, 0.84)
                box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
                if all(_region_overlap(box, other) < 0.18 for other in used_regions):
                    break
            used_regions.append(box)
            color = _coerce_rgb(rng.randint(0x303030, 0xF0F0F0))
            x0 = max(0.0, cx - w / 2)
            y0 = max(0.0, cy - h / 2)
            x1 = min(1.0, cx + w / 2)
            y1 = min(1.0, cy + h / 2)
            kind = rng.choice(["rect", "circle", "triangle", "diamond"])
            mask = _fill_mask(kind, cx, cy, w, h, image_size)
            image_array[mask] = color

            bw = round(x1 - x0, 4)
            bh = round(y1 - y0, 4)
            annotations.append(
                {
                    "id": annotation_id,
                    "image_id": image_id,
                    "category_id": class_ids[class_name],
                    "bbox": [round(x0, 4), round(y0, 4), bw, bh],
                    "area": round(bw * bh, 6),
                    "iscrowd": 0,
                }
            )
            annotation_id += 1

        file_name = f"images/img_{image_id:03d}.png"
        image = Image.fromarray(image_array, mode="RGB")
        image.save(images_dir / Path(file_name).name, format="PNG")
        images.append(
            {
                "id": image_id,
                "file_name": file_name,
                "width": image_size,
                "height": image_size,
            }
        )

    payload = {
        "info": {
            "description": (
                "EV Agent 6 synthetic detector spot-check corpus; "
                f"{size} images, abstract license-free shapes, seed={seed}"
            ),
            "version": "1.0.0",
        },
        "categories": [
            {"id": index, "name": name} for name, index in class_ids.items()
        ],
        "images": images,
        "annotations": annotations,
    }
    with (directory / "annotations.json").open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return payload


def corpus_sha256(directory: str | Path) -> str:
    """Stable digest of the corpus (PNG bytes + annotations JSON)."""

    directory = Path(directory)
    digest = hashlib.sha256()
    with (directory / "annotations.json").open("rb") as handle:
        digest.update(handle.read())
    for path in sorted((directory / "images").glob("*.png")):
        digest.update(path.read_bytes())
    return digest.hexdigest()
