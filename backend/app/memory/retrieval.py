from __future__ import annotations

import math
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.contracts import RetrievedMemory
from app.embeddings import (
    EMBEDDING_MODEL_HASH,
    get_embedder,
)
from app.memory.entities import extract_entities_from_text
from app.models import Memory, MemoryEntity, MemoryEvent
from app.training.personalization import calibration_multipliers
from app.utils.text import simple_tokens

SCORE_WEIGHTS = {
    "semantic": 0.35,
    "keyword": 0.20,
    "recency": 0.15,
    "importance": 0.15,
    "relationship": 0.10,
    "confidence": 0.05,
    # Informational components exposed for transparency; the documented
    # scoring formula above is unchanged, so they carry zero weight.
    "importance_base": 0.0,
    "personalization": 0.0,
}


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return max(0.0, min(1.0, dot / (na * nb)))


# F1.1 stage telemetry: bounded, per-stage latency rings (no owner content).
from collections import deque as _deque

_STAGE_MS: dict[str, Any] = {}
_STAGE_MAX = 256


def _note_stage(stage: str, ms: float) -> None:
    ring = _STAGE_MS.get(stage)
    if ring is None:
        ring = _deque(maxlen=_STAGE_MAX)
        _STAGE_MS[stage] = ring
    ring.append(max(0.0, float(ms)))


