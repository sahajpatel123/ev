"""Person finder: locate a person across user-owned memory (never camera scanning)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entity, Event, Memory, MemoryEntity
from app.schemas import PersonWhereaboutsOut
from app.utils.text import normalize_text


async def whereabouts(session: AsyncSession, name: str) -> PersonWhereaboutsOut:
    normalized = normalize_text(name)
    entity = None
    relationship = None
    result = await session.execute(
        select(Entity).where(
            Entity.entity_type == "person",
            Entity.canonical_key == f"person:{normalized}",
        )
    )
    entity = result.scalar_one_or_none()
    if entity is None:
        result = await session.execute(
            select(Entity)
            .where(Entity.entity_type == "person", Entity.name.ilike(f"%{name}%"))
            .limit(5)
        )
        entity = result.scalars().first()

    # Mentions across events.
    mention_stmt = (
        select(Event)
        .where(Event.tombstoned_at.is_(None))
        .order_by(Event.occurred_at.desc())
        .limit(2000)
    )
    events = list((await session.execute(mention_stmt)).scalars().all())
    mentions = [
        e
        for e in events
        if normalized in normalize_text((e.content or {}).get("text") or "")
    ]
    recent_mentions = [
        {
            "event_id": str(e.id),
            "occurred_at": e.occurred_at.isoformat(),
            "source": e.source,
            "event_type": e.event_type,
            "text": ((e.content or {}).get("text") or "")[:240],
        }
        for e in mentions[:5]
    ]

    related_memories: list[dict] = []
    if entity is not None:
        links = (
            await session.execute(
                select(MemoryEntity, Memory)
                .join(Memory, Memory.id == MemoryEntity.memory_id)
                .where(
                    MemoryEntity.entity_id == entity.id,
                    Memory.is_current.is_(True),
                    Memory.redacted.is_(False),
                )
                .order_by(Memory.event_time.desc())
                .limit(10)
            )
        ).all()
        for link, memory in links:
            if relationship is None and link.role != "related":
                relationship = link.role
            related_memories.append(
                {
                    "memory_id": str(memory.id),
                    "memory_type": memory.memory_type,
                    "text": memory.text,
                    "event_time": memory.event_time.isoformat(),
                    "confidence": memory.confidence,
                }
            )

    last_seen = None
    if mentions:
        latest = mentions[0]
        last_seen = {
            "occurred_at": latest.occurred_at.isoformat(),
            "event_id": str(latest.id),
            "source": latest.source,
            "text": ((latest.content or {}).get("text") or "")[:240],
        }

    return PersonWhereaboutsOut(
        name=name,
        entity_id=entity.id if entity else None,
        relationship=relationship,
        last_seen=last_seen,
        recent_mentions=recent_mentions,
        related_memories=related_memories,
        total_events=len(mentions),
    )
