"""Authenticated text-turn authority, independent of microphone/live sessions."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, PhoneTurnReceipt
from app.utils.text import utcnow

from .lease import _when, claim_lease, current_lease, heartbeat_lease, lease_belongs
from .sandbox import is_sandbox_device


@dataclass(frozen=True)
class PhoneTextContext:
    device_id: str
    instance_id: str
    lease_id: str
    generation: int
    auth_revision: int
    origin: str

    @property
    def confirmation_binding(self) -> str:
        return sha256(repr(self).encode()).hexdigest()


def _failed(code: str, reply: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "error_code": code, "reply": reply,
            "spoken": reply, "executed": False, "verified": False}


def _reply(receipt: dict[str, Any]) -> dict[str, Any]:
    failures = [result for result in receipt.get("phone_results", [])
                if isinstance(result, dict) and result.get("ok") is False]
    if failures:
        failure = failures[-1]
        code = str(failure.get("error_code") or failure.get("error") or failure.get("failure") or "PHONE_ACTION_FAILED")
        reply = str(failure.get("spoken") or failure.get("reply") or "That phone action could not be completed.")
        return {**receipt, **_failed(code, reply), "route": receipt.get("core_route") or "phone_text"}
    return {**receipt, "ok": bool(receipt.get("ok")) and receipt.get("core_route") != "unavailable",
            "reply": receipt.get("core_reply") or receipt.get("reply") or "I couldn't complete that request.",
            "route": receipt.get("core_route") or "phone_text"}


async def run_phone_text(
    session: AsyncSession, *, device: Device, text: str, instance_id: str,
    origin: str, request_id: str | None,
) -> dict[str, Any]:
    from .turn_receipts import public_receipt, record_turn_receipt

    instance = (instance_id or "default")[:64]
    request_key = request_id or str(uuid4())
    key = "text-" + sha256(f"{device.id}:{request_key}".encode()).hexdigest()
    existing = (await session.execute(select(PhoneTurnReceipt).where(
        PhoneTurnReceipt.idempotency_key == key,
    ))).scalar_one_or_none()
    if device.revoked_at is not None or is_sandbox_device(device):
        return _failed("DEVICE_TRUST_CHANGED", "This phone needs a current owner-approved connection.")
    if existing is not None:
        # Replay neither claims a lease nor invokes the kernel again.
        if existing.device_id != device.id:
            return _failed("IDEMPOTENCY_KEY_CONFLICT", "That request belongs to another phone.")
        payload = public_receipt(existing, replayed=True)
        lease = await current_lease(session)
        if lease is not None:
            await session.refresh(lease)
        matches = False
        if (lease is not None and lease_belongs(lease, device_id=device.id, instance_id=instance)
                and (_when(lease.expires_at) or utcnow()) > utcnow()):
            candidate = PhoneTextContext(
                device_id=str(device.id), instance_id=instance, lease_id=lease.lease_id,
                generation=lease.client_generation, auth_revision=device.auth_revision,
                origin=origin.rstrip("/"),
            )
            matches = (existing.evidence or {}).get("text_binding") == candidate.confirmation_binding
        if not matches and (payload.get("phone_action") or payload.get("phone_actions")):
            payload.pop("phone_action", None)
            payload.pop("phone_actions", None)
            payload["actions_withheld"] = "PHONE_CONTEXT_CHANGED"
            payload["core_reply"] = "That earlier action belongs to a different phone context. Please request it again here."
        elif matches:
            from .durable_actions import reconcile_recovered_actions

            payload = await reconcile_recovered_actions(session, device_id=str(device.id), payload=payload)
        return _reply(payload)
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        return _failed("PHONE_ORIGIN_REQUIRED", "Open Evie through its private HTTPS address.")
    lease = await current_lease(session)
    if lease is not None and not lease_belongs(lease, device_id=device.id, instance_id=instance):
        return _failed("CONVERSATION_ELSEWHERE", "The conversation is active on another phone or tab. Switch it here before trying that action.")
    if lease is None:
        lease = await claim_lease(session, device_id=device.id, instance_id=instance, method="text")
    else:
        await heartbeat_lease(session, device_id=device.id, instance_id=instance)
        await session.flush()
    # Reuse a matching live lease without rotating it or clearing its session.
    context = PhoneTextContext(
        device_id=str(device.id), instance_id=lease.instance_id, lease_id=lease.lease_id,
        generation=lease.client_generation, auth_revision=device.auth_revision,
        origin=origin.rstrip("/"),
    )
    receipt = await record_turn_receipt(
        session, device=device, idempotency_key=key, transcript=text,
        kind="text", text_context=context,
    )
    return _reply(receipt)


async def execute_phone_text_tool(
    session: AsyncSession, name: str, arguments: dict[str, Any], *,
    context: PhoneTextContext, transcript: str, prepare_only: bool,
) -> dict[str, Any]:
    from .mobile_actions.tool import dispatch_phone_action

    if "confirm_action_id" in arguments:
        return _failed("OWNER_CONFIRMATION_REQUIRED", "Confirm the pending action on this phone.")
    device = await session.get(Device, UUID(context.device_id), populate_existing=True)
    if (device is None or device.revoked_at is not None or is_sandbox_device(device)
            or device.auth_revision != context.auth_revision):
        return _failed("DEVICE_TRUST_CHANGED", "This phone's authorization changed. Please reconnect.")
    lease = await current_lease(session)
    if lease is not None:
        await session.refresh(lease)
    if (lease is None or not lease_belongs(lease, device_id=device.id, instance_id=context.instance_id)
            or lease.lease_id != context.lease_id or lease.client_generation != context.generation
            or (_when(lease.expires_at) or utcnow()) <= utcnow()):
        return _failed("PHONE_CONTEXT_CHANGED", "The conversation moved while I was thinking. Please ask again.")
    if name == "capability.discover":
        from .mobile_actions.service import status_snapshot

        snapshot = status_snapshot(device_id=str(device.id), role=device.role or "companion", display_name=device.name)
        return {"ok": True, "capabilities": snapshot.get("capabilities", [])}
    if name == "phone.read":
        from .phone_core import maybe_phone_core_read

        return await maybe_phone_core_read(session, device=device, text=str(arguments.get("query") or transcript)) or _failed("PHONE_READ_UNAVAILABLE", "That phone data is unavailable.")
    if name != "phone_action":
        return _failed("PHONE_TOOL_NOT_ALLOWED", "That is not a phone-local capability.")
    if prepare_only:
        return _failed("PREPARE_ONLY", "This turn is prepare-only; no phone action was authorized.")
    return await dispatch_phone_action(
        device_id=str(device.id), role=device.role or "companion", instance_id=context.instance_id,
        session_id=None, origin=context.origin, arguments=arguments, transcript=transcript,
        device_label=device.name, db_session=session,
        allow_home_station_fallback=True,
        require_confirmation_context=True, text_confirmation_binding=context.confirmation_binding,
    )


async def text_context_still_current(
    session: AsyncSession, *, context: PhoneTextContext,
) -> str | None:
    """``None`` when the typed turn still holds the conversation, else a code.

    Mirrors the guard inside ``execute_phone_text_tool`` so that semantic tools
    on a typed phone turn carry the same authority binding as device-local ones.
    """

    from .lease import _when, current_lease, lease_belongs

    try:
        device = await session.get(Device, UUID(context.device_id), populate_existing=True)
    except (TypeError, ValueError):
        return "DEVICE_TRUST_CHANGED"
    if (device is None or device.revoked_at is not None or is_sandbox_device(device)
            or device.auth_revision != context.auth_revision):
        return "DEVICE_TRUST_CHANGED"
    lease = await current_lease(session)
    if lease is not None:
        await session.refresh(lease)
    if (lease is None
            or not lease_belongs(lease, device_id=device.id, instance_id=context.instance_id)
            or lease.lease_id != context.lease_id
            or lease.client_generation != context.generation
            or (_when(lease.expires_at) or utcnow()) <= utcnow()):
        return "PHONE_CONTEXT_CHANGED"
    return None


async def maybe_cancel_phone_text(
    session: AsyncSession, *, context: PhoneTextContext, transcript: str,
) -> dict[str, Any] | None:
    """Owner cancellation targets its pending phone action before global reflexes."""
    from .mobile_actions.store import pending_confirmation
    from .mobile_actions.trust import classify_utterance

    if classify_utterance(transcript) != "no":
        return None
    pending = pending_confirmation(context.device_id)
    if pending is None:
        return None
    return await execute_phone_text_tool(
        session, "phone_action", {"operation": str(pending.get("operation") or "")},
        context=context, transcript=transcript, prepare_only=False,
    )
