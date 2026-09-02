"""Device inbox: routed results, conversation movement, offline explanations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeviceInboxItem
from app.utils.text import utcnow


def _when(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def push_inbox(
    session: AsyncSession,
    *,
    device_id: UUID | str,
    kind: str,
    title: str,
    body: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = DeviceInboxItem(
        device_id=UUID(str(device_id)),
        kind=(kind or "notice")[:64],
        title=(title or "")[:160],
        body=(body or "")[:2000],
        payload=payload or {},
    )
    session.add(row)
    await session.flush()
    return public_item(row)


async def list_inbox(
    session: AsyncSession,
    *,
    device_id: UUID | str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(DeviceInboxItem)
                .where(DeviceInboxItem.device_id == UUID(str(device_id)))
                .order_by(DeviceInboxItem.created_at.desc())
                .limit(max(1, min(limit, 100)))
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    out: list[dict[str, Any]] = []
    for row in rows:
        if _when(row.expires_at) is not None and _when(row.expires_at) <= now:
            continue
        out.append(public_item(row))
    return out


async def ack_inbox(session: AsyncSession, *, device_id: UUID | str, item_id: UUID | str) -> dict[str, Any] | None:
    row = await session.get(DeviceInboxItem, UUID(str(item_id)))
    if row is None or str(row.device_id) != str(device_id):
        return None
    row.read_at = utcnow()
    return public_item(row)


def public_item(row: DeviceInboxItem) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "payload": row.payload or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "read_at": row.read_at.isoformat() if row.read_at else None,
        "unread": row.read_at is None,
    }
