from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import EntityRef
from app.models import Entity, MemoryEntity
from app.utils.text import normalize_text


def extract_entities_from_text(text: str) -> list[EntityRef]:
    """Lightweight entity extraction: @mentions, relatives, residence."""
    refs: list[EntityRef] = []
    for match in re.findall(r"@([A-Za-z0-9_]+)", text):
        refs.append(EntityRef(name=match, entity_type="topic"))
    for relation, name in re.findall(
        r"(?:my|our)\s+(friend|colleague|boss|manager|mom|dad|mother|father|brother|sister|wife|husband|partner|girlfriend|boyfriend|roommate|neighbor)\s+([A-Z][a-z]+)",
        text,
    ):
        refs.append(EntityRef(name=name, entity_type="person", role=relation, weight=1.0))
    for place in re.findall(
        r"(?:live|lives|moved|based)\s+(?:in|to|at)\s+([A-Z][a-zA-Z.\-]+(?:\s+[A-Z][a-zA-Z.\-]+)?)",
        text,
    ):
        refs.append(EntityRef(name=place, entity_type="place"))
    # Deduplicate by (type, name).
    seen: set[tuple[str, str]] = set()
    unique: list[EntityRef] = []
    for ref in refs:
        key = (ref.entity_type, normalize_text(ref.name))
        if key not in seen:
            seen.add(key)
            unique.append(ref)
    return unique


async def get_or_create_entity(session: AsyncSession, name: str, entity_type: str) -> Entity:
    canonical = f"{entity_type}:{normalize_text(name)}"
    result = await session.execute(select(Entity).where(Entity.canonical_key == canonical))
    entity = result.scalar_one_or_none()
    if entity is not None:
        return entity
    entity = Entity(name=name, entity_type=entity_type, canonical_key=canonical)
    session.add(entity)
    await session.flush()
    return entity


async def link_entities(
    session: AsyncSession,
    memory_id,
    refs: list[EntityRef],
) -> list:
    linked: list[dict] = []
    for ref in refs:
        entity = await get_or_create_entity(session, ref.name, ref.entity_type)
        session.add(
            MemoryEntity(
                memory_id=memory_id,
                entity_id=entity.id,
                role=ref.role,
                weight=ref.weight,
            )
        )
        linked.append(
            {"id": str(entity.id), "name": entity.name, "entity_type": entity.entity_type, "role": ref.role}
        )
    return linked

