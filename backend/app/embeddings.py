"""Embedding providers for EV retrieval.

``HashEmbeddingProvider`` is the deterministic offline fallback (dev/test
only). ``HTTPEmbeddingProvider`` talks to any OpenAI-compatible /embeddings
endpoint. ``ONNXEmbeddingProvider`` runs local encoder models (granite R2,
Qwen3) through ONNX Runtime behind the ModelArbiter, degrading to the
deterministic hash double when weights or runtimes are absent.

Model-version law: vectors from different embedding models are never
comparable. Every provider exposes ``model_version`` and the re-embed job
records it on each memory row so mixed-model vectors are detectable and
migration is a tracked, deliberate operation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
from sqlalchemy import Table, bindparam, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ml import store
from app.ml.arbiter import ModelArbiter
from app.ml.registry import ModelRegistry, ModelSpec, ModelTier
from app.ml.settings import MLSettings, get_ml_settings
from app.models import Memory
from app.utils.text import utcnow

EMBEDDING_MODEL_HASH = "hash"
EMBEDDING_MODEL_GRANITE = "granite-embedding-97m-multilingual-r2"
EMBEDDING_MODEL_QWEN3 = "qwen3-embedding-0.6b"

GRANITE_SOURCE_URL = (
    "https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2/"
    "resolve/main/onnx/model_quint8_avx2.onnx"
)
GRANITE_TOKENIZER_URL = (
    "https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2/"
    "resolve/main/tokenizer.json"
)
GRANITE_POOLING_URL = (
    "https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2/"
    "resolve/main/1_Pooling/config.json"
)

QWEN3_SOURCE_URL = (
    "https://huggingface.co/janni-t/qwen3-embedding-0.6b-int8-tei-onnx/resolve/main/"
    "model.onnx"
)
QWEN3_TOKENIZER_URL = (
    "https://huggingface.co/janni-t/qwen3-embedding-0.6b-int8-tei-onnx/resolve/main/"
    "tokenizer.json"
)

# Companion artifacts (tokenizer, pooling config) downloaded next to the main
# ONNX weight. Keys are the relative repo filenames; values are resolved URLs.
# Checksums are pinned once a maintainer verifies the artifact (FLEET_LAW §9).
MODEL_COMPANION_URLS: dict[str, dict[str, str]] = {
    EMBEDDING_MODEL_GRANITE: {
        "tokenizer.json": GRANITE_TOKENIZER_URL,
        "1_Pooling/config.json": GRANITE_POOLING_URL,
    },
    EMBEDDING_MODEL_QWEN3: {
        "tokenizer.json": QWEN3_TOKENIZER_URL,
    },
}

# sha256 pins for companion files, filled after checksum verification.
MODEL_COMPANION_SHA256: dict[str, dict[str, str]] = {
    EMBEDDING_MODEL_GRANITE: {
        "tokenizer.json": "4f2842d568e2724370aec203652a42ac783c7937f8347a1a2cc7506d71f1582f",
        "1_Pooling/config.json": "8bc5c9a40814fcf48d2fbe7cfeff4bee6736c3c2a823ba0ce098985c59d12ab7",
    },
    EMBEDDING_MODEL_QWEN3: {},
}


def embedding_model_specs() -> list[ModelSpec]:
    """ModelArbiter entries owned by Agent 8 (SYNAPSE).

    granite R2 (verified: 97M params, native 384-dim output, 32K context,
    Apache-2.0, MTEB multilingual retrieval 60.3) replaces all-MiniLM as the
    always-resident embedder. The ONNX file is ~100 MB, but ONNX Runtime's
    ModernBERT graph measures ~450 MB resident on this Apple M2 (measured
    2026-08-11), so ``resident_mb`` is honest. Agent 2 (budget owner) resolved
    the always-tier conflict by registering granite as ``on_demand`` (460 MB
    fits the 600 MB slot) — mirrored here.
    Qwen3-Embedding-0.6B is the opt-in quality tier (native 1024-dim,
    Matryoshka-truncated to 384 by this module) and rides the on-demand slot.

    sha256 pins are intentionally None (seed entries): the store refuses to
    download until a maintainer pins the checksum, per FLEET_LAW §9.
    """

    return [
        ModelSpec(
            name=EMBEDDING_MODEL_GRANITE,
            task="embedding",
            source_url=GRANITE_SOURCE_URL,
            sha256="a6022dd8220ea6f6595562a1328ee216f4a94faa55362f2f4747c80f1e78772e",
            disk_mb=100,
            resident_mb=460,
            peak_mb=520,
            tier=ModelTier.ON_DEMAND,
            license="Apache-2.0",
            license_url="https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2",
            version="r2",
            verified=True,
        ),
        ModelSpec(
            name=EMBEDDING_MODEL_QWEN3,
            task="embedding",
            source_url=QWEN3_SOURCE_URL,
            sha256=None,
            disk_mb=614,
            resident_mb=614,
            peak_mb=700,
            tier=ModelTier.ON_DEMAND,
            license="Apache-2.0",
            license_url="https://huggingface.co/Qwen/Qwen3-Embedding-0.6B",
            version="0.6b",
            verified=False,
        ),
    ]


def create_embedding_arbiter(
    ml_settings: MLSettings | None = None,
) -> ModelArbiter:
    """Arbiter whose registry includes the Agent 8 embedding roster."""

    ml_settings = ml_settings or get_ml_settings()
    registry = ModelRegistry(exclusive_limit_mb=ml_settings.ml_exclusive_limit_mb)
    for spec in embedding_model_specs():
        registry.register(spec)
    return ModelArbiter(registry, ml_settings)


def _l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def truncate_matryoshka(
    vector: Sequence[float],
    dim: int,
) -> list[float]:
    """Matryoshka truncation: first ``dim`` dimensions, re-L2-normalized."""

    if len(vector) <= dim:
        return _l2_normalize(vector)
    return _l2_normalize(vector[:dim])


class HashEmbeddingProvider:
    """Deterministic, offline bag-of-hashes embedding (dev/test only)."""

    name = "hash"
    model_version = EMBEDDING_MODEL_HASH
    degraded = False

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def ensure_ready(self) -> bool:
        return True

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for token in text.lower().split():
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                idx = int.from_bytes(digest[:4], "big") % self.dim
                vec[idx] += 1.0
            vectors.append(_l2_normalize(vec))
        return vectors


class HTTPEmbeddingProvider:
    """OpenAI-compatible embeddings endpoint (dedicated embedding model)."""

    name = "http"
    degraded = False

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        dim: int,
        send_dimensions: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.send_dimensions = send_dimensions
        self.model_version = f"http:{model}"

    def ensure_ready(self) -> bool:
        return True

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload: dict = {"model": self.model, "input": list(texts)}
        if self.send_dimensions:
            payload["dimensions"] = self.dim
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/embeddings",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda item: item["index"])
        vectors = [item["embedding"] for item in data]
        for index, vector in enumerate(vectors):
            if len(vector) != self.dim:
                raise EmbeddingDimensionError(
                    f"hosted embedder {self.model!r} returned {len(vector)}-dim "
                    f"vector for input {index}; expected {self.dim}. Refusing to "
                    "store or compare vectors from a different dimension than "
                    "EV_EMBEDDING_DIM."
                )
        return vectors


class EmbeddingDimensionError(RuntimeError):
    """An embedder returned vectors whose dimension differs from the configured one."""


class ModelWeightsMissing(RuntimeError):
    """A registered model's weights/tokenizer are not cached locally."""


