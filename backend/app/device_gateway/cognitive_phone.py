"""Phone-only capability projection for Muse; authority never comes from tool args."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import ToolSpec
from app.models import Device
from app.utils.text import utcnow
from app.voice.live.layer import live_for_session

from . import PAIRABLE_ROLES
from .lease import _when
from .live_authority import assert_live_authority
from .mobile_actions.tool import dispatch_phone_action, phone_action_parameters
from .sandbox import is_sandbox_device


@dataclass(frozen=True)
class PhoneTurnBinding:
    live: Any
    identity: tuple[Any, ...]


def _binding(device: Device, live: Any, lease: Any) -> PhoneTurnBinding | None:
    if lease is None or not getattr(lease, "lease_id", None):
        return None
    if (
        live_for_session(str(lease.session_id or "")) is not live
        or str(getattr(live, "device_id", "")) != str(device.id)
        or lease.device_id != device.id
        or getattr(live, "session_id", None) != lease.session_id
        or not lease.instance_id
        or getattr(live, "instance_id", None) != lease.instance_id
        or getattr(live, "lease_id", None) != lease.lease_id
        or getattr(live, "client_generation", None) != lease.client_generation
        or getattr(live, "auth_revision", None) != device.auth_revision
        or getattr(live, "_closed", False)
        or (_when(lease.expires_at) or utcnow()) <= utcnow()
    ):
        return None
    return PhoneTurnBinding(live=live, identity=(
        str(device.id), device.auth_revision, device.role, device.memory_scope,
        str(lease.device_id), lease.session_id, lease.instance_id, lease.lease_id,
        lease.client_generation, getattr(live, "client_generation", 0),
        getattr(live, "auth_revision", None), getattr(live, "instance_id", None),
        getattr(live, "gateway_origin", None), live.device_id, live.session_id, live.lease_id,
    ))


async def capture_phone_binding(
    session: AsyncSession, *, device_id: str, live_session_id: str | None,
) -> PhoneTurnBinding | None:
    """Capture server authority before inference; never reconstruct it from args."""
    if not live_session_id:
        return None
    try:
        device = await session.get(Device, UUID(device_id), populate_existing=True)
        if device is None or device.revoked_at is not None or is_sandbox_device(device):
            return None
        live, lease = await assert_live_authority(session, device=device, session_id=live_session_id)
        if lease is not None:
            await session.refresh(lease)
        return _binding(device, live, lease)
    except (HTTPException, ValueError, TypeError):
        return None


async def is_phone_turn(session: AsyncSession, device_id: str | None) -> bool:
    try:
        device = await session.get(Device, UUID(str(device_id)))
    except (ValueError, TypeError):
        return False
    return bool(device and (
        device.role in PAIRABLE_ROLES or device.platform == "ios"
        or device.device_type == "phone"
    ))


def phone_tool_specs() -> list[ToolSpec]:
    parameters = phone_action_parameters()
    # Confirmation is owner input, never a capability the model can grant.
    parameters["properties"].pop("confirm_action_id", None)
    return [
        ToolSpec(
            name="phone_action",
            description="Request an approved action on the originating iPhone. The broker checks availability and confirmation; accepted is not executed. Never substitute a Mac action.",
            parameters=parameters,
            read_only=False, permission="phone:act", risk_class="R2",
        ),
        ToolSpec(
            name="phone.read",
            description="Read the originating phone's server-validated weather, calendar, contacts, notifications, identity or capability state. Pass the owner's question unchanged. Unavailable data is not an empty result.",
            parameters={"type": "object", "additionalProperties": False,
                        "properties": {"query": {"type": "string", "maxLength": 4000}},
                        "required": ["query"]},
            read_only=True, permission="phone:read", risk_class="R0",
        ),
        ToolSpec(
            name="capability.discover", description="List approved phone-local actions, not Mac tools.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            read_only=True, permission="phone:read", risk_class="R0",
        ),
    ]


def _failure(code: str, spoken: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "spoken": spoken, "executed": False, "verified": False}


async def execute_phone_tool(
    session: AsyncSession, name: str, arguments: dict[str, Any], *,
    device_id: str, live_session_id: str | None, transcript: str,
    prepare_only: bool = False,
    expected_binding: PhoneTurnBinding | None = None,
) -> dict[str, Any]:
    if "confirm_action_id" in arguments:
        return _failure("OWNER_CONFIRMATION_REQUIRED", "Confirm the pending action on this phone.")
    # Re-read trust on every tool call, not once before a potentially slow model.
    try:
        device = await session.get(Device, UUID(device_id), populate_existing=True)
    except (ValueError, TypeError):
        device = None
    if device is None or device.revoked_at is not None or is_sandbox_device(device):
        return _failure("PHONE_NOT_TRUSTED", "This phone needs a current owner-approved connection.")
    if not live_session_id:
        return _failure("PHONE_SESSION_REQUIRED", "Start Talk on this phone to bind phone actions to its current session.")
    try:
        live, lease = await assert_live_authority(session, device=device, session_id=live_session_id)
        if lease is not None:
            await session.refresh(lease)
        if (lease is None or lease.device_id != device.id or lease.session_id != live_session_id
                or not lease.instance_id or lease.instance_id != getattr(live, "instance_id", None)
                or (_when(lease.expires_at) or utcnow()) <= utcnow()
                or getattr(live, "_closed", False)):
            return _failure("PHONE_LEASE_CHANGED", "The phone session changed. Reconnect before trying that action.")
    except HTTPException:
        return _failure("PHONE_SESSION_CHANGED", "The phone session changed. Reconnect before trying that action.")
    origin = str(getattr(live, "gateway_origin", "") or "")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return _failure("PHONE_ORIGIN_MISSING", "Reconnect through this phone's private HTTPS Evie address.")
    if name == "capability.discover":
        from .mobile_actions.service import status_snapshot

        snapshot = status_snapshot(device_id=str(device.id), role=device.role or "companion", display_name=device.name)
        # Discovery is model-visible. Do not expose the last action's signed
        # launch links or receipt credentials along with the capability catalog.
        return {"ok": True, "capabilities": snapshot.get("capabilities", [])}
    if name == "phone.read":
        from .phone_core import maybe_phone_core_read

        return await maybe_phone_core_read(session, device=device, text=str(arguments.get("query") or transcript)) or _failure("PHONE_READ_UNAVAILABLE", "That phone data is not available through this reader.")
    if name != "phone_action":
        return _failure("PHONE_TOOL_NOT_ALLOWED", "That is not a phone-local capability. No Mac action was run.")
    if prepare_only:
        return _failure("PREPARE_ONLY", "This turn is prepare-only; no phone action was authorized.")
    actual_binding = _binding(device, live, lease)
    if (expected_binding is None or actual_binding is None
            or expected_binding.live is not live
            or expected_binding.identity != actual_binding.identity):
        return _failure("PHONE_CONTEXT_CHANGED", "The phone connection changed while I was thinking. Please ask again.")
    # Instance, origin, role and target identity are never taken from model input.
    return await dispatch_phone_action(
        device_id=str(device.id), role=device.role or "companion",
        instance_id=lease.instance_id, session_id=live_session_id, origin=origin,
        arguments=arguments, transcript=transcript, device_label=device.name,
        allow_home_station_fallback=False, db_session=session,
        require_confirmation_context=True,
    )
