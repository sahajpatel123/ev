"""Whole-life recall (plan 2.5): reconstruct any past week with provenance.

The endpoint returns the raw event stream for a week, the memory state as it
existed at the end of that week (versioned time-travel view), any weekly
period-summary consolidation, and the decisions/goals active then — so a
"what happened that week?" question is answered from stored data, not from the
model's memory.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.patterns import classify_topic
from app.models import Event, Memory
from app.schemas import (
    EventOut,
    WeekRecallConsolidationOut,
    WeekRecallMemoryOut,
    WeekRecallOut,
)


def _compact_memory(memory: Memory) -> WeekRecallMemoryOut:
    return WeekRecallMemoryOut(
        id=memory.id,
        memory_type=memory.memory_type,
        text=memory.text,
        importance=memory.importance,
        event_time=memory.event_time,
        payload=memory.payload or {},
    )


async def reconstruct_week(
    session: AsyncSession,
    *,
    week_start: datetime,
    limit_events: int = 500,
    limit_memories: int = 300,
) -> WeekRecallOut:
    """Reconstruct one week: events + end-of-week memory state + consolidation."""
    if week_start.tzinfo is None:
        week_start = week_start.replace(tzinfo=UTC)
    week_end = week_start + timedelta(days=7)
    as_of = week_end

    events = list(
        (
            await session.execute(
                select(Event)
                .where(
                    Event.occurred_at >= week_start,
                    Event.occurred_at < week_end,
                    Event.tombstoned_at.is_(None),
                )
                .order_by(Event.occurred_at.asc(), Event.id.asc())
                .limit(limit_events)
            )
        ).scalars().all()
    )

    # Time-travel view: what did EV know at the end of the week? The versioned
    # validity window (valid_from <= as_of < valid_until) is authoritative; the
    # current-version flag alone would silently drop later-superseded memories.
    memories = list(
        (
            await session.execute(
                select(Memory)
                .where(
                    Memory.valid_from <= as_of,
                    (Memory.valid_until.is_(None)) | (Memory.valid_until > as_of),
                    Memory.redacted.is_(False),
                )
                .order_by(Memory.importance.desc(), Memory.event_time.desc())
                .limit(limit_memories)
            )
        ).scalars().all()
    )

    summaries = (
        await session.execute(
            select(Memory).where(
                Memory.memory_type == "summary",
                Memory.redacted.is_(False),
            )
        )
    ).scalars().all()
    consolidation: Memory | None = None
    for memory in summaries:
        payload = memory.payload or {}
        if (
            payload.get("kind") == "period_summary"
            and payload.get("granularity") == "week"
            and payload.get("period_start") == week_start.isoformat()
        ):
            consolidation = memory
            break

    topics: Counter[str] = Counter()
    for event in events:
        text = (event.content or {}).get("text") or ""
        if text.strip():
            topic, _kind = classify_topic(text)
            if topic:
                topics[topic] += 1

    decisions = [m for m in memories if m.memory_type == "decision"]
    goals = [m for m in memories if m.memory_type == "goal"]
    compact_memories = [_compact_memory(m) for m in memories]

    consolidation_out: WeekRecallConsolidationOut | None = None
    if consolidation is not None:
        payload = consolidation.payload or {}
        consolidation_out = WeekRecallConsolidationOut(
            period_start=week_start,
            period_end=week_end,
            summary=consolidation.text,
            topics=payload.get("topics") or [],
            event_count=payload.get("event_count") or len(events),
        )

    return WeekRecallOut(
        week_start=week_start,
        week_end=week_end,
        as_of=as_of,
        events=[EventOut.model_validate(e) for e in events],
        memories=compact_memories,
        decisions=[_compact_memory(m) for m in decisions],
        goals=[_compact_memory(m) for m in goals],
        consolidation=consolidation_out,
        event_count=len(events),
        memory_count=len(memories),
        top_topics=[topic for topic, _count in topics.most_common(5)],
    )
