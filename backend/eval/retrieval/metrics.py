"""Ranking metrics for the retrieval eval (nDCG@10, MRR, top-5 hit rate)."""

from __future__ import annotations

import math


def ndcg_at_k(relevance: list[int], k: int = 10) -> float:
    """nDCG with binary relevance, truncated at ``k``."""

    if not relevance:
        return 0.0
    rel = relevance[:k]
    dcg = sum(item / math.log2(i + 2) for i, item in enumerate(rel))
    ideal = sorted(rel, reverse=True)
    idcg = sum(item / math.log2(i + 2) for i, item in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def reciprocal_rank(relevance: list[int]) -> float:
    """MRR for one query: 1/rank of the first relevant result, 0 if none."""

    for index, item in enumerate(relevance):
        if item:
            return 1.0 / (index + 1)
    return 0.0


def top_k_hit(relevance: list[int], k: int = 5) -> bool:
    return any(relevance[:k])


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
