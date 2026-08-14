"""WAVE LIFE device-aware routing for outbound life actions and attention.

The registry (``devices`` table + runtime heartbeats) is the source of truth
for reachability; capability lists decide which device can execute an action.
Routing never fakes delivery: a job stays ``queued`` until a device is
assigned, and the lifecycle only advances on real evidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Device, LifeOutboundAction
from app.utils.text import utcnow


def action_capability(action: str) -> str:
    """Map a life action to the device capability required to execute it."""
    lowered = action.lower()
    if "messaging" in lowered or lowered in ("mail.send", "message.send"):
        return "messaging"
    if "call" in lowered:
        return "call"
    return "attention"


def device_reachability(
    device: Device,
    now: datetime | None = None,
    *,
    grace_seconds: int | None = None,
) -> str:
    """online / away / unknown from the last-seen heartbeat (explicit)."""
    now = now or utcnow()
    grace = grace_seconds or settings.runtime_heartbeat_grace_seconds
    if device.revoked_at is not None:
        return "unknown"
    last_seen = device.last_seen_at
    if last_seen is None:
        return "unknown"
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=now.tzinfo)
    if now - last_seen <= timedelta(seconds=grace):
        return "online"
    if now - last_seen <= timedelta(days=1):
        return "away"
    return "unknown"


def backend_for_device(device: Device) -> str:
    """Pick the delivery backend a device can actually receive."""
    if (device.device_type or "").lower() == "mac":
        return "macos"
    if device.push_token:
        return "apns"
    return "console"


async def best_reachable_device(
    session: AsyncSession,
    capability: str,
    *,
    exclude_device_id: UUID | None = None,
    now: datetime | None = None,
) -> Device | None:
    """Pick the best non-revoked device that can perform ``capability``."""
    now = now or utcnow()
    rows = (
        await session.execute(
            select(Device).where(Device.revoked_at.is_(None))
        )
    ).scalars().all()
    candidates: list[tuple[int, bool, datetime | None, Device]] = []
    for device in rows:
        if exclude_device_id is not None and device.id == exclude_device_id:
            continue
        caps = {str(c).lower() for c in (device.capabilities or [])}
        if capability.lower() not in caps:
            continue
        reach = device_reachability(device, now)
        rank = {"online": 0, "away": 1, "unknown": 2}[reach]
        last_seen = device.last_seen_at
        if last_seen is not None and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=now.tzinfo)
        candidates.append((rank, bool(device.push_token), last_seen, device))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], not item[1], -(item[2].timestamp() if item[2] else 0)))
    return candidates[0][3]


async def route_notification_target(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> Device | None:
    """Best reachable attention device for a notification."""
    return await best_reachable_device(session, "attention", now=now)


async def assign_life_actions(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> dict:
    """Assign queued, unassigned life actions to the best capable device.

    The outbox status stays ``queued`` (Agent 12's device-poll contract); the
    PULSE lifecycle moves to ``dispatched`` when a device is assigned.
    """
    now = now or utcnow()
    rows = list(
        (
            await session.execute(
                select(LifeOutboundAction)
                .where(
                    LifeOutboundAction.status == "queued",
                    LifeOutboundAction.device_id.is_(None),
                )
                .order_by(LifeOutboundAction.created_at.asc())
                .limit(min(limit, 500))
            )
        ).scalars().all()
    )
    assigned = 0
    unrouted: list[str] = []
    for row in rows:
        capability = action_capability(row.action)
        target = await best_reachable_device(
            session, capability, now=now
        )
        if target is None:
            unrouted.append(str(row.id))
            continue
        row.device_id = target.id
        row.lifecycle = "dispatched"
        row.dispatched_at = now
        row.updated_at = now
        assigned += 1
    await session.flush()
    return {"assigned": assigned, "unrouted": len(unrouted), "unrouted_ids": unrouted}


async def reconcile_life_jobs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> dict:
    """Advance lifecycle from device-posted terminal evidence (never faked)."""
    now = now or utcnow()
    rows = list(
        (
            await session.execute(
                select(LifeOutboundAction).where(
                    LifeOutboundAction.status.in_(["delivered", "failed", "cancelled"]),
                    LifeOutboundAction.lifecycle.notin_(["executed", "failed"]),
                )
            )
        ).scalars().all()
    )
    executed = 0
    failed = 0
    for row in rows:
        if row.status == "delivered":
            row.lifecycle = "executed"
            executed += 1
        else:
            row.lifecycle = "failed"
            failed += 1
        row.updated_at = now
    await session.flush()
    return {"executed": executed, "failed": failed}


async def claim_life_job(
    session: AsyncSession,
    job_id: UUID,
    *,
    device_id: UUID,
    now: datetime | None = None,
) -> LifeOutboundAction:
    """Device acknowledges a dispatched job it has picked up."""
    now = now or utcnow()
    row = await session.get(LifeOutboundAction, job_id)
    if row is None:
        raise KeyError(f"life outbound action {job_id} not found")
    if row.device_id != device_id:
        raise PermissionError("life outbound action is assigned to another device")
    if row.lifecycle in ("executed", "failed"):
        raise ValueError(f"life outbound action is already {row.lifecycle}")
    if row.lifecycle != "acknowledged":
        row.lifecycle = "acknowledged"
        row.acknowledged_at = now
        row.updated_at = now
        await session.flush()
    return row
