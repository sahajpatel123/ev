"""DecisionMemory — G1 adapter over the EXISTING decision authority.

G0 audit law: there is NO second decision store. DecisionOutcome
(app/ev/decisions.py, backed by memories of type "decision" + outcome rows)
is already canonical for decision follow-up. This adapter composes it with
canonical events and relevant memories into one read model so future
decision intelligence never needs a migration.

G1 scope: read-only composition. No new tables, no inference.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DecisionOutcome, Event, Memory
from app.utils.text import utcnow

SOURCE = "life"


async def pending_decisions(session: AsyncSession) -> list[dict]:
    """Open expected-vs-actual loops awaiting review."""
    rows = (
        await session.execute(
            select(DecisionOutcome)
            .where(DecisionOutcome.status == "pending")
            .order_by(DecisionOutcome.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    return [
        {
            "id": str(r.id),
            "topic": r.decision_topic,
            "expected": r.expected_outcome,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


async def decision_context(
    session: AsyncSession, *, topic_query: str | None = None, days: int = 30
) -> dict:
    """One composed view: outcomes + recent decision-shaped events + recall.

    Authority note (audit result): current-state authority = DecisionOutcome;
    history = `events`; recall = Memory OS. This function only joins them.
    """
    since = utcnow() - timedelta(days=days)
    outcomes = await pending_decisions(session)
    ev_rows = (
        await session.execute(
            select(Event)
            .where(
                Event.source == SOURCE,
                Event.occurred_at >= since,
                Event.event_type.in_(("goal.completed", "goal.cancelled", "project.completed")),
            )
            .order_by(Event.occurred_at.desc())
            .limit(20)
        )
    ).scalars().all()
    memory_rows = (
        await session.execute(
            select(Memory)
            .where(
                Memory.memory_type == "decision",
                Memory.redacted.is_(False),
                Memory.event_time >= since,
            )
            .order_by(Memory.event_time.desc())
            .limit(20)
        )
    ).scalars().all()
    memories = [
        {
            "text": m.text,
            "at": m.event_time.isoformat() if m.event_time else None,
        }
        for m in memory_rows
        if not topic_query or topic_query.lower() in (m.text or "").lower()
    ]
    return {
        "pending_outcomes": outcomes,
        "recent_decision_shaped_events": [
            {"type": e.event_type, "at": e.occurred_at.isoformat()} for e in ev_rows
        ],
        "decision_memories": memories[:10],
    }
