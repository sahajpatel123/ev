"""SYNAPSE retrieval evaluation harness (ev-eval retrieval)."""

from eval.retrieval.harness import run_comparison
from eval.retrieval.metrics import ndcg_at_k, reciprocal_rank, top_k_hit
from eval.retrieval.synthetic_corpus import build_synthetic_corpus

__all__ = [
    "build_synthetic_corpus",
    "ndcg_at_k",
    "reciprocal_rank",
    "run_comparison",
    "top_k_hit",
]
