"""Face DETECTION only — boxes, landmarks, aligned crops.

Identity (who a face belongs to) is owned by Agent 7 (``app/ev/people``) and
is NEVER computed here. This module produces no embeddings of identity and
stores no names.
"""

from __future__ import annotations

import asyncio
import io
import math
from dataclasses import dataclass
from typing import Any

_FACE_SCORE_THRESHOLD = 0.5
_FACE_NMS_THRESHOLD = 0.3
# The 2023mar ONNX has a fixed 640x640 input and emits cls/obj/bbox/kps for
# strides 8/16/32 (12 outputs). Decoding follows OpenCV's FaceDetectorYN
# reference exactly: score = sqrt(cls * obj), boxes/landmarks relative to the
# cell anchor, exp() for width/height.
_YUNET_INPUT_SIZE = 640
_YUNET_STRIDES = (8, 16, 32)
_YUNET_MAX_FACES = 32


@dataclass
class FaceDetectionResult:
    faces: list[dict]
    degraded: bool
    engine: str


class DeterministicFaceDetector:
    """Offline double: no faces, no fabricated detections."""

    name = "deterministic"

    async def detect(
        self,
        data: bytes,
        content_type: str | None = None,
    ) -> FaceDetectionResult:
        return FaceDetectionResult(faces=[], degraded=True, engine=self.name)


class OnnxFaceDetector:
    """YuNet ONNX face detector (OpenCV Zoo, Apache-2.0)."""

    name = "onnx"

    def __init__(self, session: Any, *, model_name: str = "face-yunet") -> None:
        self.session = session
        self.model_name = model_name

    async def detect(
        self,
        data: bytes,
        content_type: str | None = None,
    ) -> FaceDetectionResult:
        return await asyncio.to_thread(self._detect_sync, data)

    def _detect_sync(self, data: bytes) -> FaceDetectionResult:
        inputs = _preprocess_image(data)
        outputs = self.session.run(None, inputs)
        names: list[str] | None = None
        try:
            names = [output.name for output in self.session.get_outputs()]
        except Exception:  # noqa: BLE001 - stubs may not expose output metadata
            names = None
        return FaceDetectionResult(
            faces=_parse_yunet(outputs, names),
            degraded=False,
            engine=self.name,
        )


def _preprocess_image(data: bytes, size: int = _YUNET_INPUT_SIZE) -> dict[str, Any]:
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        converted = image.convert("RGB").resize((size, size))
        array = np.asarray(converted, dtype=np.float32)
        array = array[:, :, ::-1]  # 2023mar ONNX expects BGR, 0-255
    array = array.transpose(2, 0, 1)[None, ...]
    return {"input": array}


def _to_list(value: Any) -> Any:
    return value.tolist() if hasattr(value, "tolist") else value


def _parse_yunet(outputs: list[Any], names: list[str] | None = None) -> list[dict]:
    """Decode YuNet 2023mar multi-output heads, or a legacy single tensor."""

    if not outputs:
        return []
    if names and any(name.startswith("cls_") for name in names):
        return _parse_yunet_multi(outputs, names)
    return _parse_yunet_single(outputs)


def _parse_yunet_multi(outputs: list[Any], names: list[str]) -> list[dict]:
    import numpy as np

    by_name = {name: output for name, output in zip(names, outputs, strict=False)}
    raw: list[tuple[float, float, float, float, float, list[tuple[float, float]]]] = []
    for stride in _YUNET_STRIDES:
        try:
            cls = np.asarray(by_name[f"cls_{stride}"], dtype=np.float32).reshape(-1)
            obj = np.asarray(by_name[f"obj_{stride}"], dtype=np.float32).reshape(-1)
            bbox = np.asarray(by_name[f"bbox_{stride}"], dtype=np.float32).reshape(-1, 4)
            kps = np.asarray(by_name[f"kps_{stride}"], dtype=np.float32).reshape(-1, 10)
        except KeyError:
            continue
        cols = _YUNET_INPUT_SIZE // stride
        score = np.sqrt(np.clip(cls, 0.0, 1.0) * np.clip(obj, 0.0, 1.0))
        for index in np.flatnonzero(score >= _FACE_SCORE_THRESHOLD):
            row, col = divmod(int(index), cols)
            cx = (col + float(bbox[index, 0])) * stride
            cy = (row + float(bbox[index, 1])) * stride
            width = math.exp(float(bbox[index, 2])) * stride
            height = math.exp(float(bbox[index, 3])) * stride
            if width <= 1.0 or height <= 1.0:
                continue
            landmarks = [
                (
                    (float(kps[index, i * 2]) + col) * stride,
                    (float(kps[index, i * 2 + 1]) + row) * stride,
                )
                for i in range(5)
            ]
            raw.append((cx - width / 2, cy - height / 2, width, height, float(score[index]), landmarks))
    return _yunet_faces(raw, _YUNET_INPUT_SIZE)


