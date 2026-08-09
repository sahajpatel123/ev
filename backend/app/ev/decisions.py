"""Decision intelligence: decision loops, expected-vs-actual outcomes, lessons."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import RetrievedMemory
from app.embeddings import get_embedder
from app.models import DecisionOutcome, Memory, MemoryEvent
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.utils.text import fingerprint, normalize_text, utcnow


def _topic_of(memory: Memory) -> str:
    payload = memory.payload or {}
    return str(payload.get("topic") or payload.get("decision") or memory.text)[:200]


async def find_decision_loops(
    session: AsyncSession,
    *,
    window_days: int = 30,
    min_count: int = 2,
) -> list[dict]:
    """Group current decision memories by topic; return loops above min_count."""
    since = utcnow() - timedelta(days=window_days)
    result = await session.execute(
        select(Memory).where(
            Memory.memory_type == "decision",
            Memory.redacted.is_(False),
            Memory.event_time >= since,
        )
    )
    memories = list(result.scalars().all())
    groups: dict[str, list[Memory]] = {}
    for memory in memories:
        key = normalize_text(_topic_of(memory))[:80]
        if not key:
            continue
        groups.setdefault(key, []).append(memory)

    loops: list[dict] = []
    for topic, rows in groups.items():
        if len(rows) < min_count:
            continue
        loops.append(
            {
                "topic": topic,
                "count": len(rows),
                "confidence": round(min(0.95, 0.4 + 0.15 * len(rows)), 3),
                "memory_ids": [str(m.id) for m in rows],
                "latest_at": max(m.event_time for m in rows),
            }
        )
    loops.sort(key=lambda d: d["count"], reverse=True)
    return loops


async def record_outcome(
    session: AsyncSession,
    decision_memory_id: UUID,
    *,
    expected_outcome: str | None,
    actual_outcome: str,
    lesson: str | None,
    actor: str = "api",
) -> DecisionOutcome:
    memory = await session.get(Memory, decision_memory_id)
    if memory is None:
        raise KeyError(f"Decision memory {decision_memory_id} not found")
    if memory.memory_type != "decision":
        raise ValueError(f"Memory {decision_memory_id} is not a decision")

    topic = _topic_of(memory)
    if not lesson and expected_outcome and normalize_text(expected_outcome) != normalize_text(actual_outcome):
        lesson = (
            f"Expected: {expected_outcome} — actual: {actual_outcome}. "
            "Re-evaluate before repeating this choice."
        )

    # Raw event so the lesson derivation is replayable from the event log.
    event = await EventService(session, actor=actor).create(
        EventCreate(
            source="memory",
            event_type="decision.outcome",
            text=lesson or f"Decision outcome reviewed: {actual_outcome}",
            metadata={
                "decision_memory_id": str(memory.id),
                "decision_topic": topic,
                "expected_outcome": expected_outcome,
                "actual_outcome": actual_outcome,
                "lesson": lesson,
            },
        )
    )

    outcome = DecisionOutcome(
        decision_memory_id=memory.id,
        decision_topic=topic[:256],
        expected_outcome=expected_outcome,
        actual_outcome=actual_outcome,
        reviewed_at=utcnow(),
        lesson=lesson,
        status="reviewed",
    )
    session.add(outcome)
    await session.flush()

    if lesson:
        outcome.lesson_memory_id = await _write_lesson(session, memory, lesson, event)
    return outcome


async def _build_lesson_memory(
    session: AsyncSession,
    decision: Memory | None,
    lesson: str,
    event,
) -> Memory:
    text = f"Lesson: {lesson}"
    memory = Memory(
        memory_type="lesson",
        text=text,
        payload={
            "kind": "decision_lesson",
            "lesson": lesson,
            "decision_topic": _topic_of(decision) if decision else "",
        },
        importance=0.7,
        confidence=0.75,
        source_type="derived",
        privacy_level="normal",
        event_time=event.occurred_at,
        valid_from=event.occurred_at,
        version_group=uuid4(),
        version=1,
        fingerprint=fingerprint({"memory_type": "lesson", "lesson": normalize_text(lesson)}),
    )
    try:
        memory.embedding = (await get_embedder().embed([text]))[0]
    except Exception:
        memory.embedding = None
    session.add(memory)
    await session.flush()

    if decision is not None:
        # Provenance: the lesson traces to the same raw events as the decision.
        source_rows = (
            await session.execute(select(MemoryEvent).where(MemoryEvent.memory_id == decision.id))
        ).scalars().all()
        for row in source_rows:
            session.add(MemoryEvent(memory_id=memory.id, event_id=row.event_id))
    session.add(MemoryEvent(memory_id=memory.id, event_id=event.id))
    return memory


async def _write_lesson(
    session: AsyncSession,
    decision: Memory,
    lesson: str,
    event,
) -> UUID:
    memory = await _build_lesson_memory(session, decision, lesson, event)
    return memory.id


async def recreate_lesson_from_event(session: AsyncSession, event) -> Memory | None:
    """Replay a decision.outcome event into its derived lesson memory."""
    meta = event.metadata_ or {}
    lesson = meta.get("lesson")
    if not lesson:
        return None

    topic = meta.get("decision_topic") or ""
    decision: Memory | None = None
    if topic:
        rows = (
            await session.execute(
                select(Memory).where(
                    Memory.memory_type == "decision",
                    Memory.is_current.is_(True),
                    Memory.redacted.is_(False),
                )
            )
        ).scalars().all()
        for row in rows:
            if normalize_text(_topic_of(row))[:80] == normalize_text(topic)[:80]:
                decision = row
                break

    return await _build_lesson_memory(session, decision, lesson, event)


async def followups_due(session: AsyncSession, *, after_days: int = 7) -> list[DecisionOutcome]:
    cutoff = utcnow() - timedelta(days=after_days)
    result = await session.execute(
        select(DecisionOutcome).where(DecisionOutcome.status == "pending", DecisionOutcome.created_at <= cutoff)
    )
    return list(result.scalars().all())


def outcome_stats(decisions: list[RetrievedMemory], outcomes: list[DecisionOutcome]) -> dict:
    """Summarize outcome evidence for tactical briefs."""
    by_topic: Counter[str] = Counter()
    lessons: list[str] = []
    for outcome in outcomes:
        by_topic[normalize_text(outcome.decision_topic)] += 1
        if outcome.lesson:
            lessons.append(outcome.lesson)
    return {"count": len(outcomes), "topics": dict(by_topic), "lessons": lessons}
