"""Durable phone turn receipts. Clients report content; they cannot grant authority."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cognitive.kernel import _UNAVAILABLE as _PHONE_KERNEL_FALLBACK
from app.models import Device, PhoneTurnReceipt
from app.utils.text import utcnow

from .cognitive_text import PhoneTextContext
from .sandbox import is_sandbox_device


def _stamp_takeover(
    row: PhoneTurnReceipt,
    *,
    spoken: str,
    route: str,
    core: dict[str, Any] | None = None,
    phone_action: dict[str, Any] | None = None,
) -> None:
    ev = dict(row.evidence or {})
    ev["core_takeover"] = True
    ev["core_reply"] = spoken[:2000]
    ev["core_route"] = (route or "")[:80]
    action = phone_action if isinstance(phone_action, dict) else None
    if action is None and isinstance(core, dict) and isinstance(core.get("phone_action"), dict):
        action = core["phone_action"]
    if action is not None:
        ev["phone_action"] = action
    if isinstance(core, dict) and not core.get("conversational"):
        tool = str(core.get("tool") or core.get("operation") or "").strip()
        if tool and tool.upper() != "UNKNOWN":
            ev["action_tool"] = tool[:80]
            ev["action_status"] = str(core.get("status") or "")[:32]
            ev["action_accepted"] = bool(core.get("accepted"))
            ev["action_executed"] = bool(core.get("executed"))
            ev["action_verified"] = bool(core.get("verified"))
    row.evidence = ev


async def _apply_muse_kernel_phone_turn(
    session: AsyncSession,
    *,
    device: Device,
    row: PhoneTurnReceipt,
    key: str,
    text_context: PhoneTextContext | None = None,
) -> None:
    """Spark 1.3 decides; Mini never gets a leftover conversational turn."""

    from app.cognitive.kernel import handle_turn

    phone_actions: list[dict[str, Any]] = []
    phone_results: list[dict[str, Any]] = []
    # The general pipeline can execute on the Mac before Muse sees the turn.
    # Phone effects must instead pass the kernel's device-bound phone adapter.
    try:
        kernel = await handle_turn(
            transcript=row.transcript,
            live_session_id=row.session_id,
            device_id=str(device.id),
            modality="text" if text_context is not None else "voice",
            actor=f"device:{device.name}",
            session=session,
            phone_text_context=text_context,
        )
        spoken = str(getattr(kernel, "spoken", "") or "").strip()
        route = str(getattr(kernel, "kind", "") or "muse")[:80]
        phone_actions = [
            action for action in kernel.evidence
            if isinstance(action, dict) and isinstance(action.get("card"), dict)
        ]
        phone_results = [
            {key: value for key, value in result.items() if key in {
                "ok", "error", "error_code", "failure", "spoken", "reply",
                "action_id", "operation", "executed", "verified",
            }}
            for result in kernel.evidence if isinstance(result, dict)
        ]
    except Exception:
        spoken = _PHONE_KERNEL_FALLBACK
        route = "unavailable"
    if not spoken:
        spoken = _PHONE_KERNEL_FALLBACK
        route = route or "unavailable"
    _stamp_takeover(
        row,
        spoken=spoken,
        route=route,
        phone_action=phone_actions[-1] if phone_actions else None,
    )
    if phone_actions:
        row.evidence = {**row.evidence, "phone_actions": phone_actions}
    if phone_results:
        row.evidence = {**row.evidence, "phone_results": phone_results}
    await session.flush()


async def record_turn_receipt(
    session: AsyncSession,
    *,
    device: Device,
    idempotency_key: str,
    transcript: str,
    session_id: str | None = None,
    lease_id: str | None = None,
    provider_item_id: str | None = None,
    provider_response_id: str | None = None,
    action_calls: list[dict[str, Any]] | None = None,
    evidence: dict[str, Any] | None = None,
    kind: str = "final_transcript",
    text_context: PhoneTextContext | None = None,
) -> dict[str, Any]:
    key = (idempotency_key or "").strip()[:128]
    if len(key) < 8:
        return {"ok": False, "error_code": "INVALID_IDEMPOTENCY_KEY", "authority": False}
    existing = (
        await session.execute(select(PhoneTurnReceipt).where(PhoneTurnReceipt.idempotency_key == key))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.device_id != device.id:
            return {"ok": False, "error_code": "IDEMPOTENCY_KEY_CONFLICT", "authority": False}
        if device.revoked_at is not None or (existing.trusted_owner and is_sandbox_device(device)):
            return {"ok": False, "error_code": "DEVICE_TRUST_CHANGED", "authority": False}
        return public_receipt(existing, replayed=True)

    trusted = not is_sandbox_device(device) and device.revoked_at is None
    row = PhoneTurnReceipt(
        device_id=device.id,
        idempotency_key=key,
        kind=(kind or "final_transcript")[:32],
        transcript=(transcript or "")[:8000],
        session_id=(session_id or "")[:64] or None,
        lease_id=(lease_id or "")[:64] or None,
        provider_item_id=(provider_item_id or "")[:128] or None,
        provider_response_id=(provider_response_id or "")[:128] or None,
        action_calls=list(action_calls or []),
        # Client observations cannot populate the server's result namespace.
        evidence={"client_evidence": dict(evidence or {})},
        durable=True,
        life_mutation=False,
        trusted_owner=trusted,
    )
    if text_context is not None:
        row.evidence = {**row.evidence, "text_binding": text_context.confirmation_binding}
    session.add(row)
    await session.flush()

    if trusted and ((row.kind or "") == "final_transcript" or (row.kind == "text" and text_context is not None)) and (row.transcript or "").strip():
        from app.cognitive.mode import muse_kernel_active

        if muse_kernel_active():
            await _apply_muse_kernel_phone_turn(session, device=device, row=row, key=key, text_context=text_context)
        else:
            from .phone_core import maybe_phone_core_read
            from .phone_mac import maybe_phone_mac_act

            core = await maybe_phone_core_read(session, device=device, text=row.transcript)
            if core is None:
                core = await maybe_phone_mac_act(
                    session,
                    device=device,
                    text=row.transcript,
                    idempotency_key=key,
                )
            spoken = str((core or {}).get("reply") or "").strip()
            if spoken:
                _stamp_takeover(row, spoken=spoken, route=str((core or {}).get("route") or ""), core=core)
                await session.flush()

    from app.everywhere.sync import emit_everywhere_event

    await emit_everywhere_event(
        session,
        event_type="conversation.turn",
        actor_label=f"device:{device.name}",
        content={
            "transcript": row.transcript,
            "session_id": row.session_id,
            "provider_item_id": row.provider_item_id,
            "kind": row.kind,
            "trusted_owner": trusted,
            "life_mutation": False,
            "ephemeral": False,
            "receipt_id": str(row.id),
        },
        device_id=str(device.id),
        privacy_level="normal",
    )
    return public_receipt(row, replayed=False)


def public_receipt(row: PhoneTurnReceipt, *, replayed: bool = False) -> dict[str, Any]:
    evidence = row.evidence if isinstance(row.evidence, dict) else {}
    payload: dict[str, Any] = {
        "ok": True,
        "receipt_id": str(row.id),
        "idempotency_key": row.idempotency_key,
        "durable": True,
        "ephemeral": False,
        "life_mutation": bool(row.life_mutation),
        "trusted_owner": bool(row.trusted_owner),
        "authority": False,
        "replayed": replayed,
        "kind": row.kind,
        "created_at": row.created_at.isoformat() if row.created_at else utcnow().isoformat(),
    }
    if evidence.get("core_takeover") and evidence.get("core_reply"):
        payload["core_takeover"] = True
        payload["core_reply"] = str(evidence["core_reply"])
        payload["core_route"] = str(evidence.get("core_route") or "")
    if isinstance(evidence.get("phone_action"), dict):
        payload["phone_action"] = {**evidence["phone_action"], **({"recovered": True} if replayed else {})}
    if isinstance(evidence.get("phone_actions"), list):
        payload["phone_actions"] = [
            {**action, **({"recovered": True} if replayed else {})}
            for action in evidence["phone_actions"] if isinstance(action, dict)
        ]
    if isinstance(evidence.get("phone_results"), list):
        payload["phone_results"] = evidence["phone_results"]
    if evidence.get("action_tool"):
        payload["action_tool"] = str(evidence["action_tool"])
        payload["action_status"] = str(evidence.get("action_status") or "")
        payload["action_accepted"] = bool(evidence.get("action_accepted"))
        payload["action_executed"] = bool(evidence.get("action_executed"))
        payload["action_verified"] = bool(evidence.get("action_verified"))
    return payload
