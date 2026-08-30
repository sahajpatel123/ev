"""Park-and-resume confirmation tickets on existing ``ApprovedAction`` rows.

Voice R3/R4 cannot stall the realtime loop waiting for a HUD tap. Policy
already returns ``confirm``. This module parks the original arguments plus a
target-bound TTL ticket in ``payload['_pol']``, keeps the live audio loop
alive, and resumes the same tool after an independent HTTP/HUD approve.

This is not a new job framework or a fourth registry. The ticket is metadata
on the action the owner already has.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.policy import PolicyDecision, canonical_target, ttl_for
from app.models import ApprovedAction, Device
from app.utils.text import utcnow

POL_META_KEY = "_pol"


def pol_meta(payload: dict | None) -> dict[str, Any]:
    raw = (payload or {}).get(POL_META_KEY) if isinstance(payload, dict) else None
    return dict(raw) if isinstance(raw, dict) else {}


def tool_arguments(payload: dict | None) -> dict[str, Any]:
    """Strip POL metadata so ``additionalProperties: False`` schemas still validate."""

    return {key: value for key, value in dict(payload or {}).items() if key != POL_META_KEY}


def args_fingerprint(payload: dict | None) -> str:
    canonical = json.dumps(tool_arguments(payload), sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def confirmation_expired(payload: dict | None, *, now: datetime | None = None) -> bool:
    expires = parse_iso(pol_meta(payload).get("expires_at"))
    if expires is None:
        return False
    clock = now or utcnow()
    if expires.tzinfo is None and clock.tzinfo is not None:
        expires = expires.replace(tzinfo=clock.tzinfo)
    return clock > expires


def payload_tampered(payload: dict | None) -> bool:
    """Refuse resume when the parked target/args were swapped after the hold."""

    meta = pol_meta(payload)
    expected = str(meta.get("args_fingerprint") or "")
    if not expected:
        return False
    if expected != args_fingerprint(payload):
        return True
    stored_target = str(meta.get("target") or "").strip()
    name = str(meta.get("name") or "")
    current = canonical_target(name, tool_arguments(payload)) if name else None
    return bool(
        stored_target and current and stored_target.strip().lower() != current.strip().lower()
    )


def expire_action(action: ApprovedAction, *, reason: str, now: datetime | None = None) -> None:
    clock = now or utcnow()
    action.status = "denied"
    action.denied_at = clock
    action.denied_reason = reason
    action.updated_at = clock


def _device_uuid(value) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _title_for(decision: PolicyDecision, name: str) -> str:
    spoken = str(decision.spoken or f"Confirm {name}").strip()
    return spoken[:256]


async def park_confirmation(
    session: AsyncSession,
    *,
    name: str,
    arguments: dict,
    decision: PolicyDecision,
    actor: str,
    device_id=None,
    live_session_id: str | None = None,
    request_id: str | None = None,
    channel: str = "voice",
    now: datetime | None = None,
) -> ApprovedAction:
    """Create a pending, target-bound confirmation ticket. Never waits for the tap."""

    clock = now or utcnow()
    ttl = decision.confirmation_ttl_seconds or ttl_for(decision.risk_class) or 120
    expires_at = clock + timedelta(seconds=int(ttl))
    target = decision.target or canonical_target(name, arguments)
    payload = dict(arguments or {})
    fingerprint_source = dict(payload)
    payload[POL_META_KEY] = {
        "name": name,
        "target": target,
        "risk_class": decision.risk_class,
        "ttl_seconds": int(ttl),
        "expires_at": expires_at.isoformat(),
        "issued_at": clock.isoformat(),
        "channel": channel,
        "live_session_id": str(live_session_id) if live_session_id else None,
        "request_id": request_id,
        "device_id": str(device_id) if device_id else None,
        "independent": True,
        "resume_on_approve": True,
        "audio_loop": "alive",
        "args_fingerprint": args_fingerprint(fingerprint_source),
    }
    existing = await _reuse_open_ticket(
        session,
        name=name,
        request_id=request_id,
        live_session_id=live_session_id,
        fingerprint=payload[POL_META_KEY]["args_fingerprint"],
    )
    if existing is not None:
        existing.payload = payload
        existing.title = _title_for(decision, name)
        existing.updated_at = clock
        await session.flush()
        return existing
    if live_session_id:
        await _supersede_session_holds(session, live_session_id, now=clock)
    bound_id = _device_uuid(device_id)
    if bound_id is not None and await session.get(Device, bound_id) is None:
        bound_id = None
    action = ApprovedAction(
        action_type=name,
        title=_title_for(decision, name),
        payload=payload,
        requires_approval=True,
        status="pending",
        requested_by=actor,
        device_id=bound_id,
    )
    session.add(action)
    await session.flush()
    return action


async def _reuse_open_ticket(
    session: AsyncSession,
    *,
    name: str,
    request_id: str | None,
    live_session_id: str | None,
    fingerprint: str,
) -> ApprovedAction | None:
    if not request_id and not live_session_id:
        return None
    rows = (
        (
            await session.execute(
                select(ApprovedAction)
                .where(
                    ApprovedAction.status == "pending",
                    ApprovedAction.action_type == name,
                )
                .order_by(ApprovedAction.created_at.desc())
                .limit(16)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        meta = pol_meta(row.payload)
        if request_id and meta.get("request_id") == request_id:
            return row
        if (
            live_session_id
            and meta.get("live_session_id") == str(live_session_id)
            and meta.get("args_fingerprint") == fingerprint
        ):
            return row
    return None


async def _supersede_session_holds(
    session: AsyncSession,
    live_session_id: str,
    *,
    now: datetime,
) -> None:
    """One live socket holds one HUD confirm. Newer requests replace older tickets."""

    rows = (
        (
            await session.execute(
                select(ApprovedAction)
                .where(ApprovedAction.status == "pending")
                .order_by(ApprovedAction.created_at.desc())
                .limit(32)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        meta = pol_meta(row.payload)
        if meta.get("live_session_id") == str(live_session_id):
            expire_action(row, reason="superseded", now=now)


async def attach_hold_to_live(
    payload: dict,
    *,
    live_session_id: str | None = None,
    device_id=None,
) -> bool:
    """Park the HUD card on the live socket. Do not pause or close the audio loop."""

    from app.voice.live.layer import live_for_device, live_for_session

    live = live_for_session(live_session_id)
    if live is None:
        live = live_for_device(str(device_id) if device_id else None)
    if live is None:
        return False
    apply = getattr(live, "apply_approval_hold", None)
    if apply is None:
        return False
    await apply(payload, speak=True)
    return True


async def deliver_parked_result(action: ApprovedAction, result: dict | None) -> bool:
    """Speak the completed (or honest failed) result on the still-alive live socket."""

    from app.ev.tools import life_success_reply
    from app.voice.live.layer import live_for_device, live_for_session

    meta = pol_meta(action.payload)
    live = live_for_session(meta.get("live_session_id"))
    if live is None:
        live = live_for_device(str(meta.get("device_id") or action.device_id or "") or None)
    if live is None:
        return False
    payload = result if isinstance(result, dict) else {}
    spoken = str(
        payload.get("spoken")
        or life_success_reply(payload, tool_name=action.action_type)
    )
    complete = getattr(live, "complete_approval_hold", None)
    if complete is None:
        return False
    await complete(action.action_type, payload, spoken=spoken)
    return True


async def release_parked_hold(action: ApprovedAction, *, spoken: str | None = None) -> bool:
    from app.voice.live.layer import live_for_device, live_for_session

    meta = pol_meta(action.payload)
    if not meta:
        return False
    live = live_for_session(meta.get("live_session_id"))
    if live is None:
        live = live_for_device(str(meta.get("device_id") or action.device_id or "") or None)
    if live is None:
        return False
    clear = getattr(live, "clear_approval_hold", None)
    if clear is None:
        return False
    await clear(spoken=spoken)
    return True
