"""Temporal as-of reconstruction and project-state views. Uses Event time."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.continuity import conversation_time_requested, wants_historical_truth
from app.memory.loops import list_loops, loop_public, rank_open_loops
from app.memory.observe import log_memory
from app.memory.temporal import resolve_temporal_expressions
from app.models import Memory
from app.utils.text import utcnow

_LEAVE_OFF = re.compile(
    r"\b(where did we leave off|where were we|what were we (?:still )?working on|"
    r"what(?:'s| is) the current (?:state|status)|what should we (?:work on|do) next)\b",
    re.IGNORECASE,
)
_STILL_OPEN = re.compile(
    r"\b(what(?:'s| is) still (?:open|unresolved|stuck|broken|left)|"
    r"what (?:are we|were we) still (?:stuck on|working on)|"
    r"what haven'?t we finished|what was left|"
    r"what(?:'s| is) (?:still )?unresolved)\b",
    re.IGNORECASE,
)
_SOLVED = re.compile(
    r"\b(what did we (?:solve|fix|finish)|what (?:got|have we) (?:fixed|solved|resolved)|"
    r"what (?:issue|problem) did we (?:solve|fix)|what used to be broken|"
    r"what problems did we solve)\b",
    re.IGNORECASE,
)
_CHANGED = re.compile(
    r"\b(what changed|how did .{0,40} change|what became different)\b",
    re.IGNORECASE,
)
_ORIGINAL = re.compile(
    r"\b(originally|at first|when we first|what did we (?:originally |first )?think|"
    r"what used to|back then|at the time)\b",
    re.IGNORECASE,
)


@dataclass
class TemporalQuery:
    mode: str
    as_of: datetime | None = None
    since: datetime | None = None
    until: datetime | None = None


def classify_temporal_query(query: str, *, now: datetime | None = None) -> TemporalQuery:
    text = (query or "").strip()
    now = now or utcnow()
    if _LEAVE_OFF.search(text):
        return TemporalQuery(mode="leave_off")
    if _STILL_OPEN.search(text):
        return TemporalQuery(mode="still_open")
    if _SOLVED.search(text):
        return TemporalQuery(mode="solved")
    if _CHANGED.search(text):
        since, until = _window(text, now)
        return TemporalQuery(mode="changes", since=since, until=until or now)
    if _ORIGINAL.search(text) or wants_historical_truth(text):
        since, until = _window(text, now)
        return TemporalQuery(mode="historical", since=since, until=until)
    if conversation_time_requested(text):
        since, until = _window(text, now)
        as_of = until or since
        return TemporalQuery(mode="as_of", as_of=as_of, since=since, until=until)
    return TemporalQuery(mode="current")


def _window(text: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    resolved = resolve_temporal_expressions(text, now)
    if not resolved:
        if "yesterday" in text.lower():
            start = now - timedelta(days=1)
            return start.replace(hour=0, minute=0, second=0, microsecond=0), now
        return None, None
    starts = [item.start for item in resolved if item.start is not None]
    ends = [item.end for item in resolved if item.end is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def valid_as_of(row: Memory, boundary: datetime) -> bool:
    start = _as_utc(row.valid_from) or _as_utc(row.event_time)
    end = _as_utc(row.valid_until)
    moment = _as_utc(boundary)
    if moment is None:
        return bool(row.is_current)
    if start is not None and start > moment:
        return False
    return not (end is not None and end <= moment)


async def memories_as_of(
    session: AsyncSession,
    *,
    boundary: datetime,
    memory_types: list[str] | None = None,
    k: int = 24,
) -> list[Memory]:
    stmt = select(Memory).where(Memory.redacted.is_(False)).order_by(Memory.event_time.desc()).limit(400)
    if memory_types:
        stmt = stmt.where(Memory.memory_type.in_(memory_types))
    rows = list((await session.execute(stmt)).scalars().all())
    kept = [row for row in rows if valid_as_of(row, boundary)]
    log_memory("memory.state_reconstructed", extra={"count": len(kept), "types": len(memory_types or [])})
    return kept[:k]


async def current_typed(session: AsyncSession, memory_type: str, *, k: int = 8) -> list[Memory]:
    rows = (
        await session.execute(
            select(Memory)
            .where(
                Memory.memory_type == memory_type,
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
            .order_by(Memory.importance.desc(), Memory.event_time.desc())
            .limit(k)
        )
    ).scalars().all()
    return list(rows)


async def get_project_state(session: AsyncSession, scope: str | None = None) -> dict[str, Any]:
    from app.ev.user_state import build_user_state

    state = await build_user_state(session, access="model")
    active = scope or state.active_project or "Evie"
    opens = await list_loops(
        session,
        status="open,blocked,waiting",
        scope=scope,
        k=12,
    )
    ranked = rank_open_loops(opens, active_project=active, k=5)
    resolved = await list_loops(session, status="resolved", scope=scope, k=4)
    decisions = await current_typed(session, "decision", k=5)
    rejections = await current_typed(session, "rejection", k=4)
    hypotheses = await current_typed(session, "hypothesis", k=4)
    return {
        "scope": active,
        "active_project": state.active_project,
        "current_task": state.current_task,
        "open_loops": [loop_public(row) for row in ranked],
        "recently_resolved": [loop_public(row) for row in resolved[:3]],
        "decisions": [_typed_public(row) for row in decisions],
        "rejected_options": [_typed_public(row) for row in rejections],
        "hypotheses": [_typed_public(row) for row in hypotheses],
    }


def _typed_public(row: Memory) -> dict[str, Any]:
    payload = row.payload or {}
    return {
        "id": str(row.id),
        "text": row.text,
        "status": payload.get("status"),
        "evidence_type": payload.get("evidence_type") or row.source_type,
        "when": row.event_time.isoformat() if row.event_time else None,
        "is_current": row.is_current,
        "source_event_ids": payload.get("source_event_ids") or [],
    }


async def get_changes(
    session: AsyncSession,
    *,
    since: datetime | None,
    until: datetime | None = None,
    k: int = 12,
) -> dict[str, Any]:
    until = until or utcnow()
    stmt = (
        select(Memory)
        .where(Memory.redacted.is_(False), Memory.event_time <= until)
        .order_by(Memory.event_time.desc())
        .limit(200)
    )
    if since is not None:
        stmt = stmt.where(Memory.event_time >= since)
    rows = list((await session.execute(stmt)).scalars().all())
    added = [row for row in rows if row.is_current and row.supersedes_id is None]
    changed = [row for row in rows if row.supersedes_id is not None]
    resolved = [
        row
        for row in rows
        if row.memory_type == "open_loop" and (row.payload or {}).get("status") == "resolved"
    ]
    opened = [
        row
        for row in rows
        if row.memory_type == "open_loop" and (row.payload or {}).get("status") in {"open", "blocked", "waiting"}
    ]
    log_memory("memory.temporal_query", extra={"mode": "changes", "count": len(rows)})
    return {
        "mode": "changes",
        "added": [_typed_public(row) for row in added[:k]],
        "changed": [_typed_public(row) for row in changed[:k]],
        "resolved": [loop_public(row) for row in resolved[:k]],
        "opened": [loop_public(row) for row in opened[:k]],
        "superseded": [_typed_public(row) for row in rows if not row.is_current][:k],
    }


async def leave_off_packet(session: AsyncSession) -> dict[str, Any]:
    state = await get_project_state(session)
    from app.memory.episodes import recent_episodes

    episodes = await recent_episodes(session, k=1)
    log_memory("memory.temporal_query", extra={"mode": "leave_off"})
    return {
        "mode": "leave_off",
        "active_project": state.get("active_project"),
        "open_loops": state.get("open_loops") or [],
        "decisions": state.get("decisions") or [],
        "last_episode": (episodes[0].text[:240] if episodes else None),
        "current_state": state,
    }
