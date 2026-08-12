"""Open-vocabulary scene labels + image embeddings (MobileCLIP-S0 class).

When the model or runtime is absent the honest double returns NO labels and
NO embedding with ``degraded=True``. Embeddings are produced on-device and
may be stored for future visual search; labels are suggestions that still go
through the human-confirmation flow.

License note: MobileCLIP *weights* are under Apple's ML Research Model
license (see docs/VISION.md); the code license differs. YOLOv8/YOLO11 are
AGPL-3.0 and deliberately not used.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_SCENE_CANDIDATES: list[str] = [
    "person", "workbench", "laptop", "computer screen", "document", "paper",
    "meeting room", "kitchen", "food", "receipt", "invoice", "passport",
    "prescription", "menu", "outdoor", "indoor", "vehicle", "animal", "phone",
    "book", "desk", "whiteboard", "plant", "coffee", "office", "home",
]

_LABEL_SIMILARITY_FLOOR = 0.2

# Pluggable candidate-text encoder. Production needs the MobileCLIP text tower
# (Foundry registry entry); until then, scene labels are NOT fabricated.
ENCODE_CANDIDATES_HOOK: Callable[[list[str], Any, str], list[list[float]]] | None = None


@dataclass
class SceneResult:
    labels: list[dict]
    embedding: list[float] | None
    degraded: bool
    engine: str


class DeterministicSceneEncoder:
    """Offline double: no labels, no embedding, honest degradation."""

    name = "deterministic"

    async def encode_scene(
        self,
        data: bytes,
        candidates: list[str] | None = None,
        content_type: str | None = None,
    ) -> SceneResult:
        return SceneResult(labels=[], embedding=None, degraded=True, engine=self.name)


class OnnxSceneEncoder:
    """Real MobileCLIP-class image encoder with optional text scoring."""

    name = "onnx"

    def __init__(
        self,
        session: Any,
        *,
        text_session: Any = None,
        model_name: str = "scene-mobileclip-s0",
    ) -> None:
        self.session = session
        self.text_session = text_session
        self.model_name = model_name

    async def encode_scene(
        self,
        data: bytes,
        candidates: list[str] | None = None,
        content_type: str | None = None,
    ) -> SceneResult:
        return await asyncio.to_thread(
            self._encode_sync,
            data,
            candidates or DEFAULT_SCENE_CANDIDATES,
        )

    def _encode_sync(self, data: bytes, candidates: list[str]) -> SceneResult:
        inputs = _preprocess_image(data)
        outputs = self.session.run(None, inputs)
        embedding = _normalize(_first_vector(outputs))
        labels: list[dict] = []
        if self.text_session is not None and ENCODE_CANDIDATES_HOOK is not None:
            try:
                text_embeddings = ENCODE_CANDIDATES_HOOK(
                    candidates,
                    self.text_session,
                    self.model_name,
                )
                labels = _score_candidates(embedding, text_embeddings, candidates)
            except Exception:  # noqa: BLE001 - labels are optional suggestions
                labels = []
        return SceneResult(
            labels=labels,
            embedding=embedding,
            degraded=False,
            engine=self.name,
        )


def _preprocess_image(data: bytes, size: int = 256) -> dict[str, Any]:
    import io

    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        converted = image.convert("RGB").resize((size, size))
        array = np.asarray(converted, dtype=np.float32) / 255.0
    array = array.transpose(2, 0, 1)[None, ...]
    return {"pixel_values": array}


def _first_vector(outputs: list[Any]) -> list[float]:
    if not outputs:
        return []
    value = outputs[0]
    if hasattr(value, "tolist"):
        value = value.tolist()
    flat: list[float] = []

    def walk(item: Any) -> None:
        if isinstance(item, (int, float)):
            flat.append(float(item))
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(value)
    return flat


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 0:
        return vector
    return [round(v / norm, 6) for v in vector]


def _score_candidates(
    image_embedding: list[float],
    text_embeddings: list[list[float]],
    candidates: list[str],
) -> list[dict]:
    labels: list[dict] = []
    for candidate, text_vector in zip(candidates, text_embeddings, strict=False):
        similarity = sum(a * b for a, b in zip(image_embedding, text_vector, strict=False))
        if similarity < _LABEL_SIMILARITY_FLOOR:
            continue
        labels.append(
            {
                "label": candidate,
                "confidence": round(max(0.0, min(1.0, similarity)), 3),
            }
        )
    return labels


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


def create_scene_encoder(
    engine: str = "auto",
    *,
    session: Any = None,
    text_session: Any = None,
    model_name: str = "scene-mobileclip-s0",
) -> DeterministicSceneEncoder | OnnxSceneEncoder:
    """Real factory: ONNX when available, honest deterministic double otherwise."""

    if engine not in {"auto", "onnx", "double"}:
        raise ValueError(f"unknown scene engine {engine!r}")
    if session is not None:
        return OnnxSceneEncoder(session, text_session=text_session, model_name=model_name)
    if engine == "double":
        return DeterministicSceneEncoder()
    if engine in {"auto", "onnx"}:
        try:
            import numpy  # noqa: F401
            import onnxruntime  # noqa: F401

            real = _open_session(model_name)
            if real is not None:
                return OnnxSceneEncoder(real, model_name=model_name)
        except Exception:  # noqa: BLE001 - offline CI has no weights
            pass
    return DeterministicSceneEncoder()