def _yunet_faces(
    raw: list[tuple[float, float, float, float, float, list[tuple[float, float]]]],
    size: int,
) -> list[dict]:
    raw.sort(key=lambda item: item[4], reverse=True)
    kept: list[tuple[float, float, float, float, float, list[tuple[float, float]]]] = []
    for candidate in raw:
        if all(_iou_px(candidate, prior) <= _FACE_NMS_THRESHOLD for prior in kept):
            kept.append(candidate)
            if len(kept) >= _YUNET_MAX_FACES:
                break

    faces: list[dict] = []
    for x, y, width, height, score, landmarks in kept:
        # YuNet landmark order: right eye, left eye, nose, right mouth, left mouth.
        right_eye, left_eye = landmarks[0], landmarks[1]
        angle = math.degrees(
            math.atan2(left_eye[1] - right_eye[1], left_eye[0] - right_eye[0])
        )
        faces.append(
            {
                "bounding_box": {
                    "x": round(max(0.0, x / size), 4),
                    "y": round(max(0.0, y / size), 4),
                    "width": round(max(0.0, width / size), 4),
                    "height": round(max(0.0, height / size), 4),
                },
                "landmarks": [
                    {"x": round(point[0] / size, 4), "y": round(point[1] / size, 4)}
                    for point in landmarks
                ],
                "alignment_angle": round(angle, 3),
                "score": round(score, 3),
            }
        )
    return faces


def _iou_px(
    a: tuple[float, float, float, float, float, list[tuple[float, float]]],
    b: tuple[float, float, float, float, float, list[tuple[float, float]]],
) -> float:
    ax1, ay1, ax2, ay2 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx1, by1, bx2, by2 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _parse_yunet_single(outputs: list[Any]) -> list[dict]:
    if not outputs:
        return []
    try:
        tensor = _to_list(outputs[0])
        if not isinstance(tensor, list) or not tensor:
            return []
        rows = tensor[0] if isinstance(tensor[0], list) else tensor
        faces: list[dict] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 15:
                continue
            score = float(row[14])
            if score < _FACE_SCORE_THRESHOLD:
                continue
            x, y, width, height = (float(v) for v in row[:4])
            landmarks = [
                {"x": float(row[4 + i * 2]), "y": float(row[5 + i * 2])}
                for i in range(5)
            ]
            left_eye, right_eye = landmarks[0], landmarks[1]
            angle = math.degrees(
                math.atan2(
                    right_eye["y"] - left_eye["y"],
                    right_eye["x"] - left_eye["x"],
                )
            )
            faces.append(
                {
                    "bounding_box": {
                        "x": round(max(0.0, x), 4),
                        "y": round(max(0.0, y), 4),
                        "width": round(max(0.0, width), 4),
                        "height": round(max(0.0, height), 4),
                    },
                    "landmarks": [
                        {"x": round(float(p["x"]), 4), "y": round(float(p["y"]), 4)}
                        for p in landmarks
                    ],
                    "alignment_angle": round(angle, 3),
                    "score": round(score, 3),
                }
            )
        return faces
    except (IndexError, TypeError, ValueError):
        return []


def aligned_crop(data: bytes, face: dict) -> bytes | None:
    """Return a rotation-normalized face crop as PNG bytes (Agent 7 consumer).

    Requires Pillow; returns None when Pillow is unavailable or the crop
    cannot be produced. This is DETECTION output only — no identity.
    """

    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - optional dependency
        return None
    try:
        box = face.get("bounding_box") or {}
        landmarks = face.get("landmarks") or []
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            x = max(0, int(box.get("x", 0) * width))
            y = max(0, int(box.get("y", 0) * height))
            w = max(1, int(box.get("width", 0) * width))
            h = max(1, int(box.get("height", 0) * height))
            crop = image.convert("RGB").crop((x, y, min(width, x + w), min(height, y + h)))
            if len(landmarks) >= 2:
                angle = -float(face.get("alignment_angle") or 0)
                crop = crop.rotate(angle, expand=True, resample=Image.Resampling.BILINEAR)
            buffer = io.BytesIO()
            crop.save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception:  # noqa: BLE001 - optional crop helper
        return None


def _model_path(model_name: str) -> Any:
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


def create_face_detector(
    engine: str = "auto",
    *,
    session: Any = None,
    model_name: str = "face-yunet",
) -> DeterministicFaceDetector | OnnxFaceDetector:
    """Real factory: YuNet ONNX when available, honest double otherwise."""

    if engine not in {"auto", "onnx", "double"}:
        raise ValueError(f"unknown face engine {engine!r}")
    if session is not None:
        return OnnxFaceDetector(session, model_name=model_name)
    if engine == "double":
        return DeterministicFaceDetector()
    if engine in {"auto", "onnx"}:
        try:
            import numpy  # noqa: F401
            import onnxruntime  # noqa: F401

            real = _open_session(model_name)
            if real is not None:
                return OnnxFaceDetector(real, model_name=model_name)
        except Exception:  # noqa: BLE001 - offline CI has no weights
            pass
    return DeterministicFaceDetector()
