from __future__ import annotations

import math
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.contracts import RetrievedMemory
from app.embeddings import get_embedder
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
    ) -> list[RetrievedMemory]:
        """Hybrid retrieval with the locked default scoring formula."""
        if not query.strip():
            return []
        query_tokens = simple_tokens(query)
        query_entities = {e.name.lower() for e in extract_entities_from_text(query)}
        try:
            query_emb = (await self.embeddings.embed([query]))[0]
        except Exception:
            query_emb = None

        stmt = (
            select(Memory)
            .where(Memory.is_current.is_(True), Memory.redacted.is_(False))
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
        scored: list[RetrievedMemory] = []
        for m in memories:
            semantic = cosine(query_emb, m.embedding) if query_emb is not None and m.embedding else 0.0
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
                "keyword": round(keyword, 4),
                "recency": round(recency, 4),
                "importance": round(importance, 4),
                "importance_base": round(base_importance, 4),
                "personalization": round(multiplier, 4),
                "relationship": round(relationship, 4),
                "confidence": round(confidence, 4),
            }
            score = sum(SCORE_WEIGHTS[k] * v for k, v in components.items())
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
