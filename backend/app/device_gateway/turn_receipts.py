"""Durable phone turn receipts. Clients report content; they cannot grant authority."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, PhoneTurnReceipt
from app.utils.text import utcnow

from .sandbox import is_sandbox_device

_PHONE_KERNEL_FALLBACK = (
    "I can't think that through right now. Give me a moment and ask again — "
    "I won't guess."
)


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
) -> None:
    """Spark 1.3 decides; Mini never gets a leftover conversational turn."""

    from app.cognitive.kernel import handle_turn

    from .pipeline import run_trusted_device_turn

    try:
        turn = await run_trusted_device_turn(
            session,
            device=device,
            text=row.transcript,
            idempotency_key=key,
        )
    except Exception:
        turn = {"conversational": True, "reply": None}
    if not isinstance(turn, dict):
        turn = {"conversational": True, "reply": None}

    spoken = ""
    route = str(turn.get("route") or "")
    phone_action = turn.get("phone_action") if isinstance(turn.get("phone_action"), dict) else None
    if not turn.get("conversational"):
        spoken = str(turn.get("reply") or "").strip()
    if not spoken:
        try:
            kernel = await handle_turn(
                transcript=row.transcript,
                live_session_id=row.session_id,
                device_id=str(device.id),
                modality="voice",
                actor=f"device:{device.name}",
            )
            spoken = str(getattr(kernel, "spoken", "") or "").strip()
            route = str(getattr(kernel, "kind", "") or route or "muse")[:80]
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
        core=turn,
        phone_action=phone_action,
    )
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
) -> dict[str, Any]:
    key = (idempotency_key or "").strip()[:128]
    if len(key) < 8:
        return {"ok": False, "error_code": "INVALID_IDEMPOTENCY_KEY", "authority": False}
    existing = (
        await session.execute(select(PhoneTurnReceipt).where(PhoneTurnReceipt.idempotency_key == key))
    ).scalar_one_or_none()
    if existing is not None:
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
        evidence=dict(evidence or {}),
        durable=True,
        life_mutation=False,
        trusted_owner=trusted,
    )
    session.add(row)
    await session.flush()

    if trusted and (row.kind or "") == "final_transcript" and (row.transcript or "").strip():
        from app.cognitive.mode import muse_kernel_active

        if muse_kernel_active():
            await _apply_muse_kernel_phone_turn(session, device=device, row=row, key=key)
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
        payload["phone_action"] = evidence["phone_action"]
    if evidence.get("action_tool"):
        payload["action_tool"] = str(evidence["action_tool"])
        payload["action_status"] = str(evidence.get("action_status") or "")
        payload["action_accepted"] = bool(evidence.get("action_accepted"))
        payload["action_executed"] = bool(evidence.get("action_executed"))
        payload["action_verified"] = bool(evidence.get("action_verified"))
    return payload
