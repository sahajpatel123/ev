"""Layer 3: compact episode summaries derived from the event timeline.

Summaries are rebuildable. Events remain the source of truth.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.observe import log_memory
from app.models import Event, Memory, MemoryEvent
from app.utils.text import fingerprint, normalize_text, utcnow

EPISODE_GAP = timedelta(hours=3)
MAX_EPISODE_LINES = 8
MAX_EVENTS = 80


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _event_text(event: Event) -> str:
    return str((event.content or {}).get("text") or "").strip()


async def maybe_update_episode(
    session: AsyncSession,
    conversation_id: UUID | None,
    *,
    seed_event: Event | None = None,
) -> Memory | None:
    if conversation_id is None:
        return None
    stmt = (
        select(Event)
        .where(
            Event.conversation_id == conversation_id,
            Event.tombstoned_at.is_(None),
            Event.event_type.in_(("message.user", "message.assistant")),
        )
        .order_by(Event.occurred_at.desc())
        .limit(MAX_EVENTS)
    )
    rows = list(reversed((await session.execute(stmt)).scalars().all()))
    if not rows:
        return None
    cluster: list[Event] = [rows[-1]]
    for event in reversed(rows[:-1]):
        newest = cluster[0]
        newest_at = _as_utc(newest.occurred_at)
        event_at = _as_utc(event.occurred_at)
        if newest_at is None or event_at is None:
            break
        if newest_at - event_at > EPISODE_GAP:
            break
        cluster.insert(0, event)
    user_lines = [
        _event_text(event)
        for event in cluster
        if event.event_type == "message.user" and _event_text(event)
    ]
    if not user_lines:
        return None
    window_start = cluster[0].occurred_at.isoformat()
    window_end = cluster[-1].occurred_at.isoformat()
    bullets = []
    seen: set[str] = set()
    for line in user_lines:
        key = normalize_text(line)[:80]
        if key in seen:
            continue
        seen.add(key)
        bullets.append(line[:180])
        if len(bullets) >= MAX_EPISODE_LINES:
            break
    summary = "Episode: " + "; ".join(bullets)
    payload = {
        "kind": "episode",
        "thread_id": str(conversation_id),
        "window_start": window_start,
        "window_end": window_end,
        "event_ids": [str(event.id) for event in cluster],
        "turn_count": len(cluster),
        "not_verbatim": True,
    }
    existing = await _open_episode(session, conversation_id, window_start)
    if existing is None:
        memory = Memory(
            memory_type="summary",
            text=summary[:2000],
            payload=payload,
            importance=0.55,
            confidence=0.7,
            source_type="derived",
            event_time=cluster[-1].occurred_at,
            version_group=uuid4(),
            fingerprint=fingerprint({"kind": "episode", "thread": str(conversation_id), "start": window_start}),
        )
        session.add(memory)
        await session.flush()
        if seed_event is not None:
            session.add(MemoryEvent(memory_id=memory.id, event_id=seed_event.id))
        log_memory(
            "memory.episode_updated",
            extra={"memory_id": str(memory.id), "turns": len(cluster), "created": True},
        )
        return memory
    existing.text = summary[:2000]
    existing.payload = payload
    existing.event_time = cluster[-1].occurred_at
    existing.updated_time = utcnow()
    if seed_event is not None:
        linked = await session.execute(
            select(MemoryEvent).where(
                MemoryEvent.memory_id == existing.id,
                MemoryEvent.event_id == seed_event.id,
            )
        )
        if linked.scalar_one_or_none() is None:
            session.add(MemoryEvent(memory_id=existing.id, event_id=seed_event.id))
    log_memory(
        "memory.episode_updated",
        extra={"memory_id": str(existing.id), "turns": len(cluster), "created": False},
    )
    return existing


async def recent_episodes(
    session: AsyncSession, *, k: int = 6, conversation_id: UUID | None = None
) -> list[Memory]:
    stmt = (
        select(Memory)
        .where(
            Memory.memory_type == "summary",
            Memory.is_current.is_(True),
            Memory.redacted.is_(False),
        )
        .order_by(Memory.event_time.desc())
        .limit(40)
    )
    rows = (await session.execute(stmt)).scalars().all()
    episodes = [row for row in rows if (row.payload or {}).get("kind") == "episode"]
    if conversation_id is not None:
        key = str(conversation_id)
        episodes = [row for row in episodes if (row.payload or {}).get("thread_id") == key]
    return episodes[:k]


async def _open_episode(
    session: AsyncSession, conversation_id: UUID, window_start: str
) -> Memory | None:
    rows = (
        await session.execute(
            select(Memory)
            .where(
                Memory.memory_type == "summary",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
            .order_by(Memory.updated_time.desc())
            .limit(20)
        )
    ).scalars().all()
    thread = str(conversation_id)
    for row in rows:
        payload = row.payload or {}
        if payload.get("kind") != "episode":
            continue
        if payload.get("thread_id") != thread:
            continue
        if payload.get("window_start") == window_start:
            return row
    return None
