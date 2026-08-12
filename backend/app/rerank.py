"""Optional on-demand cross-encoder reranker for hard retrieval queries.

The base hybrid formula (0.35 semantic + ... ) is the locked retrieval law;
the reranker is an explicit post-pass: top-50 base candidates are rescored by
a small cross-encoder and cut to top-10, only when the base scores suggest a
hard query. It is optional — ``EV_RERANKER_ENABLED=false`` disables it — and
it degrades to a no-op (base order preserved, ``degraded=True``) when the
weights, ONNX Runtime, or tokenizer library are absent.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import settings
from app.contracts import RetrievedMemory
from app.ml import store
from app.ml.arbiter import ModelArbiter
from app.ml.registry import ModelRegistry, ModelSpec, ModelTier
from app.ml.settings import MLSettings, get_ml_settings

RERANKER_MODEL = "ms-marco-MiniLM-L-12-v2-qint8"
RERANKER_SOURCE_URL = (
    "https://huggingface.co/Xenova/ms-marco-MiniLM-L-12-v2/resolve/main/"
    "onnx/model_quantized.onnx"
)
RERANKER_TOKENIZER_URL = (
    "https://huggingface.co/Xenova/ms-marco-MiniLM-L-12-v2/resolve/main/"
    "tokenizer.json"
)
RERANKER_TOKENIZER_SHA256: str | None = (
    "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66"
)
RERANKER_ONNX_SHA256: str | None = (
    "c5551b3e446396364913c5ad79e9c8411a76d26523b7d87232052ae6c0d0c7fd"
)


def reranker_spec() -> ModelSpec:
    return ModelSpec(
        name=RERANKER_MODEL,
        task="cross_encoder_rerank",
        source_url=RERANKER_SOURCE_URL,
        sha256=RERANKER_ONNX_SHA256,
        disk_mb=35,
        resident_mb=142,
        peak_mb=180,
        tier=ModelTier.ON_DEMAND,
        license="Apache-2.0",
        license_url="https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-12-v2",
        version="ms-marco-MiniLM-L-12-v2 (quantized)",
        verified=RERANKER_ONNX_SHA256 is not None,
    )


def create_reranker_arbiter(ml_settings: MLSettings | None = None) -> ModelArbiter:
    ml_settings = ml_settings or get_ml_settings()
    registry = ModelRegistry(exclusive_limit_mb=ml_settings.ml_exclusive_limit_mb)
    registry.register(reranker_spec())
    return ModelArbiter(registry, ml_settings)


def ensure_reranker_weights(ml_settings: MLSettings | None = None) -> list[Path]:
    """Checksum-verified download of the reranker ONNX + tokenizer."""

    ml_settings = ml_settings or get_ml_settings()
    spec = reranker_spec()
    if spec.sha256 is None or RERANKER_TOKENIZER_SHA256 is None:
        raise RuntimeError("reranker checksums are not pinned")
    main = store.download_model(spec, ml_settings)
    tokenizer = Path(ml_settings.ml_model_dir) / f"{RERANKER_MODEL}__tokenizer.json"
    if tokenizer.exists() and store.sha256_file(tokenizer) == RERANKER_TOKENIZER_SHA256:
        return [main, tokenizer]
    partial = tokenizer.with_name(f"{tokenizer.name}.part")
    with store.open_source(RERANKER_TOKENIZER_URL) as chunks, partial.open("wb") as out:
        for chunk in chunks:
            out.write(chunk)
    actual = store.sha256_file(partial)
    if actual != RERANKER_TOKENIZER_SHA256:
        partial.unlink(missing_ok=True)
        raise store.ChecksumError(
            f"sha256 mismatch for {tokenizer.name}: expected "
            f"{RERANKER_TOKENIZER_SHA256}, got {actual}"
        )
    import os

    os.replace(partial, tokenizer)
    return [main, tokenizer]


@dataclass
class RerankResult:
    results: list[RetrievedMemory]
    degraded: bool
    triggered: bool
    latency_ms: float
    candidates: int
    model: str | None = None
    scores: list[float] = field(default_factory=list)


def should_rerank(
    results: Sequence[RetrievedMemory],
    *,
    k: int,
    threshold: float | None = None,
    span_threshold: float | None = None,
) -> bool:
    """Hard-query heuristic: low ceiling or compressed top-k scores."""

    if not results:
        return False
    threshold = settings.reranker_hard_threshold if threshold is None else threshold
    span_threshold = settings.reranker_span_threshold if span_threshold is None else span_threshold
    top = results[0].score
    if top < threshold:
        return True
    if len(results) >= 2 and k >= 2:
        cutoff = results[min(k, len(results)) - 1].score
        return (top - cutoff) < span_threshold
    return False


class CrossEncoderReranker:
    """ms-marco MiniLM L-12-v2 cross-encoder on the on-demand arbiter slot."""

    name = "cross-encoder"
    model_version = RERANKER_MODEL

    def __init__(
        self,
        *,
        max_length: int = 512,
        batch_size: int = 8,
        ml_settings: MLSettings | None = None,
        arbiter: ModelArbiter | None = None,
    ) -> None:
        self.max_length = max_length
        self.batch_size = batch_size
        self.ml_settings = ml_settings or get_ml_settings()
        self.arbiter = arbiter or create_reranker_arbiter(self.ml_settings)
        self._session: Any = None
        self._tokenizer: Any = None
        self._input_names: list[str] = []
        self._lock = threading.Lock()
        self._degraded = True

    @property
    def degraded(self) -> bool:
        return self._degraded

    def _tokenizer_path(self) -> Path:
        return Path(self.ml_settings.ml_model_dir) / f"{RERANKER_MODEL}__tokenizer.json"

    def _ensure_ready(self) -> bool:
        if self._session is not None and self._tokenizer is not None:
            self._degraded = False
            return True
        with self._lock:
            if self._session is not None and self._tokenizer is not None:
                self._degraded = False
                return True
            try:
                self._load()
            except Exception:  # noqa: BLE001 - degraded fallback is the contract
                self._session = None
                self._tokenizer = None
                self._degraded = True
                return False
            self._degraded = False
            return True

    def _load(self) -> None:
        try:
            import onnxruntime as ort  # noqa: F401
        except Exception as exc:
            raise RuntimeError("onnxruntime not installed; install the 'ml' extra") from exc
        try:
            from tokenizers import Tokenizer  # type: ignore[import-not-found]
        except Exception as exc:
            raise RuntimeError("'tokenizers' not installed; lazy dependency requested from Agent 2") from exc

        spec = reranker_spec()
        model_path = store.target_path(self.ml_settings, spec)
        tokenizer_path = self._tokenizer_path()
        if not model_path.exists() or not tokenizer_path.exists():
            raise RuntimeError(
                f"reranker weights not cached ({model_path} / {tokenizer_path}); "
                "see docs/RETRIEVAL.md for the pinned download"
            )
        with self.arbiter.acquire(RERANKER_MODEL):
            session_options = ort.SessionOptions()
            session_options.graph_optimization_level = (
                ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
            )
            if hasattr(session_options, "enable_cpu_mem_arena"):
                session_options.enable_cpu_mem_arena = False
            session = ort.InferenceSession(
                str(model_path),
                sess_options=session_options,
                providers=["CPUExecutionProvider"],
            )
            tokenizer = Tokenizer.from_file(str(tokenizer_path))
            tokenizer.enable_truncation(max_length=self.max_length)
        self._session = session
        self._tokenizer = tokenizer
        self._input_names = [inp.name for inp in session.get_inputs()]

    def _score_pairs_sync(self, pairs: list[tuple[str, str]]) -> list[float]:
        import numpy as np

        tokenizer = self._tokenizer
        if tokenizer.padding is None:
            tokenizer.enable_padding()
        encodings = tokenizer.encode_batch(pairs)
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
        logits = np.asarray(outputs[0]).reshape(-1)
        return [1.0 / (1.0 + float(np.exp(-value))) for value in logits]

    async def rerank(
        self,
        query: str,
        candidates: Sequence[RetrievedMemory],
        *,
        final_k: int = 10,
    ) -> RerankResult:
        started = time.perf_counter()
        ordered = sorted(candidates, key=lambda r: r.score, reverse=True)
        if not ordered:
            return RerankResult(
                results=[],
                degraded=False,
                triggered=False,
                latency_ms=0.0,
                candidates=0,
                model=self.model_version,
            )
        ready = await asyncio.to_thread(self._ensure_ready)
        if not ready:
            return RerankResult(
                results=ordered[:final_k],
                degraded=True,
                triggered=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                candidates=len(ordered),
                model=None,
            )
        pairs = [(query, candidate.text) for candidate in ordered]
        scores: list[float] = []
        for start in range(0, len(pairs), self.batch_size):
            chunk = pairs[start : start + self.batch_size]
            scores.extend(await asyncio.to_thread(self._score_pairs_sync, chunk))
        ranked = sorted(
            zip(ordered, scores, strict=False),
            key=lambda item: item[1],
            reverse=True,
        )
        results: list[RetrievedMemory] = []
        for candidate, score in ranked[:final_k]:
            candidate.score = round(score, 4)
            candidate.components = {**candidate.components, "rerank": round(score, 4)}
            results.append(candidate)
        return RerankResult(
            results=results,
            degraded=False,
            triggered=True,
            latency_ms=(time.perf_counter() - started) * 1000,
            candidates=len(ordered),
            model=self.model_version,
            scores=[round(score, 4) for score in scores],
        )


def get_reranker() -> CrossEncoderReranker | None:
    """Factory; returns None when reranking is disabled."""

    if not settings.reranker_enabled:
        return None
    global _reranker_singleton
    if _reranker_singleton is None:
        _reranker_singleton = CrossEncoderReranker(
            batch_size=settings.reranker_batch_size
        )
    return _reranker_singleton


_reranker_singleton: CrossEncoderReranker | None = None
