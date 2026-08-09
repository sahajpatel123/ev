"""User State Engine: lightweight, continuously derivable picture of the user's current world."""

from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import live
from app.models import DecisionOutcome, Entity, Event, LiveChannel, Memory
from app.schemas import UserStateOut
from app.utils.text import utcnow

STOPWORDS = {
    "the", "and", "that", "this", "with", "from", "have", "been", "was", "were",
    "will", "would", "should", "could", "can", "has", "had", "for", "but", "not",
    "are", "you", "your", "about", "into", "than", "then", "there", "they", "them",
}

FAILURE_TOKENS = re.compile(r"\b(failed|blocked|broke|broken|didn't work|not working|crashed|stuck)\b", re.IGNORECASE)
SUCCESS_TOKENS = re.compile(r"\b(worked|fixed|done|completed|shipped|succeeded|solved|shipped|landed)\b", re.IGNORECASE)
CONSTRAINT_TOKENS = re.compile(r"\b(can't|cannot|blocked|because|but i have to|unfortunately)\b", re.IGNORECASE)


async def build_user_state(
    session: AsyncSession,
    *,
    window_days: int = 7,
    access: str = "user",
) -> UserStateOut:
    """Derive the user's current state from recorded events.

    ``access="model"`` restricts the derived slice to data that may be sent to
    the model: never_send_to_model events, memories, and live channels/events
    are excluded so EVIE only touches the permitted live-data slice.
    """
    since = utcnow() - timedelta(days=window_days)
    stmt = (
        select(Event)
        .where(Event.tombstoned_at.is_(None), Event.occurred_at >= since)
        .order_by(Event.occurred_at.desc())
        .limit(300)
    )
    if access == "model":
        stmt = stmt.where(Event.privacy_level.notin_(("never_send_to_model", "sensitive")))
    result = await session.execute(stmt)
    events = list(result.scalars().all())

    # Active goal: most recent current goal memory.
    goal_stmt = (
        select(Memory)
        .where(
            Memory.memory_type == "goal",
            Memory.is_current.is_(True),
            Memory.redacted.is_(False),
        )
        .order_by(Memory.event_time.desc())
        .limit(5)
    )
    if access == "model":
        goal_stmt = goal_stmt.where(
            Memory.privacy_level.notin_(("never_send_to_model", "sensitive"))
        )
    goal_rows = (await session.execute(goal_stmt)).scalars().all()
    active_goal = None
    for goal in goal_rows:
        payload = goal.payload or {}
        if payload.get("status", "active") == "active":
            active_goal = goal.text
            break

    # Active project from @mentions or project entities in recent memories.
    active_project = None
    if events:
        mentions = re.findall(r"@([A-Za-z0-9_]+)", " ".join((e.content or {}).get("text", "") for e in events))
        if mentions:
            active_project = max(set(mentions), key=mentions.count)
    if active_project is None:
        entity_rows = (
            await session.execute(
                select(Entity)
                .where(Entity.entity_type == "project")
                .order_by(Entity.updated_at.desc())
                .limit(3)
            )
        ).scalars().all()
        if entity_rows:
            active_project = entity_rows[0].name

    current_task = None
    for event in events:
        if event.event_type in ("message.user", "note", "voice", "share"):
            text = (event.content or {}).get("text") or ""
            if text.strip():
                current_task = text[:200]
                break

    recent_text = " ".join((e.content or {}).get("text") or "" for e in events)
    tokens = [t for t in re.findall(r"[a-z0-9']+", recent_text.lower()) if t not in STOPWORDS and len(t) >= 4]
    recent_topics = [t for t, _ in Counter(tokens).most_common(10)]

    decision_stmt = (
        select(Memory).where(
            Memory.memory_type == "decision",
            Memory.is_current.is_(True),
            Memory.redacted.is_(False),
        )
    )
    if access == "model":
        decision_stmt = decision_stmt.where(
            Memory.privacy_level.notin_(("never_send_to_model", "sensitive"))
        )
    open_decision_memories = (await session.execute(decision_stmt)).scalars().all()
    reviewed_decision_ids = {
        row.decision_memory_id
        for row in (
            await session.execute(select(DecisionOutcome))
        ).scalars().all()
    }
    open_decisions = [
        {
            "id": str(m.id),
            "text": m.text,
            "event_time": m.event_time.isoformat(),
            "confidence": m.confidence,
        }
        for m in open_decision_memories
        if m.id not in reviewed_decision_ids
    ][:10]

    failures = []
    successes = []
    constraints = []
    for event in events:
        text = (event.content or {}).get("text") or ""
        if not text:
            continue
        if FAILURE_TOKENS.search(text):
            failures.append(text[:160])
        if SUCCESS_TOKENS.search(text):
            successes.append(text[:160])
        if CONSTRAINT_TOKENS.search(text):
            constraints.append(text[:160])

    updated_at = events[0].occurred_at if events else None
    live_rows = await live.query_live_events(session, access=access, since=since, limit=5)
    channel_map: dict[UUID, LiveChannel] = {}
    if live_rows:
        channel_ids = {row.channel_id for row in live_rows}
        channel_rows = await session.execute(
            select(LiveChannel).where(LiveChannel.id.in_(channel_ids))
        )
        channel_map = {channel.id: channel for channel in channel_rows.scalars().all()}
    live_context = [
        live.live_context_line(channel_map.get(row.channel_id), row, access=access)
        for row in live_rows
    ]
    return UserStateOut(
        activity=_infer_activity(recent_text),
        active_project=active_project,
        active_goal=active_goal,
        current_task=current_task,
        recent_topics=recent_topics,
        open_decisions=open_decisions,
        known_constraints=constraints[-5:],
        recent_failures=failures[-5:],
        recent_successes=successes[-5:],
        live_context=live_context,
        updated_at=updated_at,
    )


def _infer_activity(text: str) -> str:
    lowered = text.lower()
    if any(
        t in lowered
        for t in (
            "code",
            "bug",
            "deploy",
            "api",
            "python",
            "database",
            "sqlite",
            "postgres",
            "server",
            "docker",
            "retrieval",
            "algorithm",
            "migration",
            "script",
            "ranking",
        )
    ):
        return "coding"
    if any(t in lowered for t in ("research", "compare", "which model", "read", "article")):
        return "researching"
    if any(t in lowered for t in ("meeting", "interview", "call with", "sync with")):
        return "meeting"
    if any(t in lowered for t in ("plan", "roadmap", "milestone")):
        return "planning"
    if any(t in lowered for t in ("tired", "sleep", "rest", "workout", "gym")):
        return "personal"
    return "general"
