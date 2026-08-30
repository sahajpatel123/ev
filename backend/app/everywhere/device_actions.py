"""G2 B — Cross-device capability routing broker.

Canonical broker for ONE EVIE routed actions: iPhone requests, Mac executes.
Deterministic resolver, idempotent by action_id, durable, revocation-aware,
approval-aware, offline-queued. No model routes devices.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, DeviceRoutedAction
from app.utils.text import utcnow

from .capabilities import KNOWN_CAPABILITY_BASES  # for validation truth
from .devices import presence_state

# Allowed routed capabilities (canonical truth). Unknown are rejected (B5).
# These are safe, reversible, low-risk, tied to one endpoint.
ALLOWED_ROUTED_CAPABILITIES: frozenset[str] = frozenset(
    {
        "device.echo",
        "device.ping",
        "mac.notify",
        "mac.echo",
        "mac.computer.echo",
        # Reuse existing safe canary via routing instead of synchronous helper
        "computer.open_calculator",
        "computer.close_calculator",
    }
)

# For capability advertisement filtering: which client bases map to routed caps
ROUTED_CAPABILITY_BASE_MAP: dict[str, str] = {
    "device_echo": "device.echo",
    "mac_notify": "mac.notify",
    "computer_control": "computer.open_calculator",
}

# Action TTL: queued actions expire if target never comes online
ACTION_TTL_SECONDS = 300  # 5 minutes
# Terminal states are immutable
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"})

# Risk: our safe caps are R1 (no approval). Higher risk would require ApprovedAction.
RISK_FOR_CAPABILITY: dict[str, str] = {cap: "R1" for cap in ALLOWED_ROUTED_CAPABILITIES}


def _is_trusted(device: Device | None) -> bool:
    if device is None or device.revoked_at is not None:
        return False
    scope = str(getattr(device, "memory_scope", "") or "").lower()
    return scope != "sandbox"


def _ensure_aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        from datetime import UTC

        return dt.replace(tzinfo=UTC)
    return dt


def _error(status: int, code: str, message: str, **extra) -> dict:
    return {"ok": False, "error_code": code, "message": message, "status": status, **extra}


async def _resolve_target(
    session: AsyncSession,
    *,
    capability: str,
    requesting_device: Device,
) -> dict:
    """Deterministic device-capability resolver (B2)."""
    cap = capability.strip().lower()
    if cap not in ALLOWED_ROUTED_CAPABILITIES:
        return _error(422, "CAPABILITY_UNAVAILABLE", f"Capability {cap} is not routed", capability=cap)

    # Load all trusted, non-revoked devices
    rows = (await session.execute(select(Device).where(Device.revoked_at.is_(None)))).scalars().all()
    trusted = [d for d in rows if _is_trusted(d)]

    # Filter by capability eligibility
    eligible: list[Device] = []
    for d in trusted:
        if d.id == requesting_device.id:
            # Never route to self for echo-type; but allow if no other candidate?
            # For device.echo we want OTHER device; for self-tests we allow self.
            # Keep self out for cross-device proof unless only one device exists.
            continue
        if cap in {"device.echo", "device.ping"}:
            eligible.append(d)
        elif cap in {"mac.notify", "mac.echo", "mac.computer.echo"}:
            if (d.role == "home_station") or (d.device_type or "").lower() in {"desktop", "mac", "macos"} or "computer_control" in (d.capabilities or []):
                eligible.append(d)
        elif cap.startswith("computer."):
            if "computer_control" in (d.capabilities or []) or d.role == "home_station":
                eligible.append(d)
        else:
            eligible.append(d)

    # Fallback: if no OTHER device eligible but requesting device itself could handle it,
    # allow self for single-device tests (keeps unit tests hermetic). Real cross-device
    # proof needs two devices so this fallback won't hide routing bugs in physical proof.
    if not eligible and cap in {"device.echo", "device.ping"}:
        eligible = [d for d in trusted if d.id != requesting_device.id] or trusted

    if not eligible:
        return _error(404, "CAPABILITY_UNAVAILABLE", "No eligible trusted device for capability", capability=cap)

    # Partition by presence
    online_candidates: list[Device] = []
    offline_candidates: list[Device] = []
    for d in eligible:
        ps = presence_state(d)
        if ps == "ONLINE":
            online_candidates.append(d)
        elif ps in {"OFFLINE", "DEGRADED", "RECENTLY_SEEN"}:
            offline_candidates.append(d)
        else:
            offline_candidates.append(d)

    # Sort deterministically: presence priority then last_seen_at desc then created_at
    def _sort_key(d: Device):
        ps = presence_state(d)
        online_rank = 0 if ps == "ONLINE" else 1
        ls = _ensure_aware(d.last_seen_at) if d.last_seen_at else None
        seen_ts = ls.timestamp() if ls else 0
        return (online_rank, -seen_ts, str(d.id))

    online_candidates.sort(key=_sort_key)
    offline_candidates.sort(key=_sort_key)

    if online_candidates:
        return {"ok": True, "target": online_candidates[0], "presence": "ONLINE", "candidates": online_candidates, "offline": offline_candidates}
    if offline_candidates:
        # No online device; caller decides queue vs TARGET_DEVICE_OFFLINE per policy (B7)
        return {"ok": False, "error_code": "TARGET_DEVICE_OFFLINE", "target": offline_candidates[0], "candidates": [], "offline": offline_candidates, "presence": "OFFLINE"}
    return _error(404, "TARGET_DEVICE_OFFLINE", "Target device offline", capability=cap)


def _public_action(row: DeviceRoutedAction) -> dict:
    return {
        "action_id": row.action_id,
        "command_id": row.action_id,
        "id": str(row.id),
        "owner_scope": row.owner_scope,
        "requesting_device_id": str(row.requesting_device_id),
        "target_device_id": str(row.target_device_id) if row.target_device_id else None,
        "capability": row.capability,
        "arguments": row.arguments or {},
        "status": row.status,
        "result": row.result,
        "error": row.error,
        "error_code": row.error_code,
        "risk_class": row.risk_class,
        "confirmation_required": bool(row.confirmation_required),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "idempotency_key": row.idempotency_key,
    }


async def create_routed_action(
    session: AsyncSession,
    *,
    requesting_device: Device,
    capability: str,
    arguments: dict | None,
    action_id: str | None,
    owner_scope: str | None = None,
) -> dict:
    """Create or deduplicate routed action (B5-B8).

    Idempotent by (owner_scope, action_id). Returns public action dict with status.
    """
    if requesting_device.revoked_at is not None:
        return _error(401, "DEVICE_REVOKED", "Requesting device revoked")
    if not _is_trusted(requesting_device):
        return _error(403, "DEVICE_NOT_TRUSTED", "Requesting device not trusted")
    cap = (capability or "").strip().lower()
    if not cap:
        return _error(422, "CAPABILITY_UNAVAILABLE", "Missing capability")
    if cap not in ALLOWED_ROUTED_CAPABILITIES:
        return _error(422, "CAPABILITY_UNAVAILABLE", f"Unknown capability {cap}", capability=cap)

    # Approval gate: safe R1 caps need no approval; if we ever add R3, check here.
    # For now all allowed are R1.
    scope = owner_scope or "master"
    # Clean id
    aid = (action_id or "").strip()[:128] or f"ra_{uuid4().hex[:12]}"
    # Idempotency: lookup existing
    existing = (
        await session.execute(
            select(DeviceRoutedAction).where(
                DeviceRoutedAction.owner_scope == scope,
                DeviceRoutedAction.action_id == aid,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Duplicate delivery to source: return existing (single canonical mutation / single side effect)
        return {"ok": True, "duplicate": True, **_public_action(existing)}

    resolved = await _resolve_target(session, capability=cap, requesting_device=requesting_device)
    target_device = resolved.get("target") if isinstance(resolved.get("target"), Device) else None
    presence = resolved.get("presence")

    # If target offline, queue if policy permits (B7). For our safe caps, queue.
    if resolved.get("error_code") == "TARGET_DEVICE_OFFLINE" and target_device is not None:
        # Queue: status QUEUED, expires in 5 min
        row = DeviceRoutedAction(
            action_id=aid,
            owner_scope=scope,
            requesting_device_id=requesting_device.id,
            target_device_id=target_device.id,
            capability=cap,
            arguments=dict(arguments or {}),
            status="QUEUED",
            risk_class=RISK_FOR_CAPABILITY.get(cap, "R1"),
            idempotency_key=aid,
            expires_at=utcnow() + timedelta(seconds=ACTION_TTL_SECONDS),
        )
        session.add(row)
        await session.flush()
        return {"ok": True, "queued": True, "error_code": "TARGET_DEVICE_OFFLINE", "target_offline": True, **_public_action(row)}

    if resolved.get("ok") is not True:
        # No eligible or other resolver error
        err = resolved.get("error_code") or "TARGET_DEVICE_OFFLINE"
        return {"ok": False, "error_code": err, "message": resolved.get("message") or "No target", "capability": cap}

    # Route to online target
    assert target_device is not None
    if target_device.revoked_at is not None:
        return _error(403, "DEVICE_REVOKED", "Target revoked before execution")

    row = DeviceRoutedAction(
        action_id=aid,
        owner_scope=scope,
        requesting_device_id=requesting_device.id,
        target_device_id=target_device.id,
        capability=cap,
        arguments=dict(arguments or {}),
        status="ROUTED",
        risk_class=RISK_FOR_CAPABILITY.get(cap, "R1"),
        idempotency_key=aid,
        expires_at=utcnow() + timedelta(seconds=ACTION_TTL_SECONDS),
    )
    session.add(row)
    await session.flush()
    return {"ok": True, "routed": True, **_public_action(row)}


async def get_action(session: AsyncSession, action_id: str, *, owner_scope: str = "master") -> DeviceRoutedAction | None:
    row = (
        await session.execute(
            select(DeviceRoutedAction).where(
                DeviceRoutedAction.owner_scope == owner_scope,
                DeviceRoutedAction.action_id == action_id,
            )
        )
    ).scalar_one_or_none()
    return row


async def list_pending_for_target(session: AsyncSession, *, target_device: Device) -> list[dict]:
    """Target device polls for its pending actions (ROUTED/QUEUED)."""
    if target_device.revoked_at is not None:
        return []
    # expire stale
    now = _ensure_aware(utcnow())
    rows = (
        await session.execute(
            select(DeviceRoutedAction).where(
                DeviceRoutedAction.target_device_id == target_device.id,
                DeviceRoutedAction.status.in_(["ROUTED", "QUEUED"]),
            )
        )
    ).scalars().all()
    out: list[dict] = []
    for r in rows:
        # Expiry check
        exp_aware = _ensure_aware(r.expires_at)
        if exp_aware and exp_aware <= now:
            r.status = "EXPIRED"
            r.error_code = "ACTION_EXPIRED"
            r.error = "Action expired before execution"
            continue
        # Revocation fence: if target revoked since creation, cancel
        if target_device.revoked_at is not None:
            r.status = "CANCELLED"
            r.error_code = "DEVICE_REVOKED"
            r.error = "Target revoked before execution"
            continue
        out.append(_public_action(r))
    await session.flush()
    # Sort by creation time
    out.sort(key=lambda x: x["created_at"] or "")
    return out


async def claim_action(session: AsyncSession, *, action_id: str, claiming_device: Device, owner_scope: str = "master") -> dict:
    """Target claims action for execution exactly once (B8)."""
    row = await get_action(session, action_id, owner_scope=owner_scope)
    if row is None:
        return _error(404, "NOT_FOUND", "Action not found")
    if str(row.target_device_id) != str(claiming_device.id):
        return _error(403, "WRONG_TARGET", "This device is not the target")
    if claiming_device.revoked_at is not None:
        row.status = "CANCELLED"
        row.error_code = "DEVICE_REVOKED"
        row.error = "Target revoked before claim"
        await session.flush()
        return _error(403, "DEVICE_REVOKED", "Target revoked")
    now = _ensure_aware(utcnow())
    exp_aware = _ensure_aware(row.expires_at)
    if exp_aware and exp_aware <= now:
        row.status = "EXPIRED"
        row.error_code = "ACTION_EXPIRED"
        await session.flush()
        return _error(410, "ACTION_EXPIRED", "Action expired")
    if row.status in TERMINAL_STATUSES:
        return {"ok": True, "already_terminal": True, **_public_action(row)}
    if row.status == "EXECUTING":
        return {"ok": True, "already_claimed": True, **_public_action(row)}
    if row.status in {"ROUTED", "QUEUED", "REQUESTED"}:
        row.status = "EXECUTING"
        row.claimed_at = now
        row.updated_at = now
        await session.flush()
        return {"ok": True, "claimed": True, **_public_action(row)}
    return _error(409, "INVALID_STATE", f"Cannot claim from {row.status}")


async def complete_action(
    session: AsyncSession,
    *,
    action_id: str,
    completing_device: Device,
    result: dict | None,
    error: str | None = None,
    error_code: str | None = None,
    success: bool = True,
    owner_scope: str = "master",
) -> dict:
    """Target reports completion (SUCCEEDED/FAILED). Idempotent."""
    row = await get_action(session, action_id, owner_scope=owner_scope)
    if row is None:
        return _error(404, "NOT_FOUND", "Action not found")
    if str(row.target_device_id) != str(completing_device.id):
        return _error(403, "WRONG_TARGET", "Not target")
    if completing_device.revoked_at is not None and row.status != "SUCCEEDED":
        # If execution already completed honestly, record truth; else do not execute
        if row.status in TERMINAL_STATUSES:
            return {"ok": True, **_public_action(row)}
        row.status = "CANCELLED"
        row.error_code = "DEVICE_REVOKED"
        row.error = "Target revoked before completion"
        await session.flush()
        return _error(403, "DEVICE_REVOKED", "Target revoked")
    if row.status in TERMINAL_STATUSES:
        # Duplicate completion: side effect already once, return same
        return {"ok": True, "duplicate": True, **_public_action(row)}
    now = _ensure_aware(utcnow())
    if success:
        row.status = "SUCCEEDED"
        row.result = dict(result or {})
        row.error = None
        row.error_code = None
    else:
        row.status = "FAILED"
        row.result = dict(result or {}) if result else None
        row.error = error or "failed"
        row.error_code = error_code or "FAILED"
    row.completed_at = now
    row.updated_at = now
    await session.flush()
    return {"ok": True, **_public_action(row)}


async def expire_stale(session: AsyncSession) -> int:
    """Background: expire queued actions past TTL."""
    now = _ensure_aware(utcnow())
    rows = (
        await session.execute(
            select(DeviceRoutedAction).where(
                DeviceRoutedAction.status.in_(["ROUTED", "QUEUED", "REQUESTED", "EXECUTING"]),
            )
        )
    ).scalars().all()
    n = 0
    for r in rows:
        exp_aware = _ensure_aware(r.expires_at)
        if exp_aware is None or exp_aware > now:
            continue
        if r.status not in TERMINAL_STATUSES:
            r.status = "EXPIRED"
            r.error_code = "ACTION_EXPIRED"
            n += 1
    if n:
        await session.flush()
    return n
