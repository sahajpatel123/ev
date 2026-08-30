"""Hybrid candidate fetch for Memory OS. Wraps recall; does not replace it.

Postgres uses FTS + trigram when available. SQLite/tests fall back to ILIKE
and the existing explicit-recall scan. Never walks the materialized folder.
"""

from __future__ import annotations

import time
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.os_health import note_index
from app.models import Entity, Event, Memory

_USER_TYPES = ("message.user", "message.assistant")


def _like(query: str) -> str:
    token = " ".join((query or "").split())[:80]
    return f"%{token}%" if token else "%"


async def search_event_ids(
    session: AsyncSession,
    query: str,
    *,
    k: int = 40,
    since=None,
    until=None,
) -> list[UUID]:
    started = time.perf_counter()
    dialect = "sqlite"
    bind = session.get_bind()
    if bind is not None:
        dialect = bind.dialect.name
    ids: list[UUID] = []
    if dialect.startswith("postgres") and (query or "").strip():
        try:
            stmt = text(
                """
                SELECT id FROM events
                WHERE tombstoned_at IS NULL
                  AND event_type IN ('message.user', 'message.assistant')
                  AND (
                    to_tsvector('simple', coalesce(content->>'text', ''))
                    @@ plainto_tsquery('simple', :q)
                    OR content->>'text' ILIKE :like
                  )
                ORDER BY occurred_at DESC
                LIMIT :k
                """
            )
            rows = (
                await session.execute(stmt, {"q": query[:200], "like": _like(query), "k": k})
            ).fetchall()
            ids = [row[0] for row in rows]
            note_index(fulltext_ready=True, fts_ms=(time.perf_counter() - started) * 1000)
        except Exception:  # noqa: BLE001 - lexical fallback must still work
            note_index(fulltext_ready=False)
            ids = []
    if not ids:
        needle = (query or "").strip().lower()
        stmt = (
            select(Event.id, Event.content)
            .where(
                Event.tombstoned_at.is_(None),
                Event.event_type.in_(_USER_TYPES),
            )
            .order_by(Event.occurred_at.desc())
            .limit(800)
        )
        if since is not None:
            stmt = stmt.where(Event.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(Event.occurred_at <= until)
        rows = (await session.execute(stmt)).all()
        for event_id, content in rows:
            blob = str((content or {}).get("text") or "").lower()
            if needle and needle not in blob and not any(
                token in blob for token in needle.split() if len(token) > 3
            ):
                continue
            ids.append(event_id)
            if len(ids) >= k:
                break
        note_index(fulltext_ready=False, fts_ms=(time.perf_counter() - started) * 1000)
    return ids[:k]


async def lookup_entities(session: AsyncSession, query: str, *, k: int = 6) -> list[Entity]:
    tokens = [part for part in (query or "").split() if len(part) > 2][:8]
    if not tokens:
        return []
    rows = (
        await session.execute(select(Entity).order_by(Entity.updated_at.desc()).limit(400))
    ).scalars().all()
    hits: list[Entity] = []
    lowered = (query or "").lower()
    for entity in rows:
        blob = f"{entity.name} {' '.join(entity.aliases or [])}".lower()
        if entity.name.lower() in lowered or any(token.lower() in blob for token in tokens):
            hits.append(entity)
        if len(hits) >= k:
            break
    return hits


async def current_memories(
    session: AsyncSession, *, memory_type: str | None = None, k: int = 8
) -> list[Memory]:
    stmt = select(Memory).where(Memory.is_current.is_(True), Memory.redacted.is_(False))
    if memory_type:
        stmt = stmt.where(Memory.memory_type == memory_type)
    stmt = stmt.order_by(Memory.importance.desc(), Memory.event_time.desc()).limit(k)
    return list((await session.execute(stmt)).scalars().all())