def retrieval_stage_snapshot() -> dict[str, dict[str, float]]:
    """P50/P95/max per retrieval stage for profiling (F1.1 §1)."""

    out: dict[str, dict[str, float]] = {}
    for stage, ring in _STAGE_MS.items():
        samples = sorted(ring)
        if not samples:
            continue
        out[stage] = {
            "n": len(samples),
            "p50": round(samples[len(samples) // 2], 2),
            "p95": round(samples[min(len(samples) - 1, int(0.95 * (len(samples) - 1)))], 2),
            "max": round(samples[-1], 2),
        }
    total = sum(s["p50"] for s in out.values()) or 1.0
    for stats in out.values():
        stats["p50_share_pct"] = round(100.0 * stats["p50"] / total, 1)
    return out


# Query-embedding cache: pure function of (normalized query, model version).
# No owner content persisted beyond the process; bounded LRU; no staleness
# risk because the key includes the embedding model version.
_EMBED_CACHE: dict[tuple[str, str], list[float]] = {}
_EMBED_CACHE_MAX = 256
_embed_cache_hits = 0
_embed_cache_misses = 0


def _embed_cache_key(query: str, model_version: str) -> tuple[str, str]:
    import hashlib

    normalized = " ".join((query or "").lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24], model_version


def _cached_embed(query: str) -> tuple[list[float] | None, bool]:
    """Cached single-query embedding. Returns (vector, hit)."""

    global _embed_cache_hits, _embed_cache_misses
    key = _embed_cache_key(query, str(getattr(get_embedder(), "model_version", "unknown")))
    hit = _EMBED_CACHE.get(key)
    if hit is not None:
        _embed_cache_hits += 1
        return hit, True
    _embed_cache_misses += 1
    return None, False


def _store_embed(query: str, vector: list[float]) -> None:
    key = _embed_cache_key(query, str(getattr(get_embedder(), "model_version", "unknown")))
    if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
        _EMBED_CACHE.pop(next(iter(_EMBED_CACHE)), None)
    _EMBED_CACHE[key] = vector


def embed_cache_stats() -> dict[str, int]:
    return {"entries": len(_EMBED_CACHE), "hits": _embed_cache_hits, "misses": _embed_cache_misses}


# Calibration multipliers: consent-gated, low-churn — short TTL + memory-epoch
# invalidation (any MemoryWriter write bumps the epoch, so a superseded or new
# memory can never be scored with stale personalization state).
_CAL_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_CAL_TTL_SECONDS = 15.0
_memory_epoch = 0


def bump_memory_epoch() -> None:
    """Invalidate authority-sensitive caches after ANY memory write."""

    global _memory_epoch
    _memory_epoch += 1
    _CAL_CACHE.clear()


def memory_epoch() -> int:
    return _memory_epoch


async def calibration_multipliers_cached(session: AsyncSession) -> dict[str, float]:
    import time as _time

    global _memory_epoch
    key = str(_memory_epoch)
    now = _time.monotonic()
    hit = _CAL_CACHE.get(key)
    if hit is not None and now - hit[0] < _CAL_TTL_SECONDS:
        return hit[1]
    value = await calibration_multipliers(session)
    if len(_CAL_CACHE) > 8:
        _CAL_CACHE.clear()
    _CAL_CACHE[key] = (now, value)
    return value


def reset_embed_cache() -> None:
    global _embed_cache_hits, _embed_cache_misses
    _EMBED_CACHE.clear()
    _embed_cache_hits = 0
    _embed_cache_misses = 0


# Fusion tokenization cache: text tokens per memory id. Memories are static
# between writes; epoch bump clears (supersession/new rows get retokenized).
_TOKEN_CACHE: dict[str, frozenset] = {}
_TOKEN_CACHE_MAX = 4096


def _tokens_for_memory(m) -> frozenset:
    from app.models import Memory as _M

    key = f"{m.id}:{int(m.updated_time.timestamp()) if m.updated_time else 0}"
    hit = _TOKEN_CACHE.get(key)
    if hit is not None:
        return hit
    tokens = frozenset(simple_tokens(m.text))
    if len(_TOKEN_CACHE) >= _TOKEN_CACHE_MAX:
        _TOKEN_CACHE.clear()
    _TOKEN_CACHE[key] = tokens
    return tokens


class Retriever:
    def __init__(self, session: AsyncSession, embeddings=None) -> None:
        self.session = session
        self.embeddings = embeddings or get_embedder()
        # Model-version law: vectors from different embedding models are never
        # comparable. NULL rows are legacy hash-era vectors (hash was the
        # production default before this change); anything else must match the
        # query embedder's version or semantic scoring is zeroed.
        self.embedding_model_version = getattr(
            self.embeddings, "model_version", EMBEDDING_MODEL_HASH
        )
        self.embedding_degraded = bool(getattr(self.embeddings, "degraded", False))
        # Reranker observability: aggregated across searches so evals can prove
        # whether the on-demand pass earns its latency.
        self.rerank_stats = {
            "triggered": 0,
            "runs": 0,
            "degraded": 0,
            "latency_ms": 0.0,
            "candidates": 0,
        }

    async def search(
        self,
        query: str,
        *,
        k: int = 20,
        access: str = "model",
        include_sensitive: bool = False,
        as_of: datetime | None = None,
        memory_types: list[str] | None = None,
        min_score: float = 0.0,
        rerank: bool = True,
        include_historical: bool = False,
        weight_overrides: dict[str, float] | None = None,
    ) -> list[RetrievedMemory]:
        """Hybrid retrieval with the locked default scoring formula.

        ``weight_overrides`` are MULTIPLIERS on the locked component weights
        (e.g. ``{"recency": 1.6}``), renormalized to sum 1 — used only by
        intent-specific retrieval (memory/intent.py). ``None`` keeps the
        locked formula byte-for-byte. The default formula remains law.
        """
        if not query.strip():
            return []
        import time as _time

        query_tokens = simple_tokens(query)
        query_entities = {e.name.lower() for e in extract_entities_from_text(query)}
        if weight_overrides:
            merged = {
                key: value
                * float(weight_overrides.get(key, 1.0))
                for key, value in SCORE_WEIGHTS.items()
            }
            total = sum(merged.values())
            effective_weights = (
                {key: value / total for key, value in merged.items()} if total > 0 else dict(SCORE_WEIGHTS)
            )
        else:
            effective_weights = SCORE_WEIGHTS
        t0 = _time.perf_counter()
        cached_emb, embed_hit = _cached_embed(query)
        if cached_emb is not None:
            query_emb = cached_emb
        else:
            try:
                query_emb = (await self.embeddings.embed([query]))[0]
                _store_embed(query, query_emb)
            except Exception:
                query_emb = None
        _note_stage("embed", (_time.perf_counter() - t0) * 1000.0)
        _note_stage("embed_hit", 0.01 if embed_hit else 0.0)

        current_filter = [] if include_historical else [Memory.is_current.is_(True)]
        stmt = (
            select(Memory)
            .where(Memory.redacted.is_(False), *current_filter)
            .order_by(Memory.importance.desc())
            .limit(settings.max_retrieval_memories * 4)
        )
        if access == "model":
            stmt = stmt.where(Memory.privacy_level != "never_send_to_model")
            if not include_sensitive:
                # Sensitive content requires explicit per-item opt-in before it
                # may reach a model; the chat pipeline never opts in by default.
                stmt = stmt.where(Memory.privacy_level != "sensitive")
        if memory_types:
            stmt = stmt.where(Memory.memory_type.in_(memory_types))
        if as_of is not None:
            stmt = stmt.where(
                Memory.valid_from <= as_of,
                (Memory.valid_until.is_(None)) | (Memory.valid_until >= as_of),
            )
        t0 = _time.perf_counter()
        result = await self.session.execute(stmt)
        memories = list(result.scalars().all())
        _note_stage("candidate_fetch", (_time.perf_counter() - t0) * 1000.0)

        # Entity overlap map: memory_id -> max overlap weight.
        t0 = _time.perf_counter()
        memory_links: dict = {}
        if query_entities and memories:
            mem_ids = [m.id for m in memories]
            link_stmt = select(MemoryEntity).where(MemoryEntity.memory_id.in_(mem_ids))
            links = (await self.session.execute(link_stmt)).scalars().all()
            from app.models import Entity

            entity_ids = [link.entity_id for link in links]
            ent_stmt = select(Entity.id, Entity.name).where(Entity.id.in_(entity_ids))
            entity_names = {eid: name.lower() for eid, name in (await self.session.execute(ent_stmt)).all()}
            for link in links:
                name = entity_names.get(link.entity_id)
                if name and name in query_entities:
                    memory_links.setdefault(link.memory_id, 0.0)
                    memory_links[link.memory_id] = max(memory_links[link.memory_id], link.weight)

        _note_stage("entity_links", (_time.perf_counter() - t0) * 1000.0)
        # Provenance map. F1.1 fast path: L1 implicit recall skips provenance
        # expansion (memory rows are the accelerators; events expand on
        # ambiguity/L2/L3 per the memory-first law).
        t0 = _time.perf_counter()
        prov: dict = {}
        if memories and include_historical:
            mem_ids = [m.id for m in memories]
            prov_rows = (
                await self.session.execute(select(MemoryEvent).where(MemoryEvent.memory_id.in_(mem_ids)))
            ).scalars().all()
            for row in prov_rows:
                prov.setdefault(row.memory_id, []).append(str(row.event_id))

        # Evidence-backed importance learning (consent-gated, versioned, neutral by default).
        multipliers = await calibration_multipliers_cached(self.session)
        _note_stage("provenance_calibration", (_time.perf_counter() - t0) * 1000.0)
        now = datetime.now(UTC)

        # Raw semantic cosine per memory, calibrated per query below so the
        # locked 0.35 weight sees a discriminative signal (ModernBERT-class
        # embeddings compress raw cosines into a ~0.7-0.9 band).
        raw_semantics: dict = {}
        for m in memories:
            mem_version = m.embedding_model_version
            comparable = mem_version is None or mem_version == self.embedding_model_version
            raw_semantics[m.id] = (
                cosine(query_emb, m.embedding)
                if query_emb is not None and m.embedding and comparable
                else 0.0
            )
        if raw_semantics and settings.semantic_normalize:
            semantic_min = min(raw_semantics.values())
            semantic_span = max(raw_semantics.values()) - semantic_min
        else:
            semantic_min = 0.0
            semantic_span = 0.0

        t0 = _time.perf_counter()
        scored: list[RetrievedMemory] = []
        # F1.1: vectorized semantic cosine for the candidate batch (identical
        # math; removes the per-row Python loop from the hot path).
        semantic_vec: dict = {}
        try:
            import numpy as _np

            if query_emb is not None and memories:
                qv = _np.asarray(query_emb, dtype=_np.float64)
                qnorm = _np.linalg.norm(qv) or 1.0
                vecs, ids = [], []
                for m in memories:
                    comparable = (
                        m.embedding_model_version is None
                        or m.embedding_model_version == self.embedding_model_version
                    )
                    if m.embedding and comparable:
                        vecs.append(m.embedding)
                        ids.append(m.id)
                if vecs:
                    mat = _np.asarray(vecs, dtype=_np.float64)
                    norms = _np.linalg.norm(mat, axis=1)
                    norms[norms == 0] = 1.0
                    dots = mat @ qv
                    cos = _np.clip(dots / (norms * qnorm), 0.0, 1.0)
                    semantic_vec = dict(zip(ids, cos.tolist(), strict=False))
        except Exception:  # noqa: BLE001 - vectorization is an optimization only
            semantic_vec = {}
        for m in memories:
            mem_version = m.embedding_model_version
            comparable = mem_version is None or mem_version == self.embedding_model_version
            semantic_raw = semantic_vec.get(m.id, raw_semantics.get(m.id, 0.0))
            semantic = (
                (semantic_raw - semantic_min) / semantic_span
                if settings.semantic_normalize and semantic_span > 1e-9
                else semantic_raw
            )
            mem_tokens = _tokens_for_memory(m)
            keyword = (
                len(query_tokens & mem_tokens) / len(query_tokens | mem_tokens)
                if query_tokens and mem_tokens
                else 0.0
            )
            base_time = m.event_time or m.valid_from or now
            if base_time.tzinfo is None:
                base_time = base_time.replace(tzinfo=UTC)
            days = max(0.0, (now - base_time).total_seconds() / 86400.0)
            recency = math.exp(-days / 90.0)
            base_importance = max(0.0, min(1.0, m.importance))
            multiplier = multipliers.get(m.memory_type, 1.0)
            importance = max(0.0, min(1.0, base_importance * multiplier))
            relationship = min(1.0, memory_links.get(m.id, 0.0))
            confidence = max(0.0, min(1.0, m.confidence))
            components = {
                "semantic": round(semantic, 4),
                "semantic_raw": round(semantic_raw, 4),
                "keyword": round(keyword, 4),
                "recency": round(recency, 4),
                "importance": round(importance, 4),
                "importance_base": round(base_importance, 4),
                "personalization": round(multiplier, 4),
                "relationship": round(relationship, 4),
                "confidence": round(confidence, 4),
                # Informational embedding provenance (zero weight; the six
                # weighted components above are the locked formula).
                "embedding_legacy": 1.0 if mem_version is None else 0.0,
                "embedding_comparable": 1.0 if comparable else 0.0,
                "embedding_degraded": 1.0 if self.embedding_degraded else 0.0,
            }
            score = sum(effective_weights[k] * components[k] for k in effective_weights)
            if score < min_score:
                continue
            scored.append(
                RetrievedMemory(
                    memory_id=str(m.id),
                    text=m.text,
                    memory_type=m.memory_type,
                    payload=m.payload,
                    importance=m.importance,
                    confidence=m.confidence,
                    event_time=m.event_time,
                    privacy_level=m.privacy_level,
                    source_type=m.source_type,
                    score=round(score, 4),
                    components=components,
                    source_event_ids=prov.get(m.id, []),
                )
            )
        scored.sort(key=lambda r: r.score, reverse=True)
        _note_stage("fusion_score", (_time.perf_counter() - t0) * 1000.0)
        if (
            rerank
            and settings.reranker_enabled
            and k <= settings.reranker_final_k
            and scored
        ):
            from app.rerank import get_reranker, should_rerank

            if should_rerank(scored, k=k):
                reranker = get_reranker()
                if reranker is not None:
                    top_n = min(settings.reranker_candidates, len(scored))
                    reranked = await reranker.rerank(
                        query,
                        scored[:top_n],
                        final_k=k,
                    )
                    self.rerank_stats["triggered"] += 1
                    self.rerank_stats["runs"] += 1
                    self.rerank_stats["latency_ms"] += reranked.latency_ms
                    self.rerank_stats["candidates"] += reranked.candidates
                    if reranked.degraded:
                        self.rerank_stats["degraded"] += 1
                    if not reranked.degraded and reranked.results:
                        return reranked.results
        return scored[:k]

    async def search_events(
        self,
        query: str,
        *,
        k: int = 20,
        access: str = "model",
        include_sensitive: bool = False,
    ) -> list[dict]:
        """Keyword+recency search over the raw event timeline."""
        from app.models import Event

        query_tokens = simple_tokens(query)
        stmt = select(Event).where(Event.tombstoned_at.is_(None)).order_by(Event.occurred_at.desc()).limit(2000)
        if access == "model":
            stmt = stmt.where(Event.privacy_level != "never_send_to_model")
            if not include_sensitive:
                stmt = stmt.where(Event.privacy_level != "sensitive")
        rows = (await self.session.execute(stmt)).scalars().all()
        now = datetime.now(UTC)
        scored = []
        for event in rows:
            text = (event.content or {}).get("text") or ""
            tokens = simple_tokens(text)
            keyword = (
                len(query_tokens & tokens) / len(query_tokens | tokens)
                if query_tokens and tokens
                else 0.0
            )
            occurred_at = event.occurred_at
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=UTC)
            days = max(0.0, (now - occurred_at).total_seconds() / 86400.0)
            recency = math.exp(-days / 90.0)
            score = 0.6 * keyword + 0.4 * recency
            if score > 0:
                scored.append(
                    {
                        "id": str(event.id),
                        "occurred_at": event.occurred_at.isoformat(),
                        "source": event.source,
                        "event_type": event.event_type,
                        "text": text,
                        "score": round(score, 4),
                    }
                )
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:k]
