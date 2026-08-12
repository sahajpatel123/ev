"""Open-conflict surfacing for the context window."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conflict, Memory


async def open_conflict_lines(
    session: AsyncSession,
    *,
    limit: int = 10,
) -> list[str]:
    """Render open conflicts as context lines (ask, don't silently pick)."""
    rows = (
        await session.execute(
            select(Conflict)
            .where(Conflict.status == "open")
            .order_by(Conflict.created_time.desc())
            .limit(limit)
        )
    ).scalars().all()
    if not rows:
        return []
    ids = []
    for row in rows:
        ids.extend([row.memory_id_a, row.memory_id_b])
    memories = {
        str(m.id): m
        for m in (
            await session.execute(
                select(Memory).where(Memory.id.in_(set(ids)))
            )
        ).scalars().all()
    }
    lines: list[str] = []
    for row in rows:
        a = memories.get(str(row.memory_id_a))
        b = memories.get(str(row.memory_id_b))
        if a is None or b is None:
            continue
        lines.append(
            f"- {a.memory_type}: '{a.text[:140]}' vs "
            f"{b.memory_type}: '{b.text[:140]}' — {row.reason}"
        )
    return lines
