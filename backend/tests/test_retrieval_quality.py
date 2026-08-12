"""SYNAPSE retrieval quality tests: weights, boundary, mixed models, reembed."""

from __future__ import annotations

import hashlib
import math
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app import embeddings as embeddings_module
from app.contracts import RetrievedMemory
from app.embeddings import (
    EMBEDDING_MODEL_GRANITE,
    EMBEDDING_MODEL_HASH,
    HashEmbeddingProvider,
    reembed_all_memories,
    reembed_status,
)
from app.memory.retrieval import SCORE_WEIGHTS, Retriever
from app.ml.settings import MLSettings
from app.models import Memory
from app.rerank import CrossEncoderReranker, should_rerank


class _WideProvider:
    """Deterministic embedder with a wide cosine distribution (hash-like)."""

    name = "test-wide"
    model_version = "test-wide"
    degraded = False

    def __init__(self) -> None:
        self.dim = 384

    def ensure_ready(self) -> bool:
        return True

    async def embed(self, texts):
        vectors = []
        for text in texts:
            vec = [0.0] * self.dim
            for token in text.lower().split():
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                idx = int.from_bytes(digest[:4], "big") % self.dim
                vec[idx] += 1.0 if digest[4] & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


class _CompressedProvider(_WideProvider):
    """Deterministic embedder with a compressed 0.7-0.9 cosine band (granite-like)."""

    name = "test-compressed"
    model_version = "test-compressed"

    def __init__(self) -> None:
        super().__init__()
        rng = __import__("random").Random(7)
        raw = [rng.uniform(-1.0, 1.0) for _ in range(self.dim)]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        self._bias = [v / norm for v in raw]

    async def embed(self, texts):
        vectors = []
        for text in texts:
            base = [0.0] * self.dim
            for token in set(text.lower().split()):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                idx = int.from_bytes(digest[:4], "big") % self.dim
                base[idx] += 1.0
            base_norm = math.sqrt(sum(v * v for v in base)) or 1.0
            base = [v / base_norm for v in base]
            mixed = [a + 0.6 * b for a, b in zip(base, self._bias, strict=False)]
            norm = math.sqrt(sum(v * v for v in mixed)) or 1.0
            vectors.append([v / norm for v in mixed])
        return vectors


async def _seed_memory(
    session: AsyncSession,
    text: str,
    *,
    memory_type: str = "fact",
    privacy_level: str = "normal",
    importance: float = 0.5,
    embedding: list[float] | None = None,
    embedding_model_version: str | None = None,
) -> Memory:
    memory = Memory(
        memory_type=memory_type,
        text=text,
        payload={},
        importance=importance,
        confidence=0.9,
        source_type="explicit",
        privacy_level=privacy_level,
        fingerprint=uuid4().hex,
        embedding=embedding,
        embedding_model_version=embedding_model_version,
    )
    session.add(memory)
    await session.flush()
    return memory


def test_score_weights_sum_to_one_exactly() -> None:
    assert abs(sum(SCORE_WEIGHTS.values()) - 1.0) < 1e-9


async def test_retrieval_components_present_and_boundary_holds(
    db_session: AsyncSession,
) -> None:
    embedder = HashEmbeddingProvider(dim=64)
    normal = await _seed_memory(
        db_session,
        "Decided to use DeepSeek V4 Flash for coding models.",
        memory_type="decision",
        embedding=(await embedder.embed(["Decided to use DeepSeek V4 Flash for coding models."]))[0],
    )
    secret = await _seed_memory(
        db_session,
        "Private therapy notes must never reach a model.",
        memory_type="fact",
        privacy_level="never_send_to_model",
        embedding=(await embedder.embed(["Private therapy notes must never reach a model."]))[0],
    )
    await db_session.commit()

    retriever = Retriever(db_session, embeddings=embedder)
    results = await retriever.search(
        "which coding model should I use?",
        k=10,
        access="model",
        rerank=True,
    )
    ids = [r.memory_id for r in results]
    assert str(normal.id) in ids
    assert str(secret.id) not in ids
    assert results
    assert set(SCORE_WEIGHTS) <= set(results[0].components)
    assert "embedding_degraded" in results[0].components
    assert "embedding_comparable" in results[0].components


