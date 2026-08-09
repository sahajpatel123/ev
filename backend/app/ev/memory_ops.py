"""Memory correction, forgetting, and restoration (versioned, reversible)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings import get_embedder
from app.models import Memory, MemoryEvent
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.utils.text import fingerprint, normalize_text


async def _copy_provenance(session: AsyncSession, target: Memory, source: Memory) -> None:
    rows = (
        await session.execute(
            select(MemoryEvent).where(MemoryEvent.memory_id == source.id)
        )
    ).scalars().all()
    for row in rows:
        session.add(MemoryEvent(memory_id=target.id, event_id=row.event_id))


async def apply_correction(
    session: AsyncSession,
    memory: Memory,
    *,
    corrected_text: str,
    reason: str,
    event,
) -> Memory:
    """Create the corrected version from an already-recorded raw event."""
    new = Memory(
        memory_type=memory.memory_type,
        text=corrected_text,
        payload={
            **memory.payload,
            "corrected": True,
            "original_text": memory.text,
            "original_memory_id": str(memory.id),
        },
        importance=memory.importance,
        confidence=1.0,
        source_type="explicit",
        privacy_level=memory.privacy_level,
        event_time=event.occurred_at,
        valid_from=event.occurred_at,
        version_group=memory.version_group,
        version=memory.version + 1,
        supersedes_id=memory.id,
        reason_for_change=reason,
        fingerprint=fingerprint(
            {"memory_type": memory.memory_type, "corrected_text": normalize_text(corrected_text)}
        ),
    )
    try:
        new.embedding = (await get_embedder().embed([corrected_text]))[0]
    except Exception:
        new.embedding = None
    session.add(new)
    await session.flush()

    memory.is_current = False
    memory.superseded_by_id = new.id
    memory.valid_until = event.occurred_at
    await _copy_provenance(session, new, memory)
    session.add(MemoryEvent(memory_id=new.id, event_id=event.id))
    return new


async def apply_forget(
    session: AsyncSession,
    memory: Memory,
    *,
    reason: str,
    event,
) -> Memory:
    """Hide a memory from active retrieval using an already-recorded raw event."""
    memory.is_current = False
    memory.valid_until = event.occurred_at
    memory.payload = {
        **memory.payload,
        "forgotten": True,
        "forgotten_at": event.occurred_at.isoformat(),
        "forget_reason": reason,
    }
    return memory


async def apply_restore(session: AsyncSession, memory: Memory, *, event) -> Memory:
    """Reverse a forget using an already-recorded raw event."""
    memory.is_current = True
    memory.valid_until = None
    memory.payload = {**memory.payload, "forgotten": False, "restored_at": event.occurred_at.isoformat()}
    return memory


async def correct_memory(
    session: AsyncSession,
    memory_id: UUID,
    *,
    corrected_text: str,
    reason: str = "user correction",
    actor: str = "api",
) -> Memory:
    """Create a new current version with the correction; v1 stays intact."""
    memory = await session.get(Memory, memory_id)
    if memory is None:
        raise KeyError(f"Memory {memory_id} not found")
    if not memory.is_current:
        raise ValueError("Only the current memory version can be corrected")

    event = await EventService(session, actor=actor).create(
        EventCreate(
            source="memory",
            event_type="memory.correction",
            text=corrected_text,
            metadata={
                "memory_id": str(memory.id),
                "memory_type": memory.memory_type,
                "reason": reason,
                "supersedes_fingerprint": memory.fingerprint,
                "fingerprint": fingerprint(
                    {
                        "memory_type": memory.memory_type,
                        "corrected_text": normalize_text(corrected_text),
                    }
                ),
            },
        )
    )
    return await apply_correction(session, memory, corrected_text=corrected_text, reason=reason, event=event)


async def forget_memory(
    session: AsyncSession,
    memory_id: UUID,
    *,
    reason: str = "user requested",
    actor: str = "api",
) -> Memory:
    """Hide from active retrieval; raw events and history are preserved."""
    memory = await session.get(Memory, memory_id)
    if memory is None:
        raise KeyError(f"Memory {memory_id} not found")
    if not memory.is_current:
        raise ValueError("Memory is already inactive")
    event = await EventService(session, actor=actor).create(
        EventCreate(
            source="memory",
            event_type="memory.forget",
            text=f"Forget: {memory.text}",
            metadata={
                "memory_id": str(memory.id),
                "memory_type": memory.memory_type,
                "reason": reason,
                "fingerprint": memory.fingerprint,
            },
        )
    )
    return await apply_forget(session, memory, reason=reason, event=event)


async def restore_memory(
    session: AsyncSession,
    memory_id: UUID,
    *,
    actor: str = "api",
) -> Memory:
    """Reverse a forget; history remains auditable."""
    memory = await session.get(Memory, memory_id)
    if memory is None:
        raise KeyError(f"Memory {memory_id} not found")
    if memory.is_current:
        return memory
    event = await EventService(session, actor=actor).create(
        EventCreate(
            source="memory",
            event_type="memory.restore",
            text=f"Restore: {memory.text}",
            metadata={
                "memory_id": str(memory.id),
                "memory_type": memory.memory_type,
                "reason": "user requested restore",
                "fingerprint": memory.fingerprint,
            },
        )
    )
    return await apply_restore(session, memory, event=event)
