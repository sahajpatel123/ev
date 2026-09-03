"""Server-side offline queue replay. Queued is never executed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, OfflineQueueItem
from app.utils.text import utcnow

TERMINAL = frozenset({"executed", "failed", "expired", "rejected"})


def _when(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _error(status: int, code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "status": status, "error_code": code, "message": message, **extra}


async def enqueue(
    session: AsyncSession,
    *,
    device: Device,
    idempotency_key: str,
    kind: str,
    payload: dict[str, Any],
    ttl_seconds: int = 86400,
) -> dict[str, Any]:
    key = (idempotency_key or "").strip()[:128]
    if len(key) < 8:
        return _error(422, "INVALID_IDEMPOTENCY_KEY", "Offline items need a stable idempotency key")
    existing = (
        await session.execute(
            select(OfflineQueueItem).where(
                OfflineQueueItem.device_id == device.id,
                OfflineQueueItem.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {
            "ok": True,
            "status": 409,
            "replayed": True,
            "item": public_item(existing),
            "executed": existing.state == "executed",
        }
    row = OfflineQueueItem(
        device_id=device.id,
        idempotency_key=key,
        kind=(kind or "request")[:64],
        payload=payload or {},
        state="pending",
        expires_at=utcnow() + timedelta(seconds=max(60, ttl_seconds)),
    )
    session.add(row)
    await session.flush()
    return {"ok": True, "status": 201, "item": public_item(row), "executed": False}


async def replay(
    session: AsyncSession,
    *,
    device: Device,
    idempotency_key: str,
) -> dict[str, Any]:
    key = (idempotency_key or "").strip()[:128]
    row = (
        await session.execute(
            select(OfflineQueueItem).where(
                OfflineQueueItem.device_id == device.id,
                OfflineQueueItem.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return _error(404, "NOT_FOUND", "Unknown queued item")
    now = utcnow()
    expires = _when(row.expires_at)
    if expires is not None and expires <= now and row.state == "pending":
        row.state = "expired"
        row.error_code = "EXPIRED"
        return {
            "ok": False,
            "status": 422,
            "error_code": "EXPIRED",
            "message": "Queued item expired before replay",
            "item": public_item(row),
            "executed": False,
        }
    if row.state in TERMINAL:
        return {
            "ok": True,
            "status": 409,
            "replayed": True,
            "item": public_item(row),
            "executed": row.state == "executed",
        }
    # Replay accepts the item for later canonical handling. It does not claim
    # that a side effect already happened.
    row.state = "accepted"
    row.replayed_at = now
    return {"ok": True, "status": 200, "item": public_item(row), "executed": False}


async def list_pending(session: AsyncSession, *, device_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(OfflineQueueItem)
                .where(OfflineQueueItem.device_id == device_id)
                .order_by(OfflineQueueItem.created_at.desc())
                .limit(max(1, min(limit, 100)))
            )
        )
        .scalars()
        .all()
    )
    return [public_item(r) for r in rows]


def public_item(row: OfflineQueueItem) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "idempotency_key": row.idempotency_key,
        "kind": row.kind,
        "state": row.state,
        "payload": row.payload or {},
        "error_code": row.error_code,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "executed": row.state == "executed",
    }
