"""G2 Phase 2/16 — Device roster, presence states, health summary.

Reuses the canonical Device registry and the gateway's in-process presence
TTL. App-not-foreground is NOT offline: presence evidence (hello/heartbeat/
any authenticated call refreshing last_seen_at) decides state.

States:
- ONLINE        live presence entry within TTL
- RECENTLY_SEEN no live presence, but authenticated contact recently
- DEGRADED      stale presence entry (was online, went quiet)
- OFFLINE       neither
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device
from app.utils.text import utcnow

RECENT_WINDOW = timedelta(minutes=15)


def _presence_map() -> tuple[set[str], set[str]]:
    from app.device_gateway.presence import snapshot as presence_snapshot

    snap = presence_snapshot()
    online = {str(row.get("device_id")) for row in snap.get("online", [])}
    stale = {str(row.get("device_id")) for row in snap.get("stale", [])}
    return online, stale


def presence_state(device: Device) -> str:
    if device.revoked_at is not None:
        return "OFFLINE"
    online, stale = _presence_map()
    key = str(device.id)
    if key in online:
        return "ONLINE"
    if key in stale:
        return "DEGRADED"
    if device.last_seen_at is not None:
        now = utcnow()
        seen = device.last_seen_at
        if seen.tzinfo is None:
            from datetime import UTC

            seen = seen.replace(tzinfo=UTC)
        if now - seen <= RECENT_WINDOW:
            return "RECENTLY_SEEN"
    return "OFFLINE"


def public_device(device: Device) -> dict:
    from app.everywhere.owner import owner_scope

    scope = owner_scope(f"device:{device.name}", device=device)
    is_sandbox = str(getattr(device, "memory_scope", "") or "").lower() == "sandbox"
    trusted_owner = device.revoked_at is None and not is_sandbox
    return {
        "device_id": str(device.id),
        "display_name": device.name,
        "owner_id": str(device.owner_id) if device.owner_id else None,
        "device_type": device.device_type or ("phone" if (device.role or "").endswith("companion") else None),
        "role": device.role or "companion",
        "platform": device.platform,
        "client_version": device.client_version,
        "protocol_version": getattr(device, "protocol_version", None),
        # PART 20: explicit auth-state categories per physical endpoint.
        "trust_state": (
            "revoked" if device.revoked_at is not None
            else ("TRUSTED_OWNER_DEVICE" if trusted_owner else "PAIRED_SANDBOX")
        ),
        "owner_scope_resolved": scope,
        "bootstrap_allowed": True,
        "life_read_allowed": scope == "master",
        "life_write_allowed": scope == "master",
        "memory_scope": getattr(device, "memory_scope", None),
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "presence_state": presence_state(device),
        "sync_cursor_at": device.sync_cursor_at.isoformat() if device.sync_cursor_at else None,
        "capabilities": list(device.capabilities or []),
    }


async def list_devices(session: AsyncSession, *, include_revoked: bool = False) -> list[dict]:
    stmt = select(Device).order_by(Device.created_at.asc())
    rows = (await session.execute(stmt)).scalars().all()
    out = [public_device(d) for d in rows]
    if not include_revoked:
        out = [d for d in out if d["trust_state"] != "revoked"]
    return out


async def health_summary(session: AsyncSession) -> dict:
    """G2 health: bounded diagnostics feed for Mission Control / self-check."""
    rows = (await session.execute(select(Device))).scalars().all()
    devices = [public_device(d) for d in rows if d.revoked_at is None]
    online = [d for d in devices if d["presence_state"] == "ONLINE"]
    issues = [
        {"device_id": d["device_id"], "display_name": d["display_name"], "state": d["presence_state"]}
        for d in devices
        if d["presence_state"] in ("OFFLINE", "DEGRADED")
    ]
    from sqlalchemy import func
    from sqlalchemy import select as _select

    from app.everywhere.approvals import PENDING_STATES
    from app.models import ApprovedAction, LifeOutboundAction, Notification

    pending_approvals = (
        await session.execute(
            _select(func.count()).select_from(ApprovedAction).where(ApprovedAction.status.in_(PENDING_STATES))
        )
    ).scalar_one()
    queued_actions = (
        await session.execute(
            _select(func.count()).select_from(LifeOutboundAction).where(LifeOutboundAction.status == "queued")
        )
    ).scalar_one()
    failed_actions = (
        await session.execute(
            _select(func.count()).select_from(LifeOutboundAction).where(LifeOutboundAction.status == "failed")
        )
    ).scalar_one()
    notification_backlog = (
        await session.execute(
            _select(func.count())
            .select_from(Notification)
            .where(Notification.status == "delivered", Notification.attention_kind != "acknowledged")
        )
    ).scalar_one()
    synced = [d for d in devices if d["sync_cursor_at"]]
    last_sync = max((d["sync_cursor_at"] for d in synced), default=None)
    return {
        "devices_total": len(devices),
        "devices_online": len(online),
        "devices_offline_or_degraded": len(issues),
        "issues": issues,
        "last_sync_at": last_sync,
        "pending_device_actions": int(queued_actions or 0),
        "failed_device_actions": int(failed_actions or 0),
        "pending_approvals": int(pending_approvals or 0),
        "notification_backlog": int(notification_backlog or 0),
    }
