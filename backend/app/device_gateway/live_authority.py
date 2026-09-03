"""Lease + session + generation + auth-revision fencing for phone live ops."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device

from .lease import current_lease, lease_belongs
from .webrtc_live import assert_session_owns


async def assert_live_authority(
    session: AsyncSession,
    *,
    device: Device,
    session_id: str,
    instance_id: str = "",
    lease_id: str | None = None,
    client_generation: int | None = None,
):
    live = assert_session_owns(device=device, session_id=session_id)
    current_revision = int(getattr(device, "auth_revision", 1) or 1)
    bound_revision = int(getattr(live, "auth_revision", current_revision) or current_revision)
    if bound_revision != current_revision or device.revoked_at is not None:
        raise HTTPException(
            status_code=409,
            detail="Live session authorization changed. Reconnect.",
            headers={"X-Error-Code": "auth_revision_changed"},
        )
    lease = await current_lease(session)
    inst = instance_id or str(getattr(live, "instance_id", "") or "")
    if inst and not lease_belongs(lease, device_id=device.id, instance_id=inst):
        raise HTTPException(
            status_code=409,
            detail="This phone does not hold the conversation lease.",
            headers={"X-Error-Code": "lease_not_held"},
        )
    if lease_id and lease is not None and lease.lease_id != lease_id:
        raise HTTPException(
            status_code=409,
            detail="Stale lease identity.",
            headers={"X-Error-Code": "lease_mismatch"},
        )
    if client_generation is not None:
        bound_gen = int(getattr(live, "client_generation", 0) or 0)
        incoming = int(client_generation)
        if bound_gen and bound_gen != incoming:
            raise HTTPException(
                status_code=409,
                detail="Stale live generation.",
                headers={"X-Error-Code": "stale_generation"},
            )
        if not bound_gen:
            live.client_generation = incoming
    return live, lease


def parse_generation(value: int | str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_device_uuid(value: str) -> UUID:
    return UUID(str(value))
