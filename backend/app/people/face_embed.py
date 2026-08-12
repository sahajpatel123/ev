"""Face embedding providers for AGENT 7 ROSTER.

ROSTER consumes **aligned crops** produced by Agent 6's YuNet detector; it
deliberately has no detector of its own. Two providers exist:

* ``hash`` — deterministic development/test embeddings (degraded=True). It is
  never passed off as face recognition.
* ``sface`` — OpenCV Zoo SFace ONNX (Apache-2.0, LFW 0.9940, 37 MB). The
  embedding dimension is read from the model output at load time (128 for the
  2021dec ONNX). Loaded per-use through the ModelArbiter on-demand slot and
  evicted after inference so it is never resident alongside ASR. When weights
  are absent it degrades to the deterministic double with ``degraded=True``.
"""

from __future__ import annotations

import base64
import hashlib
import math
from binascii import Error as B64Error
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.config import settings
from app.ml.settings import get_ml_settings
from app.people.errors import FaceError


@dataclass(frozen=True)
class FaceCrop:
    """One aligned face crop (Agent 6 YuNet output) plus detector metadata."""

    image_b64: str
    quality: float | None = None
    confidence: float | None = None
    source: str = "photo"
    attachment_id: str | None = None
    live_event_id: str | None = None


@dataclass(frozen=True)
class FaceEmbeddingResult:
    """One embedding plus honest provenance/quality metadata."""

    embedding: list[float]
    quality: float | None
    confidence: float | None
    provider: str
    degraded: bool
    model: str | None = None


class FaceEmbedder(Protocol):
    name: str
    embedding_dim: int
    degraded: bool

    async def embed(self, crop: FaceCrop) -> FaceEmbeddingResult: ...


def normalize(values: list[float]) -> list[float]:
    """L2-normalize a vector in place semantics (returns a new list)."""
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"Embedding dimension mismatch: {len(a)} != {len(b)}")
    return sum(x * y for x, y in zip(a, b, strict=True))


def _decode_image_bytes(image_b64: str) -> bytes:
    try:
        return base64.b64decode(image_b64, validate=True)
    except (B64Error, ValueError) as exc:
        raise FaceError(
            "image_b64 must be valid base64",
            status=400,
            code="invalid_image_data",
        ) from exc


def _byte_entropy(raw: bytes) -> float:
    """Deterministic byte-entropy proxy (0..1), NOT a face-quality score."""
    if not raw:
        return 0.0
    counts = [0] * 256
    for byte in raw:
        counts[byte] += 1
    entropy = 0.0
    for count in counts:
        if count == 0:
            continue
        probability = count / len(raw)
        entropy -= probability * math.log2(probability)
    return min(1.0, entropy / 8.0)


def _deterministic_embedding(raw: bytes, dim: int = 512) -> list[float]:
    """Signed hash-bucket embedding over 128-byte windows (dev/test only)."""
    vector = [0.0] * dim
    for offset in range(0, len(raw), 128):
        chunk = raw[offset : offset + 128]
        digest = hashlib.sha256(chunk).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    return normalize(vector)


class HashFaceEmbedder:
    """Deterministic development/test embedder (degraded=True, never faked)."""

    name = "hash"
    embedding_dim = 512
    degraded = True

    async def embed(self, crop: FaceCrop) -> FaceEmbeddingResult:
        raw = _decode_image_bytes(crop.image_b64)
        embedding = _deterministic_embedding(raw, self.embedding_dim)
        quality = crop.quality if crop.quality is not None else _byte_entropy(raw)
        return FaceEmbeddingResult(
            embedding=embedding,
            quality=quality,
            confidence=crop.confidence,
            provider=self.name,
            degraded=True,
            model="hash",
        )


