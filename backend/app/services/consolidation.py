"""Long-horizon consolidation: deterministic period summaries over raw events.

Period summaries are derived memories (``memory_type="summary"``) with
``kind="period_summary"``. Each consolidation run is recorded as an immutable
``consolidation.run`` event so the derived summary can be regenerated
deterministically by the rebuild pipeline: the job's ``executed_at`` anchors
the event window, and provenance links the summary to every source event in
the period.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings import get_embedder
from app.memory.extraction import Extractor
from app.memory.patterns import classify_topic
from app.models import Event, Memory, MemoryEvent
from app.utils.text import fingerprint, utcnow

GRANULARITIES = ("day", "week", "month")
SUMMARY_KINDS = {"period_summary"}


def next_period_start(period_start: datetime, granularity: str) -> datetime:
    """First instant of the period following ``period_start``."""
    if granularity == "day":
        return period_start + timedelta(days=1)
    if granularity == "week":
        return period_start + timedelta(days=7)
    if granularity == "month":
        year = period_start.year + (period_start.month // 12)
        month = (period_start.month % 12) + 1
        return period_start.replace(year=year, month=month, day=1)
    raise ValueError(f"Unsupported granularity: {granularity}")


def _period_key(memory: Memory) -> tuple[str, str] | None:
    payload = memory.payload or {}
    if payload.get("kind") != "period_summary":
        return None
    return (payload.get("granularity") or "", payload.get("period_start") or "")


async def _find_current_period_summary(
    session: AsyncSession,
    granularity: str,
    period_start: datetime,
) -> Memory | None:
    rows = (
        await session.execute(
            select(Memory).where(
                Memory.memory_type == "summary",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
        )
    ).scalars().all()
    expected = (granularity, period_start.isoformat())
    return next((m for m in rows if _period_key(m) == expected), None)


async def _link_events(session: AsyncSession, memory: Memory, event_ids: list[UUID]) -> None:
    existing = {
        row.event_id
        for row in (
            await session.execute(
                select(MemoryEvent).where(MemoryEvent.memory_id == memory.id)
            )
        ).scalars().all()
    }
    for event_id in event_ids:
        if event_id not in existing:
            session.add(MemoryEvent(memory_id=memory.id, event_id=event_id))


async def run_consolidation(
    session: AsyncSession,
    *,
    granularity: str,
    period_start: datetime,
    period_end: datetime,
    as_of: datetime | None = None,
) -> list[str]:
    """Derive one period summary from the raw events inside the period.

    ``as_of`` replays a historical ``consolidation.run`` job: the event window
    is anchored at the job's execution time and current tombstones are applied
    later by the rebuild redaction pass. Live runs (``as_of=None``) exclude
    already-tombstoned events.
    """
    if granularity not in GRANULARITIES:
        raise ValueError(f"Unsupported granularity: {granularity}")
    if period_end <= period_start:
        raise ValueError("period_end must be after period_start")

    stmt = select(Event).where(
        Event.occurred_at >= period_start,
        Event.occurred_at < period_end,
        Event.event_type != "message.assistant",
        Event.event_type != "consolidation.run",
        Event.event_type != "pattern.analyze",
    )
    if as_of is not None:
        # Replay the job's historical view: only events known (ingested) by the
        # job's execution time that were not already tombstoned then.
        stmt = stmt.where(
            Event.ingested_at <= as_of,
            (Event.tombstoned_at.is_(None)) | (Event.tombstoned_at > as_of),
        )
    else:
        stmt = stmt.where(Event.tombstoned_at.is_(None))
    rows = list((await session.execute(stmt.order_by(Event.occurred_at, Event.id))).scalars().all())
    if not rows:
        return []

    topics: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    extractor = Extractor()
    for event in rows:
        text = (event.content or {}).get("text") or ""
        if text.strip():
            topic, _kind = classify_topic(text)
            if topic:
                topics[topic] += 1
            for candidate in extractor.extract(event):
                if candidate.memory_type in (
                    "decision",
                    "preference",
                    "goal",
                    "fact",
                    "observation",
                ):
                    type_counts[candidate.memory_type] += 1

    event_ids = [event.id for event in rows]
    executed_at = as_of or utcnow()
    payload = {
        "kind": "period_summary",
        "granularity": granularity,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "event_count": len(rows),
        "topics": [{"topic": topic, "count": count} for topic, count in topics.most_common(5)],
        "counts": dict(type_counts),
        "evidence": [str(eid) for eid in event_ids],
        "executed_at": executed_at.isoformat(),
    }
    top_topics = [topic for topic, _count in topics.most_common(5)]
    type_bits = ", ".join(f"{key}={value}" for key, value in sorted(type_counts.items()))
    text = (
        f"Period summary ({granularity} {period_start.date()}): {len(rows)} events"
        + (f"; topics: {', '.join(top_topics)}" if top_topics else "")
        + (f"; types: {type_bits}" if type_bits else "")
        + "."
    )
    fp = fingerprint(
        {
            "memory_type": "summary",
            "kind": "period_summary",
            "granularity": granularity,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
        }
    )

    existing = await _find_current_period_summary(session, granularity, period_start)
    if existing is not None and (existing.payload or {}).get("event_count", 0) >= len(rows):
        await _link_events(session, existing, event_ids)
        return [str(existing.id)]

    memory = Memory(
        memory_type="summary",
        text=text,
        payload=payload,
        importance=0.5,
        confidence=0.8,
        source_type="derived",
        privacy_level="normal",
        event_time=period_end,
        valid_from=executed_at,
        version_group=existing.version_group if existing else uuid4(),
        version=(existing.version + 1) if existing else 1,
        supersedes_id=existing.id if existing else None,
        reason_for_change="Period recomputed" if existing else None,
        fingerprint=fp,
    )
    try:
        memory.embedding = (await get_embedder().embed([text]))[0]
    except Exception:
        memory.embedding = None
    if existing is not None:
        existing.is_current = False
        existing.superseded_by_id = memory.id
        existing.valid_until = executed_at
    session.add(memory)
    await session.flush()
    await _link_events(session, memory, event_ids)
    return [str(memory.id)]
