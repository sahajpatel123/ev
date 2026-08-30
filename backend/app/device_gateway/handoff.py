"""Short-lived ActiveConversationState for handoff. Not Memory OS."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ActiveConversationState
from app.utils.text import utcnow

from . import OWNER_KEY


def _when(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def record_turn(
    session: AsyncSession,
    *,
    device_id: UUID,
    role: str,
    text: str,
    topic: str | None = None,
) -> ActiveConversationState:
    now = utcnow()
    ttl = timedelta(seconds=max(60, int(settings.active_conversation_ttl_seconds)))
    row = (
        await session.execute(
            select(ActiveConversationState).where(ActiveConversationState.owner_key == OWNER_KEY)
        )
    ).scalar_one_or_none()
    entry = {"role": role, "text": (text or "")[:400], "device_id": str(device_id)}
    if row is None:
        row = ActiveConversationState(
            owner_key=OWNER_KEY,
            active_device_id=device_id,
            topic=(topic or "")[:240] or None,
            turns=[entry],
            updated_at=now,
            expires_at=now + ttl,
        )
        session.add(row)
        await session.flush()
        return row
    turns = list(row.turns or [])
    turns.append(entry)
    row.turns = turns[-8:]
    row.active_device_id = device_id
    if topic:
        row.topic = topic[:240]
    row.updated_at = now
    row.expires_at = now + ttl
    return row


async def current_state(session: AsyncSession) -> ActiveConversationState | None:
    row = (
        await session.execute(
            select(ActiveConversationState).where(ActiveConversationState.owner_key == OWNER_KEY)
        )
    ).scalar_one_or_none()
    if row is None or (_when(row.expires_at) or utcnow()) <= utcnow():
        return None
    return row


def state_public(row: ActiveConversationState | None) -> dict | None:
    if row is None:
        return None
    return {
        "active_device_id": str(row.active_device_id) if row.active_device_id else None,
        "topic": row.topic,
        "turns": row.turns or [],
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }
