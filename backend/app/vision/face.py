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
        return FaceDetectionResult(
            faces=_parse_yunet(outputs),
            degraded=False,
            engine=self.name,
        )


def _preprocess_image(data: bytes, size: int = 320) -> dict[str, Any]:
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        converted = image.convert("RGB").resize((size, size))
        array = np.asarray(converted, dtype=np.float32)
        array = array[:, :, ::-1]  # YuNet expects BGR
    array = array.transpose(2, 0, 1)[None, ...]
    return {"input": array}


def _to_list(value: Any) -> Any:
    return value.tolist() if hasattr(value, "tolist") else value


def _parse_yunet(outputs: list[Any]) -> list[dict]:
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
