"""SYNAPSE embedding provider tests (offline-safe)."""

from __future__ import annotations

import math

import pytest

from app import embeddings
from app.embeddings import (
    EMBEDDING_MODEL_GRANITE,
    EMBEDDING_MODEL_HASH,
    EMBEDDING_MODEL_QWEN3,
    EmbeddingDimensionError,
    GraniteEmbeddingProvider,
    HashEmbeddingProvider,
    HTTPEmbeddingProvider,
    Qwen3EmbeddingProvider,
    create_embedding_arbiter,
    embedding_model_specs,
    get_embedder,
    truncate_matryoshka,
)
from app.ml.settings import MLSettings


async def test_hash_provider_is_deterministic_and_normalized() -> None:
    provider = HashEmbeddingProvider(dim=64)
    first = await provider.embed(["hello world", "hello world"])
    second = await provider.embed(["hello world"])
    assert first[0] == first[1] == second[0]
    norm = math.sqrt(sum(v * v for v in first[0]))
    assert abs(norm - 1.0) < 1e-9
    assert provider.model_version == EMBEDDING_MODEL_HASH
    assert provider.degraded is False


async def test_http_provider_records_remote_model_version() -> None:
    provider = HTTPEmbeddingProvider(
        base_url="http://localhost:1",
        api_key=None,
        model="text-embedding-3-small",
        dim=384,
    )
    assert provider.model_version == "http:text-embedding-3-small"


class _FakeEmbedResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "data": [
                {"index": index, "embedding": vector}
                for index, vector in enumerate(self._vectors)
            ]
        }


class _FakeEmbedClient:
    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.calls: list[dict] = []

    async def __aenter__(self) -> _FakeEmbedClient:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "json": json})
        count = len(json["input"])
        return _FakeEmbedResponse([[0.1] * self.dim for _ in range(count)])


async def test_http_provider_sends_dimensions_and_validates_output_dim(
    monkeypatch,
) -> None:
    client = _FakeEmbedClient(dim=384)
    monkeypatch.setattr(embeddings.httpx, "AsyncClient", lambda **kwargs: client)
    provider = HTTPEmbeddingProvider(
        base_url="http://embeddings.test/v1",
        api_key=None,
        model="text-embedding-3-small",
        dim=384,
        send_dimensions=True,
    )
    vectors = await provider.embed(["hello world"])
    assert len(vectors[0]) == 384
    assert client.calls[0]["json"]["dimensions"] == 384
    assert provider.model_version == "http:text-embedding-3-small"

    # A hosted model that ignores `dimensions` and returns 1536 must fail
    # loudly, never truncate silently.
    bad = _FakeEmbedClient(dim=1536)
    monkeypatch.setattr(embeddings.httpx, "AsyncClient", lambda **kwargs: bad)
    with pytest.raises(EmbeddingDimensionError, match="1536"):
        await provider.embed(["hello world"])


def test_matryoshka_truncation_renormalizes() -> None:
    vector = [3.0] * 1024
    truncated = truncate_matryoshka(vector, 384)
    assert len(truncated) == 384
    norm = math.sqrt(sum(v * v for v in truncated))
    assert abs(norm - 1.0) < 1e-9
    assert truncated == truncate_matryoshka(vector, 384)
    # Shorter inputs stay normalized without truncation.
    short = truncate_matryoshka([2.0, 0.0], 384)
    assert abs(math.sqrt(sum(v * v for v in short)) - 1.0) < 1e-9


def test_granite_spec_matches_verified_facts() -> None:
    specs = {spec.name: spec for spec in embedding_model_specs()}
    granite = specs[EMBEDDING_MODEL_GRANITE]
    assert granite.license == "Apache-2.0"
    # Agent 2 (budget owner) resolved the always-tier conflict: granite rides
    # the on-demand slot at its honest measured footprint.
    assert granite.tier.value == "on_demand"
    assert granite.disk_mb <= 100
    # Honest measured footprint (ONNX Runtime ModernBERT graph on Apple M2);
    # the mission's ~100 MB assumption applies to the file, not the runtime.
    assert granite.resident_mb >= 100
    assert "huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2" in (
        granite.source_url or ""
    )
    qwen3 = specs[EMBEDDING_MODEL_QWEN3]
    assert qwen3.license == "Apache-2.0"
    assert qwen3.tier.value == "on_demand"


def test_embedding_arbiter_registers_roster() -> None:
    arbiter = create_embedding_arbiter()
    names = arbiter.registry.names()
    assert EMBEDDING_MODEL_GRANITE in names
    assert EMBEDDING_MODEL_QWEN3 in names


async def test_granite_provider_degrades_without_weights(tmp_path) -> None:
    ml_settings = MLSettings(ml_model_dir=tmp_path)
    provider = GraniteEmbeddingProvider(ml_settings=ml_settings)
    vectors = await provider.embed(["hello world"])
    assert provider.degraded is True
    assert len(vectors) == 1
    assert len(vectors[0]) == 384


async def test_qwen3_provider_reports_matryoshka_dim(tmp_path) -> None:
    ml_settings = MLSettings(ml_model_dir=tmp_path)
    provider = Qwen3EmbeddingProvider(matryoshka_dim=384, ml_settings=ml_settings)
    assert provider.model_version == EMBEDDING_MODEL_QWEN3
    assert provider.dim == 384
    vectors = await provider.embed(["hello"])
    assert provider.degraded is True
    assert len(vectors[0]) == 384


async def test_get_embedder_hash_default() -> None:
    provider = get_embedder()
    assert isinstance(provider, HashEmbeddingProvider)
    assert provider.model_version == EMBEDDING_MODEL_HASH


async def test_get_embedder_granite_factory_entry(monkeypatch, tmp_path) -> None:
    """The REAL factory entry returns the granite provider and degrades safely."""

    monkeypatch.setattr(embeddings.settings, "embedding_provider", "granite")
    monkeypatch.setattr(embeddings, "get_ml_settings", lambda: MLSettings(ml_model_dir=tmp_path))
    embeddings._embedder_cache.clear()
    provider = get_embedder()
    assert isinstance(provider, GraniteEmbeddingProvider)
    vectors = await provider.embed(["hello"])
    assert len(vectors[0]) == 384
    assert provider.degraded is True


async def test_get_embedder_http_factory_requires_base_url(monkeypatch) -> None:
    monkeypatch.setattr(embeddings.settings, "embedding_provider", "http")
    monkeypatch.setattr(embeddings.settings, "embedding_base_url", None)
    with pytest.raises(RuntimeError):
        get_embedder()
