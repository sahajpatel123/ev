"""In-process Memory OS health and gate telemetry. Never includes owner text."""

from __future__ import annotations

import time
from collections import deque
from typing import Any

_GATE_MS: deque[float] = deque(maxlen=128)
_BOOTSTRAP: dict[str, Any] = {
    "version": 0,
    "age_s": None,
    "updated_at": None,
    "through_event_id": None,
    "tokens": 0,
}
_CURATOR = {
    "calls": 0,
    "events": 0,
    "tokens": 0,
    "cards_updated": 0,
    "last_status": None,
    "last_curated_event_id": None,
}
_INDEX = {
    "fulltext_ready": False,
    "vector_ready": False,
    "last_fts_ms": None,
    "last_vector_ms": None,
    "last_hybrid_ms": None,
}
_REFLECT = {
    "last_at": None,
    "lag_events": 0,
}
_PREFETCH = {
    "last_ms": None,
    "hits": 0,
    "triggers": 0,
}


def note_gate_ms(value: float) -> None:
    _GATE_MS.append(max(0.0, float(value)))


def _percentile(samples: list[float], pct: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return round(ordered[index], 2)


def note_bootstrap(*, version: int, updated_at: str | None, through_event_id: str | None, tokens: int) -> None:
    _BOOTSTRAP["version"] = int(version)
    _BOOTSTRAP["updated_at"] = updated_at
    _BOOTSTRAP["through_event_id"] = through_event_id
    _BOOTSTRAP["tokens"] = int(tokens)
    if updated_at:
        try:
            from datetime import datetime

            stamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            _BOOTSTRAP["age_s"] = int(max(0, time.time() - stamp.timestamp()))
        except (TypeError, ValueError):
            _BOOTSTRAP["age_s"] = None


def note_curator(*, status: str, event_id: str | None = None, tokens: int = 0, cards: int = 0, events: int = 0) -> None:
    _CURATOR["calls"] += 1
    _CURATOR["events"] += int(events)
    _CURATOR["tokens"] += int(tokens)
    _CURATOR["cards_updated"] += int(cards)
    _CURATOR["last_status"] = status
    if event_id:
        _CURATOR["last_curated_event_id"] = event_id


def note_index(*, fulltext_ready: bool | None = None, vector_ready: bool | None = None, fts_ms: float | None = None, vector_ms: float | None = None, hybrid_ms: float | None = None) -> None:
    if fulltext_ready is not None:
        _INDEX["fulltext_ready"] = bool(fulltext_ready)
    if vector_ready is not None:
        _INDEX["vector_ready"] = bool(vector_ready)
    if fts_ms is not None:
        _INDEX["last_fts_ms"] = round(fts_ms, 2)
    if vector_ms is not None:
        _INDEX["last_vector_ms"] = round(vector_ms, 2)
    if hybrid_ms is not None:
        _INDEX["last_hybrid_ms"] = round(hybrid_ms, 2)


def note_reflection(*, lag_events: int = 0) -> None:
    from datetime import UTC, datetime

    _REFLECT["last_at"] = datetime.now(UTC).isoformat()
    _REFLECT["lag_events"] = int(lag_events)


def note_prefetch(*, ms: float | None = None, hit: bool = False, trigger: str | None = None) -> None:
    if ms is not None:
        _PREFETCH["last_ms"] = round(ms, 2)
    if hit:
        _PREFETCH["hits"] += 1
    if trigger:
        _PREFETCH["triggers"] += 1


# F1 shadow-memory telemetry: bounded metadata only, never owner content.
_SHADOW = {
    "turns_classified": 0,
    "retrievals": 0,
    "injections": 0,
    "zero_retrieval_turns": 0,
    "scope_denials": 0,
    "shadow_builds": 0,
    "expired_envelopes": 0,
    "last_intent": None,
    "last_level": None,
}
_SHADOW_CLASSIFY_MS: deque[float] = deque(maxlen=256)
_SHADOW_RETRIEVAL_MS: deque[float] = deque(maxlen=256)
_SHADOW_CONTEXT_MS: deque[float] = deque(maxlen=256)
_SHADOW_TOKENS: deque[int] = deque(maxlen=256)


def note_shadow_turn(intent: str, level: int) -> None:
    _SHADOW["turns_classified"] += 1
    _SHADOW["last_intent"] = str(intent)[:40]
    _SHADOW["last_level"] = int(level)


def note_shadow_classified(ms: float, intent: str, level: int) -> None:
    _SHADOW_CLASSIFY_MS.append(max(0.0, float(ms)))
    _SHADOW["last_intent"] = str(intent)[:40]
    _SHADOW["last_level"] = int(level)


def note_shadow_retrieval(ms: float, items: int, tokens: int) -> None:
    _SHADOW_RETRIEVAL_MS.append(max(0.0, float(ms)))
    _SHADOW_TOKENS.append(max(0, int(tokens)))
    _SHADOW["retrievals"] += 1


def note_context_build(ms: float) -> None:
    _SHADOW_CONTEXT_MS.append(max(0.0, float(ms)))


def note_shadow_injected(tokens: int) -> None:
    _SHADOW["injections"] += 1
    _SHADOW_TOKENS.append(max(0, int(tokens)))


def note_zero_retrieval_turn() -> None:
    _SHADOW["zero_retrieval_turns"] += 1


def note_scope_denial(scope: str) -> None:
    _SHADOW["scope_denials"] += 1


def note_shadow_expired() -> None:
    _SHADOW["expired_envelopes"] += 1


def note_shadow_build() -> None:
    _SHADOW["shadow_builds"] += 1


def _retrieval_stage_snapshot() -> dict[str, Any]:
    from app.memory.retrieval import embed_cache_stats, retrieval_stage_snapshot

    return {
        "stages": retrieval_stage_snapshot(),
        "embed_cache": embed_cache_stats(),
    }


def _memory_epoch_value() -> int:
    from app.memory.retrieval import memory_epoch

    return memory_epoch()


def shadow_health_snapshot() -> dict[str, Any]:
    classify = list(_SHADOW_CLASSIFY_MS)
    retrieval = list(_SHADOW_RETRIEVAL_MS)
    context = list(_SHADOW_CONTEXT_MS)
    tokens = list(_SHADOW_TOKENS)
    return {
        "mode_counts": {
            "turns_classified": _SHADOW["turns_classified"],
            "retrievals": _SHADOW["retrievals"],
            "injections": _SHADOW["injections"],
            "zero_retrieval_turns": _SHADOW["zero_retrieval_turns"],
            "scope_denials": _SHADOW["scope_denials"],
            "shadow_builds": _SHADOW["shadow_builds"],
            "expired_envelopes": _SHADOW["expired_envelopes"],
        },
        "last_intent": _SHADOW["last_intent"],
        "last_level": _SHADOW["last_level"],
        "classify_p50_ms": _percentile(classify, 50),
        "classify_p95_ms": _percentile(classify, 95),
        "retrieval_p50_ms": _percentile(retrieval, 50),
        "retrieval_p95_ms": _percentile(retrieval, 95),
        "context_build_p50_ms": _percentile(context, 50),
        "context_build_p95_ms": _percentile(context, 95),
        "shadow_tokens_avg": round(sum(tokens) / len(tokens), 1) if tokens else None,
        "shadow_tokens_max": max(tokens) if tokens else None,
    }


def snapshot() -> dict[str, Any]:
    samples = list(_GATE_MS)
    return {
        "memory_gate_p50_ms": _percentile(samples, 50),
        "memory_gate_p95_ms": _percentile(samples, 95),
        "bootstrap_version": _BOOTSTRAP["version"],
        "bootstrap_age": _BOOTSTRAP["age_s"],
        "bootstrap_tokens": _BOOTSTRAP["tokens"],
        "last_curated_event_id": _CURATOR["last_curated_event_id"],
        "curator_status": _CURATOR["last_status"],
        "curator_calls": _CURATOR["calls"],
        "curator_events": _CURATOR["events"],
        "curator_tokens": _CURATOR["tokens"],
        "cards_updated": _CURATOR["cards_updated"],
        "fulltext_ready": _INDEX["fulltext_ready"],
        "vector_ready": _INDEX["vector_ready"],
        "fulltext_ms": _INDEX["last_fts_ms"],
        "vector_ms": _INDEX["last_vector_ms"],
        "hybrid_ms": _INDEX["last_hybrid_ms"],
        "last_reflection_at": _REFLECT["last_at"],
        "reflection_lag_events": _REFLECT["lag_events"],
        "prefetch_ms": _PREFETCH["last_ms"],
        "prefetch_hits": _PREFETCH["hits"],
        "prefetch_triggers": _PREFETCH["triggers"],
        "shadow": shadow_health_snapshot(),
        "retrieval_stages": _retrieval_stage_snapshot(),
        "memory_epoch": _memory_epoch_value(),
    }