async def test_mixed_model_versions_never_compared_semantically(
    db_session: AsyncSession,
) -> None:
    embedder = HashEmbeddingProvider(dim=64)
    text = "love reading at night"
    vector = (await embedder.embed([text]))[0]
    legacy = await _seed_memory(
        db_session,
        text,
        embedding=vector,
        embedding_model_version=None,
    )
    foreign = await _seed_memory(
        db_session,
        text,
        embedding=vector,
        embedding_model_version=EMBEDDING_MODEL_GRANITE,
    )
    await db_session.commit()

    retriever = Retriever(db_session, embeddings=embedder)
    results = await retriever.search(text, k=10, access="model", rerank=False)
    by_id = {r.memory_id: r for r in results}
    assert str(legacy.id) in by_id
    assert str(foreign.id) in by_id
    assert by_id[str(legacy.id)].components["semantic"] > 0.0
    assert by_id[str(foreign.id)].components["semantic"] == 0.0
    assert by_id[str(foreign.id)].components["embedding_comparable"] == 0.0


async def test_reranker_fallback_preserves_base_order(tmp_path) -> None:
    ml_settings = MLSettings(ml_model_dir=tmp_path)
    reranker = CrossEncoderReranker(ml_settings=ml_settings)
    candidates = [
        RetrievedMemory(
            memory_id="a",
            text="alpha",
            memory_type="fact",
            payload={},
            importance=0.5,
            confidence=0.9,
            event_time=None,
            privacy_level="normal",
            source_type="explicit",
            score=0.9,
        ),
        RetrievedMemory(
            memory_id="b",
            text="beta",
            memory_type="fact",
            payload={},
            importance=0.5,
            confidence=0.9,
            event_time=None,
            privacy_level="normal",
            source_type="explicit",
            score=0.4,
        ),
    ]
    result = await reranker.rerank("query", candidates, final_k=2)
    assert result.degraded is True
    assert result.triggered is False
    assert [r.memory_id for r in result.results] == ["a", "b"]


def test_should_rerank_only_for_hard_queries() -> None:
    easy = [
        RetrievedMemory(
            memory_id="1",
            text="x",
            memory_type="fact",
            payload={},
            importance=0.5,
            confidence=0.9,
            event_time=None,
            privacy_level="normal",
            source_type="explicit",
            score=0.95,
        )
    ]
    hard = [
        RetrievedMemory(
            memory_id="1",
            text="x",
            memory_type="fact",
            payload={},
            importance=0.5,
            confidence=0.9,
            event_time=None,
            privacy_level="normal",
            source_type="explicit",
            score=0.3,
        )
    ]
    assert should_rerank(easy, k=1, threshold=0.55, span_threshold=0.05) is False
    assert should_rerank(hard, k=1, threshold=0.55, span_threshold=0.05) is True


async def test_reembed_is_resumable_and_stamps_model_version(
    db_session: AsyncSession,
) -> None:
    count = 5000
    for index in range(count):
        await _seed_memory(
            db_session,
            f"memory number {index} about topic {index % 17}",
            embedding_model_version=None,
            embedding=None,
        )
    await db_session.commit()

    first = await reembed_all_memories(db_session, batch_size=64, max_rows=1234)
    assert first.embedded == 1234
    assert first.interrupted is True
    assert first.model_version == EMBEDDING_MODEL_HASH

    second = await reembed_all_memories(db_session, batch_size=64)
    assert second.interrupted is False
    assert second.skipped == 1234
    assert second.embedded == count - 1234
    assert second.failed == 0

    third = await reembed_all_memories(db_session, batch_size=64)
    assert third.skipped == count
    assert third.embedded == 0

    status = await reembed_status(db_session)
    assert status["total"] == count
    assert status["models"] == {EMBEDDING_MODEL_HASH: count}
    assert status["mixed"] is False


