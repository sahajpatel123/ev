"""Retrieval eval harness: seeds a corpus, runs retrievers, reports metrics."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.embeddings import HashEmbeddingProvider, get_embedder
from app.memory.retrieval import SCORE_WEIGHTS, Retriever
from app.models import Memory
from eval.retrieval.metrics import mean, ndcg_at_k, reciprocal_rank, top_k_hit
from eval.retrieval.synthetic_corpus import Question, SyntheticCorpus


@dataclass
class QuestionResult:
    question_id: str
    query: str
    expected_ids: list[str]
    retrieved_ids: list[str]
    ndcg10: float
    mrr: float
    hit5: bool
    rank_first: int | None
    hit_components: dict[str, float]


def _load_questions(path: Path) -> list[Question]:
    data = json.loads(path.read_text(encoding="utf-8"))
    questions: list[Question] = []
    for item in data["questions"]:
        questions.append(
            Question(
                query=str(item["query"]),
                expected_keys=[str(key) for key in item.get("expected_memory_ids", [])],
            )
        )
    return questions


async def _seed_corpus(
    session: AsyncSession,
    corpus: SyntheticCorpus,
    provider,
) -> dict[str, str]:
    """Insert corpus memories and return key -> memory id."""

    key_to_id: dict[str, str] = {}
    now = datetime.now(UTC)
    for start in range(0, len(corpus.memories), 32):
        batch = corpus.memories[start : start + 32]
        vectors = await provider.embed([memory.text for memory in batch])
        for memory, vector in zip(batch, vectors, strict=False):
            row = Memory(
                id=uuid4(),
                memory_type=memory.memory_type,
                text=memory.text,
                payload={},
                importance=memory.importance,
                confidence=0.9,
                source_type="explicit",
                privacy_level=memory.privacy_level,
                event_time=memory.event_time or now,
                valid_from=memory.event_time or now,
                fingerprint=f"synapse-eval-{memory.key}",
                embedding=vector,
                embedding_model_version=getattr(provider, "model_version", "hash"),
            )
            session.add(row)
            await session.flush()
            key_to_id[memory.key] = str(row.id)
    await session.commit()
    return key_to_id


async def evaluate_retrieval(
    session: AsyncSession,
    questions: list[Question],
    *,
    provider,
    key_to_id: dict[str, str] | None = None,
    k: int = 10,
    rerank: bool = False,
) -> tuple[list[QuestionResult], dict]:
    """Run every question through Retriever.search and compute metrics."""

    retriever = Retriever(session, embeddings=provider)
    results: list[QuestionResult] = []
    rerank_stats = {
        "triggered": 0,
        "runs": 0,
        "degraded": 0,
        "latency_ms": 0.0,
        "candidates": 0,
    }
    for index, question in enumerate(questions):
        hits = await retriever.search(
            question.query,
            k=k,
            access="model",
            rerank=rerank,
        )
        if key_to_id is None:
            expected_ids = question.expected_keys
        else:
            expected_ids = [
                key_to_id[key] for key in question.expected_keys if key in key_to_id
            ]
        expected = set(expected_ids)
        retrieved = [hit.memory_id for hit in hits]
        relevance = [1 if memory_id in expected else 0 for memory_id in retrieved]
        rank_first = next(
            (i + 1 for i, memory_id in enumerate(retrieved) if memory_id in expected),
            None,
        )
        first_hit = next((hit for hit in hits if hit.memory_id in expected), None)
        results.append(
            QuestionResult(
                question_id=f"q{index + 1:02d}",
                query=question.query,
                expected_ids=expected_ids,
                retrieved_ids=retrieved[:k],
                ndcg10=ndcg_at_k(relevance, k=k),
                mrr=reciprocal_rank(relevance),
                hit5=top_k_hit(relevance, k=5),
                rank_first=rank_first,
                hit_components=dict(first_hit.components) if first_hit else {},
            )
        )
    for key in rerank_stats:
        rerank_stats[key] += retriever.rerank_stats.get(key, 0)
    return results, rerank_stats


def _aggregate(results: list[QuestionResult], k: int) -> dict:
    components: dict[str, list[float]] = {}
    for result in results:
        for key, value in result.hit_components.items():
            components.setdefault(key, []).append(float(value))
    contribution = {
        key: round(mean(values), 4) for key, values in sorted(components.items())
    }
    weighted = {
        key: round(SCORE_WEIGHTS[key] * contribution[key], 4)
        for key in SCORE_WEIGHTS
        if key in contribution and SCORE_WEIGHTS[key] > 0
    }
    return {
        "ndcg_at_10": round(mean([r.ndcg10 for r in results]), 4),
        "mrr": round(mean([r.mrr for r in results]), 4),
        "top5_hit_rate": round(mean([1.0 if r.hit5 else 0.0 for r in results]), 4),
        "top1_rate": round(
            mean([1.0 if r.rank_first == 1 else 0.0 for r in results]), 4
        ),
        "retrieved_expected": sum(1 for r in results if r.rank_first is not None),
        "questions": len(results),
        "per_component_score_contribution": contribution,
        "weighted_component_contribution": weighted,
    }


async def _run_against_engine(
    engine: AsyncEngine,
    corpus: SyntheticCorpus | None,
    questions: list[Question],
    *,
    provider,
    k: int,
    rerank: bool,
) -> tuple[dict, list[QuestionResult], dict]:
    import app.models  # noqa: F401 - register every table
    from app.db import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        key_to_id = None
        if corpus is not None:
            key_to_id = await _seed_corpus(session, corpus, provider)
        results, rerank_stats = await evaluate_retrieval(
            session,
            questions,
            provider=provider,
            key_to_id=key_to_id,
            k=k,
            rerank=rerank,
        )
    await engine.dispose()
    return _aggregate(results, k), results, rerank_stats


async def run_comparison(
    corpus: SyntheticCorpus | None = None,
    *,
    questions: list[Question] | None = None,
    k: int = 10,
    rerank: bool = False,
    real_provider=None,
    database_url: str | None = None,
    dataset: str = "synthetic",
) -> dict:
    """Before/after table: hash baseline vs the configured real provider.

    When no corpus is given (live mode), ``questions`` must carry real memory
    ids and ``database_url`` points at the existing database.
    """

    if corpus is None and database_url is None:
        raise ValueError("live mode requires --database-url")
    if questions is not None:
        question_list = questions
    elif corpus is not None:
        question_list = corpus.questions
    else:
        raise ValueError("questions are required")

    tmpdir = tempfile.mkdtemp(prefix="ev-retrieval-eval-")
    baseline_url = database_url or f"sqlite+aiosqlite:///{tmpdir}/baseline.db"
    provider_url = database_url or f"sqlite+aiosqlite:///{tmpdir}/provider.db"

    baseline_engine = create_async_engine(baseline_url)
    baseline, baseline_results, _baseline_rerank_stats = await _run_against_engine(
        baseline_engine,
        corpus,
        question_list,
        provider=HashEmbeddingProvider(dim=384),
        k=k,
        rerank=False,
    )

    real = real_provider if real_provider is not None else get_embedder()
    provider_engine = create_async_engine(provider_url)
    provider_result, provider_results, provider_rerank_stats = await _run_against_engine(
        provider_engine,
        corpus,
        question_list,
        provider=real,
        k=k,
        rerank=rerank,
    )

    baseline_name = "hash"
    provider_name = getattr(real, "model_version", "hash")
    degraded = bool(getattr(real, "degraded", False))
    delta = {
        "ndcg_at_10": round(provider_result["ndcg_at_10"] - baseline["ndcg_at_10"], 4),
        "mrr": round(provider_result["mrr"] - baseline["mrr"], 4),
        "top5_hit_rate": round(
            provider_result["top5_hit_rate"] - baseline["top5_hit_rate"], 4
        ),
    }
    return {
        "schema_version": "ev.retrieval.eval.v1",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": dataset,
        "corpus": {
            "memories": len(corpus.memories) if corpus else None,
            "questions": len(question_list),
        },
        "before_after": {
            "baseline": {
                "provider": baseline_name,
                **baseline,
                "questions": [
                    {
                        "id": result.question_id,
                        "query": result.query,
                        "rank_first": result.rank_first,
                        "ndcg10": round(result.ndcg10, 4),
                        "mrr": round(result.mrr, 4),
                        "hit5": result.hit5,
                        "retrieved_ids": result.retrieved_ids,
                    }
                    for result in baseline_results
                ],
            },
            "provider": {"provider": provider_name, "degraded": degraded, **provider_result},
            "delta": delta,
        },
        "reranker": {
            "enabled": rerank,
            "model": "ms-marco-MiniLM-L-12-v2-qint8" if rerank else None,
            "stats": provider_rerank_stats,
        },
        "questions": [
            {
                "id": result.question_id,
                "query": result.query,
                "rank_first": result.rank_first,
                "ndcg10": round(result.ndcg10, 4),
                "mrr": round(result.mrr, 4),
                "hit5": result.hit5,
                "retrieved_ids": result.retrieved_ids,
            }
            for result in provider_results
        ],
    }