class ONNXEmbeddingProvider:
    """Local ONNX Runtime encoder behind the ModelArbiter.

    Degrades to the deterministic hash double (``degraded=True``) when the
    runtime, tokenizer library, or weights are absent, so offline CI and an
    8 GB host never crash and never silently mix vector spaces.
    """

    name = "onnx"

    def __init__(
        self,
        *,
        model_name: str,
        model_version: str,
        dim: int,
        pooling: str = "mean",
        max_length: int = 32768,
        batch_size: int = 16,
        matryoshka_dim: int | None = None,
        ml_settings: MLSettings | None = None,
        arbiter: ModelArbiter | None = None,
    ) -> None:
        self.model_name = model_name
        self.model_version = model_version
        self.dim = dim
        self.pooling = pooling
        self.max_length = max_length
        self.batch_size = batch_size
        self.matryoshka_dim = matryoshka_dim
        self.ml_settings = ml_settings or get_ml_settings()
        self.arbiter = arbiter or create_embedding_arbiter(self.ml_settings)
        self._session: Any = None
        self._tokenizer: Any = None
        self._input_names: list[str] = []
        self._output_names: list[str] = []
        self._load_error: str | None = None
        self._lock = threading.Lock()
        self._degraded = True

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _spec(self) -> ModelSpec:
        for spec in embedding_model_specs():
            if spec.name == self.model_name:
                return spec
        raise KeyError(f"no registered embedding model named {self.model_name!r}")

    def _tokenizer_path(self) -> Path:
        return Path(self.ml_settings.ml_model_dir) / f"{self.model_name}__tokenizer.json"

    def _pooling_config_path(self) -> Path:
        return Path(self.ml_settings.ml_model_dir) / f"{self.model_name}__1_Pooling_config.json"

    def _providers(self) -> list[str]:
        try:
            import onnxruntime as ort

            available = set(ort.get_available_providers())
        except Exception:
            return []
        preferred = getattr(settings, "embedding_onnx_provider", "auto")
        if preferred and preferred != "auto":
            candidates = [name.strip() for name in preferred.split(",") if name.strip()]
            chosen = [name for name in candidates if name in available]
            if chosen:
                return chosen
        if "CPUExecutionProvider" in available:
            return ["CPUExecutionProvider"]
        return list(available)

    def _ensure_ready(self) -> bool:
        if (
            self._session is not None
            and self._tokenizer is not None
            and self.arbiter.is_resident(self.model_name)
        ):
            self._degraded = False
            return True
        with self._lock:
            if (
                self._session is not None
                and self._tokenizer is not None
                and self.arbiter.is_resident(self.model_name)
            ):
                self._degraded = False
                return True
            try:
                self._load()
            except Exception as exc:  # noqa: BLE001 - degraded path is the contract
                self._session = None
                self._tokenizer = None
                self._load_error = str(exc)
                self._degraded = True
                return False
            self._degraded = False
            return True

    def ensure_ready(self) -> bool:
        return self._ensure_ready()

    def _load(self) -> None:
        try:
            import onnxruntime as ort  # noqa: F401
        except Exception as exc:
            raise ModelWeightsMissing(
                "onnxruntime not installed; install the 'ml' extra "
                "(DEP REQUEST: Agent 2)"
            ) from exc
        try:
            from tokenizers import Tokenizer  # type: ignore[import-not-found]
        except Exception as exc:
            raise ModelWeightsMissing(
                "'tokenizers' not installed; lazy dependency requested from Agent 2"
            ) from exc

        spec = self._spec()
        model_path = store.target_path(self.ml_settings, spec)
        tokenizer_path = self._tokenizer_path()
        if not model_path.exists():
            raise ModelWeightsMissing(
                f"embedding weights for {self.model_name} not cached at {model_path}; "
                "run the pinned download first (see docs/RETRIEVAL.md)"
            )
        if not tokenizer_path.exists():
            raise ModelWeightsMissing(
                f"tokenizer.json for {self.model_name} not cached at {tokenizer_path}"
            )

        with self.arbiter.acquire(self.model_name):
            session_options = ort.SessionOptions()
            session_options.graph_optimization_level = (
                ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
            )
            if hasattr(session_options, "enable_cpu_mem_arena"):
                session_options.enable_cpu_mem_arena = False
            with suppress(AttributeError):
                session_options.intra_op_num_threads = max(
                    1, min(4, (os.cpu_count() or 4))
                )
            session = ort.InferenceSession(
                str(model_path),
                sess_options=session_options,
                providers=self._providers(),
            )
            tokenizer = Tokenizer.from_file(str(tokenizer_path))
            tokenizer.enable_truncation(max_length=self.max_length)

        self._session = session
        self._tokenizer = tokenizer
        self._input_names = [inp.name for inp in session.get_inputs()]
        self._output_names = [out.name for out in session.get_outputs()]
        self._load_error = None
        self._read_pooling_config()

    def _read_pooling_config(self) -> None:
        path = self._pooling_config_path()
        if not path.exists():
            return
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if config.get("pooling_mode_lasttoken"):
            self.pooling = "last_token"
        elif config.get("pooling_mode_cls_token"):
            self.pooling = "cls"
        elif config.get("pooling_mode_mean_tokens"):
            self.pooling = "mean"

    def _pool_hidden(
        self,
        hidden: Any,
        attention_mask: Any,
    ) -> Any:
        import numpy as np

        if self.pooling == "last_token":
            last = np.asarray(attention_mask).sum(axis=1) - 1
            rows = np.arange(hidden.shape[0])
            return hidden[rows, last, :]
        if self.pooling == "cls":
            return hidden[:, 0, :]
        mask = np.asarray(attention_mask)[..., None].astype(hidden.dtype)
        summed = (hidden * mask).sum(axis=1)
        counts = np.maximum(mask.sum(axis=1), 1)
        return summed / counts

    def _extract_embedding(
        self,
        outputs: Sequence[Any],
        attention_mask: Any,
    ) -> Any:
        import numpy as np

        # Prefer a 2-D output whose width looks like an embedding.
        candidates: list[tuple[Any, str]] = []
        for name, output in zip(self._output_names, outputs, strict=False):
            arr = np.asarray(output)
            if arr.ndim == 2 and arr.shape[0] > 0:
                candidates.append((arr, name))
        if candidates:
            def _prefer(item: tuple[Any, str]) -> int:
                arr, name = item
                lowered = name.lower()
                if "embedding" in lowered or "sentence" in lowered or "dense" in lowered:
                    return 0
                return 1

            candidates.sort(key=_prefer)
            return candidates[0][0]
        for _name, output in zip(self._output_names, outputs, strict=False):
            arr = np.asarray(output)
            if arr.ndim == 3:
                return self._pool_hidden(arr, attention_mask)
        raise RuntimeError(f"cannot interpret ONNX outputs for {self.model_name}: {self._output_names}")

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        tokenizer = self._tokenizer
        if tokenizer.padding is None:
            tokenizer.enable_padding()
        encodings = tokenizer.encode_batch(texts)
        if not encodings:
            return []
        input_ids = np.asarray([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.asarray([e.attention_mask for e in encodings], dtype=np.int64)
        feeds: dict[str, Any] = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.asarray(
                [e.type_ids for e in encodings],
                dtype=np.int64,
            )
        outputs = self._session.run(None, feeds)
        vectors = self._extract_embedding(outputs, attention_mask)
        if self.matryoshka_dim is not None and vectors.shape[-1] > self.matryoshka_dim:
            vectors = vectors[:, : self.matryoshka_dim]
        if vectors.shape[-1] != self.dim:
            raise EmbeddingDimensionError(
                f"local embedder {self.model_name!r} produced {vectors.shape[-1]}-dim "
                f"vectors; expected {self.dim}. Refusing to store or compare "
                "incompatible vectors."
            )
        norm = np.linalg.norm(vectors, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        vectors = vectors / norm
        return vectors.tolist()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batch = list(texts)
        if not batch:
            return []
        if not self._ensure_ready():
            self._degraded = True
            return await HashEmbeddingProvider(dim=self.dim).embed(batch)
        self._degraded = False
        vectors: list[list[float]] = []
        for start in range(0, len(batch), self.batch_size):
            chunk = batch[start : start + self.batch_size]
            vectors.extend(await asyncio.to_thread(self._embed_sync, chunk))
        return vectors


class GraniteEmbeddingProvider(ONNXEmbeddingProvider):
    """granite-embedding-97m-multilingual-r2 (native 384-dim, Apache-2.0)."""

    def __init__(
        self,
        *,
        ml_settings: MLSettings | None = None,
        arbiter: ModelArbiter | None = None,
    ) -> None:
        super().__init__(
            model_name=EMBEDDING_MODEL_GRANITE,
            model_version=EMBEDDING_MODEL_GRANITE,
            dim=384,
            pooling="mean",
            max_length=32768,
            batch_size=16,
            ml_settings=ml_settings,
            arbiter=arbiter,
        )


class Qwen3EmbeddingProvider(ONNXEmbeddingProvider):
    """Qwen3-Embedding-0.6B (Apache-2.0) with Matryoshka truncation.

    Native output is 1024-dim; this module truncates to the configured
    embedding dimension (default 384) and re-normalizes, so it rides the same
    column as granite with no schema migration.
    """

    def __init__(
        self,
        *,
        matryoshka_dim: int = 384,
        ml_settings: MLSettings | None = None,
        arbiter: ModelArbiter | None = None,
    ) -> None:
        super().__init__(
            model_name=EMBEDDING_MODEL_QWEN3,
            model_version=EMBEDDING_MODEL_QWEN3,
            dim=matryoshka_dim,
            pooling="last_token",
            max_length=32768,
            batch_size=16,
            matryoshka_dim=matryoshka_dim,
            ml_settings=ml_settings,
            arbiter=arbiter,
        )


def get_embedder():
    """Factory used by every writer/retriever in the app (offline-safe)."""

    key = (
        settings.embedding_provider,
        settings.embedding_base_url,
        settings.embedding_api_key,
        settings.embedding_model,
        settings.embedding_dim,
        settings.embedding_onnx_provider,
        settings.embedding_http_dimensions,
    )
    with _embedder_cache_lock:
        cached = _embedder_cache.get(key)
        if cached is not None:
            return cached
        if settings.embedding_provider == "http":
            if not settings.embedding_base_url:
                raise RuntimeError(
                    "EV_EMBEDDING_BASE_URL is required for http embedding provider"
                )
            provider = HTTPEmbeddingProvider(
                base_url=settings.embedding_base_url,
                api_key=settings.embedding_api_key,
                model=settings.embedding_model,
                dim=settings.embedding_dim,
                send_dimensions=settings.embedding_http_dimensions,
            )
        elif settings.embedding_provider == "granite":
            provider = GraniteEmbeddingProvider()
        elif settings.embedding_provider == "qwen3":
            provider = Qwen3EmbeddingProvider(matryoshka_dim=settings.embedding_dim)
        else:
            provider = HashEmbeddingProvider(dim=settings.embedding_dim)
        _embedder_cache[key] = provider
        return provider


# ONNX sessions are expensive to construct (~1-3 s each); retrieval/writers
# call get_embedder() per operation, so cache providers per configuration.
_embedder_cache: dict = {}
_embedder_cache_lock = threading.Lock()


def ensure_embedding_weights(
    *,
    model_name: str = EMBEDDING_MODEL_GRANITE,
    ml_settings: MLSettings | None = None,
) -> list[Path]:
    """Checksum-verified download of a model's ONNX weight + companions.

    Only runs for entries whose sha256 pins are set; seed entries raise so a
    maintainer must pin checksums first (FLEET_LAW §9).
    """

    ml_settings = ml_settings or get_ml_settings()
    spec = next(s for s in embedding_model_specs() if s.name == model_name)
    if spec.sha256 is None:
        raise RuntimeError(
            f"model {model_name!r} is a seed entry: pin its sha256 before downloading"
        )
    paths = [store.download_model(spec, ml_settings)]
    for rel_name, url in MODEL_COMPANION_URLS.get(model_name, {}).items():
        target = Path(ml_settings.ml_model_dir) / f"{model_name}__{rel_name.replace('/', '_')}"
        expected = MODEL_COMPANION_SHA256.get(model_name, {}).get(rel_name)
        if expected is None:
            raise RuntimeError(
                f"companion {rel_name!r} for {model_name!r} has no sha256 pin"
            )
        _download_verified(url, target, expected)
        paths.append(target)
    return paths


def _download_verified(url: str, target: Path, expected_sha256: str) -> Path:
    if target.exists() and store.sha256_file(target) == expected_sha256:
        return target
    partial = target.with_name(f"{target.name}.part")
    with store.open_source(url) as chunks, partial.open("wb") as out:
        for chunk in chunks:
            out.write(chunk)
    actual = store.sha256_file(partial)
    if actual != expected_sha256:
        partial.unlink(missing_ok=True)
        raise store.ChecksumError(
            f"sha256 mismatch for {target.name}: expected {expected_sha256}, got {actual}"
        )
    import os

    os.replace(partial, target)
    return target


@dataclass
class ReembedReport:
    model_version: str
    total: int
    skipped: int
    embedded: int
    failed: int
    interrupted: bool
    elapsed_s: float
    docs_per_s: float
    degraded: bool
    started_at: str
    completed_at: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "model_version": self.model_version,
            "total": self.total,
            "skipped": self.skipped,
            "embedded": self.embedded,
            "failed": self.failed,
            "interrupted": self.interrupted,
            "elapsed_s": round(self.elapsed_s, 3),
            "docs_per_s": round(self.docs_per_s, 2),
            "degraded": self.degraded,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "errors": self.errors[:50],
        }


async def reembed_all_memories(
    session: AsyncSession,
    *,
    batch_size: int = 32,
    max_rows: int | None = None,
    require_real: bool = True,
    on_progress: Callable[[dict], None] | None = None,
) -> ReembedReport:
    """Resumable, batched re-embed of every current, non-redacted memory.

    Rows already stamped with the provider's model version are skipped, so an
    interrupted run (Ctrl-C, crash) resumes exactly where it stopped: each
    batch commits independently. ``max_rows`` intentionally bounds a run for
    interruption tests.
    """

    provider = get_embedder()
    target = getattr(provider, "model_version", EMBEDDING_MODEL_HASH)
    ensure_ready = getattr(provider, "ensure_ready", None)
    if ensure_ready is not None:
        ready = await asyncio.to_thread(ensure_ready)
    else:
        ready = True
    degraded = not ready
    if degraded and require_real and target != EMBEDDING_MODEL_HASH:
        raise RuntimeError(
            "refusing to re-embed with a degraded provider: "
            f"{target!r} is selected but its weights are unavailable; "
            "install the weights first (see docs/RETRIEVAL.md)"
        )

    started = time.perf_counter()
    started_at = utcnow().isoformat(timespec="seconds")
    total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Memory)
                .where(Memory.is_current.is_(True), Memory.redacted.is_(False))
            )
        ).scalar_one()
    )
    rows = list(
        (
            await session.execute(
                select(Memory.id, Memory.text, Memory.embedding_model_version)
                .where(Memory.is_current.is_(True), Memory.redacted.is_(False))
                .order_by(Memory.id)
            )
        ).all()
    )
    pending = [row for row in rows if row.embedding_model_version != target]

    embedded = 0
    skipped = total - len(pending)
    failed = 0
    interrupted = False
    errors: list[str] = []

    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        partial = False
        if max_rows is not None:
            remaining_cap = max_rows - (embedded + skipped)
            if remaining_cap <= 0:
                interrupted = True
                break
            if len(batch) > remaining_cap:
                batch = batch[:remaining_cap]
                partial = True
        try:
            vectors = await provider.embed([row.text for row in batch])
        except Exception as exc:  # noqa: BLE001 - per-batch failure is reported, not fatal
            failed += len(batch)
            errors.append(f"batch {start // batch_size}: {exc}")
            continue
        if len(vectors) != len(batch):
            failed += len(batch)
            errors.append(f"batch {start // batch_size}: got {len(vectors)} vectors for {len(batch)} rows")
            continue
        updated_at = utcnow()
        params = [
            {
                "row_id": row.id,
                "embedding": vector,
                "embedding_model_version": target,
                "updated_time": updated_at,
            }
            for row, vector in zip(batch, vectors, strict=False)
        ]
        memories_table = cast(Table, Memory.__table__)
        await session.execute(
            memories_table.update()
            .where(memories_table.c.id == bindparam("row_id"))
            .values(
                embedding=bindparam("embedding"),
                embedding_model_version=bindparam("embedding_model_version"),
                updated_time=bindparam("updated_time"),
            )
            .execution_options(synchronize_session=None),
            params,
        )
        await session.commit()
        embedded += len(batch)
        if partial:
            interrupted = True
            break
        if on_progress is not None:
            on_progress(
                {
                    "model_version": target,
                    "done": embedded + skipped,
                    "total": total,
                    "embedded": embedded,
                    "skipped": skipped,
                    "failed": failed,
                    "elapsed_s": round(time.perf_counter() - started, 3),
                }
            )

    elapsed = time.perf_counter() - started
    processed = embedded + failed
    return ReembedReport(
        model_version=target,
        total=total,
        skipped=skipped,
        embedded=embedded,
        failed=failed,
        interrupted=interrupted,
        elapsed_s=elapsed,
        docs_per_s=processed / elapsed if elapsed else 0.0,
        degraded=degraded,
        started_at=started_at,
        completed_at=None if interrupted else utcnow().isoformat(timespec="seconds"),
        errors=errors,
    )


async def reembed_status(session: AsyncSession) -> dict:
    """Per-model-version vector counts for mixed-model detection."""

    rows = (
        await session.execute(
            select(Memory.embedding_model_version, func.count())
            .where(Memory.is_current.is_(True), Memory.redacted.is_(False))
            .group_by(Memory.embedding_model_version)
        )
    ).all()
    counts = {str(version): int(count) for version, count in rows}
    return {
        "models": counts,
        "total": sum(counts.values()),
        "mixed": len(counts) > 1,
    }
