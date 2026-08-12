"""On-device COCO object detection.

The preferred engine is a nano RT-DETR-class ONNX detector (permissive
Apache-2.0; YOLOv8/YOLO11 are AGPL-3.0 and deliberately not used). When the
model or ONNX Runtime is absent, the honest deterministic double returns NO
objects with ``degraded=True`` — never fabricated boxes or confidence.

Model weights are loaded only through the ML arbiter (Agent 2's registry);
a missing registry entry degrades to the double.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

COCO_CLASSES: list[str] = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]

_NMS_IOU_THRESHOLD = 0.45
_NMS_SCORE_THRESHOLD = 0.35


@dataclass
class DetectionResult:
    objects: list[dict]
    degraded: bool
    engine: str


class DeterministicDetector:
    """Offline double: no real detection, no fabricated outputs."""

    name = "deterministic"

    async def detect(
        self,
        data: bytes,
        content_type: str | None = None,
    ) -> DetectionResult:
        return DetectionResult(objects=[], degraded=True, engine=self.name)


class OnnxDetector:
    """Real ONNX detector (RT-DETR-style or YOLO-style outputs)."""

    name = "onnx"

    def __init__(self, session: Any, *, model_name: str = "detect-rtdetr-nano") -> None:
        self.session = session
        self.model_name = model_name

    async def detect(
        self,
        data: bytes,
        content_type: str | None = None,
    ) -> DetectionResult:
        return await asyncio.to_thread(self._detect_sync, data)

    def _detect_sync(self, data: bytes) -> DetectionResult:
        inputs = _preprocess_image(data)
        outputs = self.session.run(None, inputs)
        return DetectionResult(
            objects=_parse_detections(outputs),
            degraded=False,
            engine=self.name,
        )


def _preprocess_image(data: bytes, size: int = 640) -> dict[str, Any]:
    """Pillow + numpy preprocessing for real ONNX detectors."""

    import io

    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        converted = image.convert("RGB").resize((size, size))
        array = np.asarray(converted, dtype=np.float32) / 255.0
    array = array.transpose(2, 0, 1)[None, ...]
    return {"images": array}


def _to_list(value: Any) -> Any:
    return value.tolist() if hasattr(value, "tolist") else value


def _parse_detections(outputs: list[Any]) -> list[dict]:
    """Parse RT-DETR-style or YOLO-style ONNX outputs into normalized boxes."""

    if not outputs:
        return []
    if len(outputs) >= 4:
        parsed = _parse_rtdetr(outputs)
        if parsed:
            return parsed
    return _parse_yolo(outputs[0])


def _parse_rtdetr(outputs: list[Any]) -> list[dict]:
    try:
        num = int(_to_list(outputs[0])[0])
        boxes = _to_list(outputs[1])[0][:num]
        scores = _to_list(outputs[2])[0][:num]
        classes = _to_list(outputs[3])[0][:num]
    except (IndexError, TypeError, ValueError):
        return []
    detections: list[dict] = []
    for box, score, class_id in zip(boxes, scores, classes, strict=False):
        if float(score) < _NMS_SCORE_THRESHOLD:
            continue
        x1, y1, x2, y2 = (float(v) for v in box[:4])
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        detections.append(
            {
                "label": COCO_CLASSES[int(class_id)]
                if 0 <= int(class_id) < len(COCO_CLASSES)
                else f"class-{int(class_id)}",
                "confidence": round(max(0.0, min(1.0, float(score))), 3),
                "bounding_box": {
                    "x": round(max(0.0, x1), 4),
                    "y": round(max(0.0, y1), 4),
                    "width": round(max(0.0, x2 - x1), 4),
                    "height": round(max(0.0, y2 - y1), 4),
                },
                "class_id": int(class_id),
            }
        )
    return detections


def _parse_yolo(output: Any) -> list[dict]:
    try:
        tensor = _to_list(output)
        if not isinstance(tensor, list) or not tensor:
            return []
        if isinstance(tensor[0], list) and isinstance(tensor[0][0], list):
            tensor = tensor[0]
        # tensor: [4 + num_classes, anchors]
        num_classes = len(tensor) - 4
        if num_classes < 1:
            return []
        anchors = len(tensor[0])
        boxes: list[tuple[float, float, float, float, float, int]] = []
        for anchor in range(anchors):
            cx = float(tensor[0][anchor]) / 640.0
            cy = float(tensor[1][anchor]) / 640.0
            w = float(tensor[2][anchor]) / 640.0
            h = float(tensor[3][anchor]) / 640.0
            scores = [float(tensor[c + 4][anchor]) for c in range(num_classes)]
            class_id = max(range(num_classes), key=lambda c: scores[c])
            score = scores[class_id]
            if score < _NMS_SCORE_THRESHOLD:
                continue
            boxes.append((cx, cy, w, h, score, class_id))
        kept = _nms(boxes)
        detections: list[dict] = []
        for cx, cy, w, h, score, class_id in kept:
            detections.append(
                {
                    "label": COCO_CLASSES[class_id]
                    if 0 <= class_id < len(COCO_CLASSES)
                    else f"class-{class_id}",
                    "confidence": round(score, 3),
                    "bounding_box": {
                        "x": round(max(0.0, cx - w / 2), 4),
                        "y": round(max(0.0, cy - h / 2), 4),
                        "width": round(max(0.0, w), 4),
                        "height": round(max(0.0, h), 4),
                    },
                    "class_id": class_id,
                }
            )
        return detections
    except (IndexError, TypeError, ValueError):
        return []


def _nms(
    boxes: list[tuple[float, float, float, float, float, int]],
) -> list[tuple[float, float, float, float, float, int]]:
    kept: list[tuple[float, float, float, float, float, int]] = []
    ordered = sorted(boxes, key=lambda item: item[4], reverse=True)
    while ordered:
        best = ordered.pop(0)
        kept.append(best)
        ordered = [
            other
            for other in ordered
            if not (other[5] == best[5] and _iou(best, other) > _NMS_IOU_THRESHOLD)
        ]
    return kept


def _iou(
    a: tuple[float, float, float, float, float, int],
    b: tuple[float, float, float, float, float, int],
) -> float:
    ax1, ay1 = a[0] - a[2] / 2, a[1] - a[3] / 2
    ax2, ay2 = a[0] + a[2] / 2, a[1] + a[3] / 2
    bx1, by1 = b[0] - b[2] / 2, b[1] - b[3] / 2
    bx2, by2 = b[0] + b[2] / 2, b[1] + b[3] / 2
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def _model_path(model_name: str) -> Any:
    """Return the arbiter-managed model path, or None when unavailable."""

    try:
        from app.ml.arbiter import create_default_arbiter
        from app.ml.settings import get_ml_settings
        from app.ml.store import target_path

        ml_settings = get_ml_settings()
        arbiter = create_default_arbiter(ml_settings)
        spec = arbiter.registry.get(model_name)
        path = target_path(ml_settings, spec)
        return path if path.exists() else None
    except Exception:  # noqa: BLE001 - registry entry may not exist yet
        return None


def _open_session(model_name: str) -> Any:
    """Open an ONNX session through the arbiter; None when unavailable."""

    path = _model_path(model_name)
    if path is None:
        return None
    try:
        import onnxruntime

        from app.ml.arbiter import create_default_arbiter
        from app.ml.settings import get_ml_settings

        arbiter = create_default_arbiter(get_ml_settings())
        with arbiter.acquire(model_name):
            return onnxruntime.InferenceSession(
                str(path),
                providers=["CPUExecutionProvider"],
            )
    except Exception:  # noqa: BLE001 - refuse to load outside the arbiter
        return None


def create_detector(
    engine: str = "auto",
    *,
    session: Any = None,
    model_name: str = "detect-rtdetr-nano",
) -> DeterministicDetector | OnnxDetector:
    """Real factory: ONNX when available, honest deterministic double otherwise."""

    if engine not in {"auto", "onnx", "double"}:
        raise ValueError(f"unknown detector engine {engine!r}")
    if session is not None:
        return OnnxDetector(session, model_name=model_name)
    if engine == "double":
        return DeterministicDetector()
    if engine in {"auto", "onnx"}:
        try:
            import numpy  # noqa: F401
            import onnxruntime  # noqa: F401

            real = _open_session(model_name)
            if real is not None:
                return OnnxDetector(real, model_name=model_name)
        except Exception:  # noqa: BLE001 - offline CI has no weights
            pass
    return DeterministicDetector()