async def test_reembed_refuses_degraded_real_provider(
    db_session: AsyncSession,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(embeddings_module.settings, "embedding_provider", "granite")
    monkeypatch.setattr(
        embeddings_module,
        "get_ml_settings",
        lambda: MLSettings(ml_model_dir=tmp_path),
    )
    embeddings_module._embedder_cache.clear()
    with pytest.raises(RuntimeError, match="degraded"):
        await reembed_all_memories(db_session)


async def test_synthetic_eval_harness_runs_in_ci() -> None:
    from eval.retrieval.harness import run_comparison
    from eval.retrieval.synthetic_corpus import build_synthetic_corpus

    corpus = build_synthetic_corpus(distractor_count=25)
    report = await run_comparison(corpus, k=10)
    assert report["corpus"]["questions"] == 50
    before_after = report["before_after"]
    for side in ("baseline", "provider"):
        metrics = before_after[side]
        assert "ndcg_at_10" in metrics
        assert "mrr" in metrics
        assert "top5_hit_rate" in metrics
        assert "per_component_score_contribution" in metrics
    assert "delta" in before_after


async def test_live_mode_questions_file_runs_against_database(tmp_path) -> None:
    """The personal-set path: a questions file with real memory ids + live DB."""

    import json

    from sqlalchemy.ext.asyncio import (
        async_sessionmaker,
        create_async_engine,
    )

    from app.db import Base
    from eval.retrieval.harness import _load_questions, run_comparison

    db_path = tmp_path / "live.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    embedder = HashEmbeddingProvider(dim=64)
    ids: list[str] = []
    async with session_factory() as session:
        for _index, text in enumerate(
            [
                "Decided to use DeepSeek V4 Flash for coding models.",
                "Prefer tea over coffee in the morning.",
            ]
        ):
            memory = await _seed_memory(
                session,
                text,
                embedding=(await embedder.embed([text]))[0],
                embedding_model_version="hash",
            )
            ids.append(str(memory.id))
        await session.commit()
    await engine.dispose()

    questions_path = tmp_path / "questions.json"
    questions_path.write_text(
        json.dumps(
            {
                "schema_version": "ev.retrieval.questions.v1",
                "questions": [
                    {
                        "id": "q1",
                        "query": "Which coding model did I decide to use?",
                        "expected_memory_ids": [ids[0]],
                    },
                    {
                        "id": "q2",
                        "query": "Do I prefer tea or coffee in the morning?",
                        "expected_memory_ids": [ids[1]],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    questions = _load_questions(questions_path)
    report = await run_comparison(
        None,
        questions=questions,
        k=10,
        database_url=db_url,
    )
    assert report["corpus"]["questions"] == 2
    assert report["before_after"]["provider"]["ndcg_at_10"] > 0.0
    assert len(report["questions"]) == 2


async def test_calibration_robust_across_score_distributions(monkeypatch) -> None:
    """Per-query calibration must not collapse ranking for either distribution."""

    from app.memory import retrieval as retrieval_module
    from eval.retrieval.harness import run_comparison
    from eval.retrieval.synthetic_corpus import build_synthetic_corpus

    corpus = build_synthetic_corpus(distractor_count=25)

    async def _cosine_band(provider) -> float:
        texts = [
            "hello world",
            "quantum physics equations",
            "the cat sat on the mat",
            "Decided to use DeepSeek V4 Flash for coding models.",
            "Which coding model did I decide to use?",
            "a",
            "zzzzzzzzzzzzzzzzzzzz",
        ]

        vectors = await provider.embed(texts)

        def cos(a, b):
            dot = sum(x * y for x, y in zip(a, b, strict=False))
            na = math.sqrt(sum(x * x for x in a)) or 1.0
            nb = math.sqrt(sum(y * y for y in b)) or 1.0
            return dot / (na * nb)

        values = [
            cos(vectors[i], vectors[j])
            for i in range(len(vectors))
            for j in range(i + 1, len(vectors))
        ]
        return values

    wide = _WideProvider()
    compressed = _CompressedProvider()
    compressed_values = await _cosine_band(compressed)
    wide_values = await _cosine_band(wide)
    compressed_mean = sum(compressed_values) / len(compressed_values)
    wide_mean = sum(wide_values) / len(wide_values)
    # Deliberately different distributions: compressed sits in a higher,
    # tighter band (granite-like); wide spans a low floor (hash-like).
    assert min(compressed_values) > min(wide_values)
    assert compressed_mean > wide_mean

    for provider in (wide, compressed):
        monkeypatch.setattr(retrieval_module.settings, "semantic_normalize", True)
        calibrated = await run_comparison(corpus, real_provider=provider)
        monkeypatch.setattr(retrieval_module.settings, "semantic_normalize", False)
        raw = await run_comparison(corpus, real_provider=provider)

        cal = calibrated["before_after"]["provider"]
        raw_metrics = raw["before_after"]["provider"]
        # Calibration must never collapse ranking for either distribution:
        # no meaningful degradation versus the uncalibrated run, and a
        # functional floor. (The acceptance bar is measured on real models,
        # not these weak deterministic doubles.)
        assert cal["ndcg_at_10"] >= raw_metrics["ndcg_at_10"] - 0.02
        assert cal["top5_hit_rate"] >= raw_metrics["top5_hit_rate"] - 0.02
        assert cal["ndcg_at_10"] >= 0.55
        assert cal["top5_hit_rate"] >= 0.55


async def test_reembed_resumable_with_http_provider(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    """The resumable re-embed job is proven against the http provider path."""

    from app import embeddings as embeddings_module

    class _HttpResponse:
        def __init__(self, count: int) -> None:
            self._count = count

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": [
                    {"index": index, "embedding": [0.25] * 384}
                    for index in range(self._count)
                ]
            }

    class _HttpClient:
        def __init__(self, **kwargs) -> None:
            self._kwargs = kwargs

        async def __aenter__(self) -> _HttpClient:
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, url, headers=None, json=None):
            return _HttpResponse(len(json["input"]))

    monkeypatch.setattr(
        embeddings_module.httpx,
        "AsyncClient",
        lambda **kwargs: _HttpClient(**kwargs),
    )
    monkeypatch.setattr(embeddings_module.settings, "embedding_provider", "http")
    monkeypatch.setattr(embeddings_module.settings, "embedding_base_url", "http://test")
    monkeypatch.setattr(embeddings_module.settings, "embedding_api_key", "test-key")
    monkeypatch.setattr(embeddings_module.settings, "embedding_model", "text-embedding-3-small")
    monkeypatch.setattr(embeddings_module.settings, "embedding_dim", 384)
    monkeypatch.setattr(embeddings_module.settings, "embedding_http_dimensions", True)
    embeddings_module._embedder_cache.clear()

    count = 1000
    for index in range(count):
        await _seed_memory(
            db_session,
            f"hosted memory {index} about topic {index % 13}",
            embedding_model_version=None,
        )
    await db_session.commit()

    first = await reembed_all_memories(db_session, batch_size=32, max_rows=300)
    assert first.embedded == 300
    assert first.interrupted is True
    assert first.model_version == "http:text-embedding-3-small"

    second = await reembed_all_memories(db_session, batch_size=32)
    assert second.interrupted is False
    assert second.skipped == 300
    assert second.embedded == count - 300
    assert second.failed == 0

    status = await reembed_status(db_session)
    assert status["models"] == {"http:text-embedding-3-small": count}
    assert status["mixed"] is False
