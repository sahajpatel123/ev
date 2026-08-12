"""Deterministic regeneration of every derived memory from the immutable event log.

This is the durable invariant behind FR-SYS-05: raw events are the permanent source
of truth; all derived rows (memories, entities, relationships, conflicts, patterns)
can be dropped and replayed back into an equivalent state. No Event row is ever
updated or deleted here.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings import get_embedder
from app.ev.decisions import recreate_lesson_from_event
from app.ev.memory_ops import apply_correction, apply_forget, apply_restore
from app.ev.research import recreate_conclusion_memory
from app.ev.vision import recognition_memory_candidate
from app.memory.entities import apply_entity_merge_event, get_or_create_entity
from app.memory.extraction import Extractor
from app.memory.llm_extractor import replay_llm_extraction_event
from app.memory.patterns import PatternEngine
from app.memory.writer import MemoryWriter, redact_memories_for_event
from app.models import (
    Conflict,
    DecisionOutcome,
    Entity,
    EntityRelationship,
    Event,
    MakerProject,
    Memory,
    MemoryEntity,
    MemoryEvent,
    RecognitionLog,
)
from app.services.access_log import log_access
from app.services.consolidation import run_consolidation, run_state_of_me
from app.utils.text import utcnow

MEMORY_OPERATION_TYPES = {"memory.correction", "memory.forget", "memory.restore"}
RESEARCH_RAW_TYPES = {"research.session", "research.note"}


async def _count(session: AsyncSession, model) -> int:
    result = await session.execute(select(func.count()).select_from(model))
    return int(result.scalar_one())


async def _find_memory(
    session: AsyncSession,
    fingerprint_value: str,
    *,
    is_current: bool,
    memory_type: str | None = None,
) -> Memory | None:
    stmt = select(Memory).where(
        Memory.fingerprint == fingerprint_value,
        Memory.is_current.is_(is_current),
    )
    if memory_type:
        stmt = stmt.where(Memory.memory_type == memory_type)
    stmt = stmt.order_by(Memory.version.desc())
    result = await session.execute(stmt)
    return result.scalars().first()


async def _apply_memory_operation(session: AsyncSession, event: Event) -> bool:
    """Replay a correction/forget/restore event against the rebuilt memory layer."""
    meta = event.metadata_ or {}
    memory_type = meta.get("memory_type")

    if event.event_type == "memory.correction":
        supersedes_fp = meta.get("supersedes_fingerprint")
        if not supersedes_fp:
            return False
        memory = await _find_memory(session, supersedes_fp, is_current=True, memory_type=memory_type)
        if memory is None:
            return False
        corrected_text = (event.content or {}).get("text") or ""
        if not corrected_text:
            return False
        await apply_correction(
            session,
            memory,
            corrected_text=corrected_text,
            reason=meta.get("reason") or "replayed correction",
            event=event,
        )
        return True

    fingerprint_value = meta.get("fingerprint")
    if not fingerprint_value:
        return False
    if event.event_type == "memory.forget":
        memory = await _find_memory(session, fingerprint_value, is_current=True, memory_type=memory_type)
        if memory is None:
            return False
        await apply_forget(session, memory, reason=meta.get("reason") or "replayed forget", event=event)
        return True
    if event.event_type == "memory.restore":
        memory = await _find_memory(session, fingerprint_value, is_current=False, memory_type=memory_type)
        if memory is None:
            return False
        await apply_restore(session, memory, event=event)
        return True
    return False


async def rebuild_derived_state(
    session: AsyncSession,
    *,
    actor: str = "api",
    reason: str = "manual rebuild",
    request_id: str | None = None,
) -> dict:
    """Drop all derived memory rows and replay every raw event into equivalent state."""
    events = list(
        (
            await session.execute(
                select(Event).order_by(Event.occurred_at.asc(), Event.id.asc())
            )
        ).scalars().all()
    )

    deleted = {
        "memories": await _count(session, Memory),
        "entities": await _count(session, Entity),
        "relationships": await _count(session, EntityRelationship),
        "conflicts": await _count(session, Conflict),
    }

    # Detach operational rows that reference derived rows, then drop the derived layer.
    await session.execute(
        update(DecisionOutcome).values(decision_memory_id=None, lesson_memory_id=None)
    )
    await session.execute(update(MakerProject).values(goal_memory_id=None))
    await session.execute(update(RecognitionLog).values(entity_id=None))
    await session.execute(delete(Conflict))
    await session.execute(delete(MemoryEntity))
    await session.execute(delete(MemoryEvent))
    await session.execute(delete(EntityRelationship))
    await session.execute(delete(Memory))
    await session.execute(delete(Entity))
    await session.flush()

    writer = MemoryWriter(session, embeddings=get_embedder())
    extractor = Extractor()
    counts = {
        "events_replayed": 0,
        "memories_created": 0,
        "patterns_created": 0,
        "summaries_created": 0,
        "lessons_created": 0,
        "llm_extractions_replayed": 0,
        "rollups_created": 0,
        "merges_applied": 0,
        "operations_applied": 0,
    }

    for event in events:
        counts["events_replayed"] += 1
        if event.event_type == "message.assistant":
            continue
        if event.event_type in MEMORY_OPERATION_TYPES:
            if await _apply_memory_operation(session, event):
                counts["operations_applied"] += 1
            continue
        if event.event_type == "research.conclusion":
            if await recreate_conclusion_memory(session, event) is not None:
                counts["summaries_created"] += 1
            continue
        if event.event_type == "decision.outcome":
            if await recreate_lesson_from_event(session, event) is not None:
                counts["lessons_created"] += 1
            continue
        if event.event_type == "recognition.confirm":
            content = event.content or {}
            recognition_id = content.get("recognition_id")
            recognition = None
            if recognition_id:
                recognition = await session.get(RecognitionLog, UUID(str(recognition_id)))
            label = str(content.get("label") or (recognition.label if recognition else "")).strip()
            if not label:
                continue
            entity_type = str(content.get("entity_type") or "thing")
            entity = await get_or_create_entity(session, label, entity_type)
            if recognition is not None:
                recognition.entity_id = entity.id
                recognition.source = "user"
            try:
                confidence = float(content.get("confidence") or 0.8)
            except (TypeError, ValueError):
                confidence = 0.8
            written_memories = await writer.write_all(
                event,
                [
                    recognition_memory_candidate(
                        label=label,
                        confidence=confidence,
                        entity_type=entity_type,
                        recognition_id=str(recognition_id or ""),
                        attachment_id=content.get("attachment_id"),
                        perception_event_id=content.get("perception_event_id"),
                        source_event_id=content.get("source_event_id"),
                        entity_id=str(entity.id),
                        privacy_level=event.privacy_level or "normal",
                        event_time=event.occurred_at,
                    )
                ],
            )
            counts["memories_created"] += len(written_memories)
            continue
        if event.event_type == "pattern.analyze":
            meta = event.metadata_ or {}
            try:
                as_of = datetime.fromisoformat(meta["executed_at"])
            except (KeyError, TypeError, ValueError):
                as_of = None
            written = await PatternEngine(session, embeddings=get_embedder()).analyze(
                window_days=int(meta.get("window_days", 30)),
                min_count=int(meta.get("min_count", 3)),
                as_of=as_of,
            )
            counts["patterns_created"] += len(written)
            continue
        if event.event_type == "consolidation.run":
            meta = event.metadata_ or {}
            try:
                written = await run_consolidation(
                    session,
                    granularity=meta["granularity"],
                    period_start=datetime.fromisoformat(meta["period_start"]),
                    period_end=datetime.fromisoformat(meta["period_end"]),
                    as_of=datetime.fromisoformat(meta["executed_at"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            counts["summaries_created"] += len(written)
            continue
        if event.event_type == "extraction.llm":
            counts["llm_extractions_replayed"] += 1
            counts["memories_created"] += await replay_llm_extraction_event(session, event)
            continue
        if event.event_type == "entity.merge":
            await apply_entity_merge_event(session, event)
            counts["merges_applied"] += 1
            continue
        if event.event_type == "rollup.run":
            meta = event.metadata_ or {}
            try:
                written = await run_state_of_me(
                    session,
                    period_start=datetime.fromisoformat(meta["period_start"]),
                    period_end=datetime.fromisoformat(meta["period_end"]),
                    as_of=datetime.fromisoformat(meta["executed_at"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            counts["rollups_created"] += len(written)
            continue
        if event.event_type in RESEARCH_RAW_TYPES:
            continue
        results = await writer.write_all(event, extractor.extract(event))
        counts["memories_created"] += len(results)
    await session.flush()

    # Tombstones redact every derived row whose provenance includes the event.
    tombstoned = 0
    redacted = 0
    for event in events:
        if event.tombstoned_at is not None:
            tombstoned += 1
            redacted += await redact_memories_for_event(session, event.id)
    await session.flush()

    await log_access(
        session,
        actor=actor,
        action="rebuild",
        endpoint="POST /v1/memory/rebuild",
        resource_type="derived",
        resource_ids=[],
        request_id=request_id,
        details={"reason": reason, **counts},
    )

    return {
        "completed_at": utcnow(),
        "reason": reason,
        "events_total": len(events),
        **counts,
        "tombstoned_events": tombstoned,
        "redacted_memories": redacted,
        "deleted_memories": deleted["memories"],
        "deleted_entities": deleted["entities"],
        "deleted_relationships": deleted["relationships"],
        "deleted_conflicts": deleted["conflicts"],
    }
