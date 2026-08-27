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
        try:
            query_emb = (await self.embeddings.embed([query]))[0]
        except Exception:
            query_emb = None

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
        result = await self.session.execute(stmt)
        memories = list(result.scalars().all())

        # Entity overlap map: memory_id -> max overlap weight.
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

        # Provenance map.
        prov: dict = {}
        if memories:
            mem_ids = [m.id for m in memories]
            prov_rows = (
                await self.session.execute(select(MemoryEvent).where(MemoryEvent.memory_id.in_(mem_ids)))
            ).scalars().all()
            for row in prov_rows:
                prov.setdefault(row.memory_id, []).append(str(row.event_id))

        # Evidence-backed importance learning (consent-gated, versioned, neutral by default).
        multipliers = await calibration_multipliers(self.session)
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

        scored: list[RetrievedMemory] = []
        for m in memories:
            mem_version = m.embedding_model_version
            comparable = mem_version is None or mem_version == self.embedding_model_version
            semantic_raw = raw_semantics.get(m.id, 0.0)
            semantic = (
                (semantic_raw - semantic_min) / semantic_span
                if settings.semantic_normalize and semantic_span > 1e-9
                else semantic_raw
            )
            mem_tokens = simple_tokens(m.text)
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
