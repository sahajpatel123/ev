"""Entity merge orchestration: human-confirmed, event-sourced, rebuildable."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.entities import merge_entities
from app.models import Entity
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.utils.text import utcnow


async def confirm_entity_merge(
    session: AsyncSession,
    *,
    target_entity_id: UUID,
    absorbed_entity_id: UUID,
    reason: str,
    actor: str = "api",
) -> dict:
    """Record an ``entity.merge`` event, then apply it to the derived layer."""
    target = await session.get(Entity, target_entity_id)
    absorbed = await session.get(Entity, absorbed_entity_id)
    if target is None:
        raise KeyError(f"Entity {target_entity_id} not found")
    if absorbed is None:
        raise KeyError(f"Entity {absorbed_entity_id} not found")
    if target.id == absorbed.id:
        raise ValueError("Cannot merge an entity into itself")
    if target.entity_type != absorbed.entity_type:
        raise ValueError("Merge is only supported within the same entity type")

    occurred_at = utcnow()
    event = await EventService(session, actor=actor).create(
        EventCreate(
            source="entity",
            event_type="entity.merge",
            text=f"Entity merge: {absorbed.name} into {target.name}",
            content={
                "target_canonical_key": target.canonical_key,
                "absorbed_canonical_key": absorbed.canonical_key,
                "reason": reason,
            },
            metadata={
                "target_entity_id": str(target.id),
                "absorbed_entity_id": str(absorbed.id),
                "occurred_at": occurred_at.isoformat(),
            },
            occurred_at=occurred_at,
        )
    )
    await session.flush()
    report = await merge_entities(
        session,
        target=target,
        absorbed=absorbed,
        event=event,
        reason=reason,
    )
    return {
        "event_id": str(event.id),
        "target": {
            "id": str(target.id),
            "name": target.name,
            "entity_type": target.entity_type,
            "aliases": target.aliases,
            "canonical_key": target.canonical_key,
        },
        "absorbed": {
            "id": str(absorbed.id),
            "name": absorbed.name,
            "entity_type": absorbed.entity_type,
            "summary": absorbed.summary,
        },
        **report,
    }
