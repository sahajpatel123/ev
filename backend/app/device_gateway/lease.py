"""Conversation lease: one response device at a time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ConversationLease
from app.utils.text import utcnow

from . import OWNER_KEY


def _when(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def current_lease(session: AsyncSession) -> ConversationLease | None:
    row = (
        await session.execute(select(ConversationLease).where(ConversationLease.owner_key == OWNER_KEY))
    ).scalar_one_or_none()
    if row is None or (_when(row.expires_at) or utcnow()) <= utcnow():
        return None
    return row


async def claim_lease(
    session: AsyncSession,
    *,
    device_id: UUID,
    instance_id: str,
    method: str = "manual",
    session_id: str | None = None,
    client_generation: int = 0,
) -> ConversationLease:
    now = utcnow()
    ttl = timedelta(seconds=max(30, int(settings.conversation_lease_ttl_seconds)))
    row = (
        await session.execute(select(ConversationLease).where(ConversationLease.owner_key == OWNER_KEY))
    ).scalar_one_or_none()
    if row is None:
        row = ConversationLease(
            owner_key=OWNER_KEY,
            lease_id=uuid4().hex,
            device_id=device_id,
            instance_id=instance_id[:64],
            method=method[:32],
            acquired_at=now,
            last_activity=now,
            expires_at=now + ttl,
            session_id=(session_id or "")[:64] or None,
            client_generation=int(client_generation or 0),
        )
        session.add(row)
        await session.flush()
        return row
    row.lease_id = uuid4().hex
    row.device_id = device_id
    row.instance_id = instance_id[:64]
    row.method = method[:32]
    row.acquired_at = now
    row.last_activity = now
    row.expires_at = now + ttl
    row.session_id = (session_id or "")[:64] or None
    row.client_generation = int(client_generation or 0)
    return row


async def heartbeat_lease(
    session: AsyncSession,
    *,
    device_id: UUID,
    instance_id: str,
) -> ConversationLease | None:
    row = await current_lease(session)
    if row is None:
        return None
    if row.device_id != device_id or row.instance_id != instance_id:
        return row
    ttl = timedelta(seconds=max(30, int(settings.conversation_lease_ttl_seconds)))
    row.last_activity = utcnow()
    row.expires_at = utcnow() + ttl
    return row


def lease_belongs(row: ConversationLease | None, *, device_id: UUID, instance_id: str) -> bool:
    if row is None:
        return False
    return row.device_id == device_id and row.instance_id == instance_id


async def release_lease(
    session: AsyncSession,
    *,
    device_id: UUID,
    instance_id: str,
) -> None:
    row = await current_lease(session)
    if row is None:
        return
    if row.device_id == device_id and row.instance_id == instance_id:
        row.expires_at = utcnow()


def lease_public(row: ConversationLease | None) -> dict | None:
    if row is None:
        return None
    return {
        "lease_id": row.lease_id,
        "device_id": str(row.device_id),
        "instance_id": row.instance_id,
        "method": row.method,
        "session_id": row.session_id,
        "client_generation": int(getattr(row, "client_generation", 0) or 0),
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }
