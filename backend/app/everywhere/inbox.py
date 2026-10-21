"""Device inbox: routed results, conversation movement, offline explanations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceInboxItem
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
    # Cycle 49 — Web Push (VAPID): best-effort, non-blocking; a push
    # failure never fails the inbox write. No-op when unconfigured or
    # when the device has no stored browser subscription.
    try:
        device = (
            await session.execute(select(Device).where(Device.id == UUID(str(device_id))))
        ).scalar_one_or_none()
        if device is not None:
            from app.everywhere.web_push import send_web_push

            asyncio.create_task(
                send_web_push(device, title=title, body=body, url="/evie/"),
                name="ev-web-push",
            )
            # Silent APNs wake (content-available): best-effort, non-blocking.
            # No-op when unconfigured or when the device has no APNs token.
            if getattr(device, "push_token", None):
                import contextlib

                from app.notify.backends.apns import APNsNotifier

                async def _silent_wake(
                    _token: str = str(device.push_token),
                    _item_id: str = str(row.id),
                    _kind: str = str(row.kind),
                ) -> None:
                    with contextlib.suppress(Exception):  # wake is best-effort by law
                        await APNsNotifier().send_silent(
                            device_token=_token, item_id=_item_id, kind=_kind
                        )

                asyncio.create_task(_silent_wake(), name="ev-silent-wake")
    except Exception:  # noqa: BLE001 - push is best-effort by law
        pass
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
        _exp = _when(row.expires_at)
        if _exp is not None and _exp <= now:
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
