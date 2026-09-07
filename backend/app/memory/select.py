"""Select a small high-quality memory set for the current turn.

Modes:
  continuation — recent/working context; light retrieval
  implicit     — only memories that actually match this utterance
  explicit     — broader recall, including episodes and the event timeline
  fresh        — no old-topic injection
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import RetrievedMemory
from app.ev.continuity import (
    MemoryIntent,
    classify_memory_intent,
    conversation_time_requested,
    wants_historical_truth,
)
from app.ev.memory_ops import apply_forget
from app.memory.episodes import recent_episodes
from app.memory.observe import log_memory
from app.memory.retrieval import Retriever
from app.memory.temporal import resolve_temporal_expressions
from app.models import Event, Memory, MemoryEvent
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.utils.text import simple_tokens, utcnow

IMPLICIT_MIN_SCORE = 0.32
CONTINUATION_MIN_SCORE = 0.18
EXPLICIT_MIN_SCORE = 0.0
INTRUSION_TYPES = {"observation"}
DURABLE_TYPES = {"preference", "fact", "decision", "goal", "summary", "pattern", "lesson"}


def _lexical_overlap(query: str, text: str) -> float:
    left = simple_tokens(query)
    right = simple_tokens(text)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _keep_implicit(query: str, memory: RetrievedMemory) -> bool:
    if memory.memory_type in INTRUSION_TYPES and memory.score < 0.55:
        return False
    keyword = _lexical_overlap(query, memory.text)
    relationship = float((memory.components or {}).get("relationship") or 0.0)
    semantic_raw = float((memory.components or {}).get("semantic_raw") or 0.0)
    if keyword >= 0.08 or relationship > 0:
        return True
    return semantic_raw >= 0.35 and memory.score >= 0.45


async def select_context_memories(
    session: AsyncSession,
    query: str,
    *,
    k: int,
    access: str = "model",
    include_sensitive: bool = False,
) -> tuple[MemoryIntent, list[RetrievedMemory]]:
    intent = classify_memory_intent(query)
    retriever = Retriever(session)
    log_memory("memory.retrieval_started", extra={"intent": intent, "k": k})
    if intent == "fresh":
        hits = await retriever.search(
            query,
            k=max(8, k),
            access=access,
            include_sensitive=include_sensitive,
            min_score=IMPLICIT_MIN_SCORE,
            memory_types=list(DURABLE_TYPES),
        )
        selected = [hit for hit in hits if _keep_implicit(query, hit)][: min(4, k)]
        _log_selected(intent, selected)
        return intent, selected
    if intent == "explicit_recall":
        types = list(DURABLE_TYPES | {"episodic", "observation"})
        since, until = _temporal_window(query)
        historical = wants_historical_truth(query)
        hits = await retriever.search(
            query,
            k=max(k, 12),
            access=access,
            include_sensitive=include_sensitive,
            min_score=EXPLICIT_MIN_SCORE,
            memory_types=types,
            include_historical=historical,
        )
        if since is not None or until is not None:
            timed = [hit for hit in hits if _in_window(hit.event_time, since, until)]
            hits = timed
        episodes = await recent_episodes(session, k=5)
        extra = [
            RetrievedMemory(
                memory_id=str(row.id),
                text=row.text,
                memory_type="summary",
                payload=row.payload or {},
                importance=row.importance,
                confidence=row.confidence,
                event_time=row.event_time,
                privacy_level=row.privacy_level,
                source_type=row.source_type,
                score=0.7,
                components={"reason": 1.0},
            )
            for row in episodes
            if since is None or _in_window(row.event_time, since, until)
        ]
        merged = _dedupe([*extra, *hits])[: max(k, 12)]
        _log_selected(intent, merged, explicit=True)
        return intent, merged
    hits = await retriever.search(
        query,
        k=k,
        access=access,
        include_sensitive=include_sensitive,
        min_score=CONTINUATION_MIN_SCORE,
    )
    selected = hits[:k]
    _log_selected(intent, selected)
    return intent, selected


async def explicit_recall_payload(
    session: AsyncSession,
    query: str,
    *,
    k: int = 10,
    memory_type_hint: str | None = None,
) -> dict:
    from app.memory.recall import build_explicit_recall_payload

    return await build_explicit_recall_payload(
        session, query, k=k, memory_type_hint=memory_type_hint
    )


async def apply_forget_intent(
    session: AsyncSession,
    text: str,
    *,
    conversation_id: UUID | None,
) -> int:
    retriever = Retriever(session)
    hits = await retriever.search(text, k=5, access="owner", min_score=0.2)
    if not hits:
        return 0
    event = await EventService(session, actor="owner").create(
        EventCreate(
            source="voice",
            event_type="memory.forget",
            text=text,
            conversation_id=conversation_id,
            metadata={"intent": "forget"},
        )
    )
    forgotten = 0
    for hit in hits[:3]:
        memory = await session.get(Memory, UUID(hit.memory_id))
        if memory is None or not memory.is_current:
            continue
        await apply_forget(session, memory, reason=text[:240], event=event)
        forgotten += 1
        log_memory("memory.superseded", extra={"memory_id": hit.memory_id, "reason": "forget"})
    return forgotten


async def apply_pin_intent(
    session: AsyncSession,
    *,
    conversation_id: UUID | None,
) -> int:
    rows: list[Memory] = []
    if conversation_id is not None:
        event_ids = list(
            (
                await session.execute(
                    select(Event.id)
                    .where(
                        Event.conversation_id == conversation_id,
                        Event.event_type == "message.user",
                        Event.tombstoned_at.is_(None),
                    )
                    .order_by(Event.occurred_at.desc())
                    .limit(4)
                )
            ).scalars().all()
        )
        if event_ids:
            memory_ids = list(
                (
                    await session.execute(
                        select(MemoryEvent.memory_id).where(MemoryEvent.event_id.in_(event_ids))
                    )
                ).scalars().all()
            )
            if memory_ids:
                rows = list(
                    (
                        await session.execute(
                            select(Memory).where(
                                Memory.id.in_(memory_ids),
                                Memory.is_current.is_(True),
                                Memory.redacted.is_(False),
                            )
                        )
                    ).scalars().all()
                )
    if not rows:
        rows = list(
            (
                await session.execute(
                    select(Memory)
                    .where(
                        Memory.is_current.is_(True),
                        Memory.redacted.is_(False),
                        Memory.memory_type.in_(tuple(DURABLE_TYPES | {"episodic", "observation"})),
                    )
                    .order_by(Memory.event_time.desc())
                    .limit(3)
                )
            ).scalars().all()
        )
    camera_ids = list(
        (
            await session.execute(
                select(Event.id)
                .where(
                    Event.event_type == "camera.observation",
                    Event.tombstoned_at.is_(None),
                    Event.occurred_at >= utcnow() - timedelta(minutes=15),
                )
                .order_by(Event.occurred_at.desc())
                .limit(4)
            )
        ).scalars().all()
    )
    if camera_ids:
        camera_memory_ids = list(
            (
                await session.execute(
                    select(MemoryEvent.memory_id).where(MemoryEvent.event_id.in_(camera_ids))
                )
            ).scalars().all()
        )
        if camera_memory_ids:
            extra_rows = list(
                (
                    await session.execute(
                        select(Memory).where(
                            Memory.id.in_(camera_memory_ids),
                            Memory.is_current.is_(True),
                            Memory.redacted.is_(False),
                        )
                    )
                ).scalars().all()
            )
            have = {row.id for row in rows}
            rows = extra_rows + [row for row in rows if row.id not in have]
    pinned = 0
    for memory in rows[:3]:
        extra = dict(memory.extra or {})
        extra["pinned"] = True
        extra["pinned_at"] = utcnow().isoformat()
        memory.extra = extra
        memory.importance = min(1.0, max(memory.importance, 0.92))
        pinned += 1
    log_memory("memory.retrieval_selected", extra={"reason": "pin", "count": pinned})
    return pinned


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _in_window(event_time: datetime | None, since: datetime | None, until: datetime | None) -> bool:
    moment = _as_utc(event_time)
    if moment is None:
        return since is None and until is None
    start = _as_utc(since)
    end = _as_utc(until)
    if start is not None and moment < start:
        return False
    return end is None or moment < end


def _temporal_window(query: str) -> tuple[datetime | None, datetime | None]:
    """Hard-filter only conversation time ('yesterday'), not content time ('in March')."""
    if not conversation_time_requested(query):
        return None, None
    now = utcnow()
    resolved = resolve_temporal_expressions(query, now)
    kept = [
        item
        for item in resolved
        if conversation_time_requested(item.expression)
    ]
    starts_clean: list[datetime] = []
    ends_clean: list[datetime] = []
    for item in kept:
        if item.start is not None:
            start = _as_utc(item.start)
            if start is not None:
                starts_clean.append(start)
        if item.end is not None:
            end = _as_utc(item.end)
            if end is not None:
                ends_clean.append(end)
    if not starts_clean or not ends_clean:
        return None, None
    return min(starts_clean), max(ends_clean)


def _dedupe(rows: list[RetrievedMemory]) -> list[RetrievedMemory]:
    seen: set[str] = set()
    out: list[RetrievedMemory] = []
    for row in rows:
        if row.memory_id in seen:
            continue
        seen.add(row.memory_id)
        out.append(row)
    return out


def _log_selected(
    intent: MemoryIntent, selected: list[RetrievedMemory], *, explicit: bool = False
) -> None:
    log_memory(
        "memory.retrieval_selected",
        extra={
            "intent": intent,
            "count": len(selected),
            "types": ",".join(sorted({hit.memory_type for hit in selected})),
            "explicit": explicit,
        },
    )
    for hit in selected[:8]:
        log_memory(
            "memory.retrieval_candidate",
            extra={
                "memory_id": hit.memory_id,
                "category": hit.memory_type,
                "score": hit.score,
                "age": hit.event_time.isoformat() if hit.event_time else None,
            },
        )