class SFaceOnnxEmbedder:
    """OpenCV Zoo SFace ONNX via onnxruntime (Apache-2.0).

    The model is loaded per-use under the ModelArbiter on-demand slot and
    evicted after inference. If onnxruntime, cv2, or the weights are missing,
    ``embed`` returns the deterministic double with ``degraded=True`` instead
    of failing or fabricating a result. The embedding dimension is read from
    the actual ONNX output at load time (128 for the 2021dec model), never
    assumed.
    """

    name = "sface"
    degraded = False
    input_size = 112

    def __init__(
        self,
        *,
        model_path: str | None = None,
        session=None,
        arbiter=None,
    ) -> None:
        self.model_path = model_path or settings.face_model_path
        self._session = session
        self._arbiter = arbiter
        self._loaded = session is not None
        self.embedding_dim = 512  # nominal until the model output is known

    def _resolve_path(self) -> Path:
        if self.model_path:
            return Path(self.model_path)
        return Path(get_ml_settings().ml_model_dir) / "face-sface.onnx"

    def _load(self) -> bool:
        if self._loaded:
            return self._session is not None
        try:
            import onnxruntime as ort
        except ImportError:
            return False
        try:
            import numpy as np  # noqa: F401  (used by the session input)
        except ImportError:
            return False
        path = self._resolve_path()
        if not path.is_file():
            return False
        try:
            from app.ml.arbiter import create_default_arbiter

            if self._arbiter is None:
                self._arbiter = create_default_arbiter()
            with self._arbiter.acquire("face-sface"):
                self._session = ort.InferenceSession(
                    str(path),
                    providers=ort.get_available_providers(),
                )
            outputs = self._session.get_outputs()
            if outputs and len(outputs[0].shape) >= 2:
                self.embedding_dim = int(outputs[0].shape[1])
            self._loaded = True
            return True
        except Exception:
            self._session = None
            self._loaded = False
            return False

    def close(self) -> None:
        self._session = None
        self._loaded = False
        if self._arbiter is not None:
            with suppress(KeyError):
                self._arbiter.evict("face-sface")

    async def embed(self, crop: FaceCrop) -> FaceEmbeddingResult:
        raw = _decode_image_bytes(crop.image_b64)
        if not self._load():
            fallback = _deterministic_embedding(raw, self.embedding_dim)
            return FaceEmbeddingResult(
                embedding=fallback,
                quality=crop.quality if crop.quality is not None else _byte_entropy(raw),
                confidence=crop.confidence,
                provider=self.name,
                degraded=True,
                model="hash-fallback",
            )
        try:
            embedding = await self._embed_real(raw)
            self.embedding_dim = len(embedding)
            self.close()
            return FaceEmbeddingResult(
                embedding=embedding,
                quality=crop.quality,
                confidence=crop.confidence,
                provider=self.name,
                degraded=False,
                model=f"sface-{len(embedding)}",
            )
        except Exception:
            self.close()
            fallback = _deterministic_embedding(raw, self.embedding_dim)
            return FaceEmbeddingResult(
                embedding=fallback,
                quality=crop.quality if crop.quality is not None else _byte_entropy(raw),
                confidence=crop.confidence,
                provider=self.name,
                degraded=True,
                model="hash-fallback",
            )

    async def _embed_real(self, raw: bytes) -> list[float]:
        import cv2
        import numpy as np

        if self._session is None:
            raise RuntimeError("SFace session not loaded")
        image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Image could not be decoded as a color image")
        resized = cv2.resize(
            image,
            (self.input_size, self.input_size),
            interpolation=cv2.INTER_AREA,
        )
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(rgb, (2, 0, 1))[np.newaxis, ...].astype(np.float32)
        input_name = self._session.get_inputs()[0].name
        output = self._session.run(None, {input_name: blob})[0]
        embedding = np.asarray(output).reshape(-1).astype(np.float64).tolist()
        return normalize(embedding)


def get_face_embedder() -> FaceEmbedder:
    """Resolve the configured face embedder (hash default, sface when enabled)."""
    if (settings.face_provider or "hash").lower() == "sface":
        return SFaceOnnxEmbedder(model_path=settings.face_model_path)
    return HashFaceEmbedder()
