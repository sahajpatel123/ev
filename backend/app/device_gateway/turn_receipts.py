"""Durable phone turn receipts. Clients report content; they cannot grant authority."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cognitive.kernel import _UNAVAILABLE as _PHONE_KERNEL_FALLBACK
from app.models import Device, PhoneTurnReceipt
from app.utils.text import utcnow

from .cognitive_text import PhoneTextContext
from .sandbox import is_sandbox_device

logger = logging.getLogger("ev.device_gateway.turn_receipts")


def _error_code(source: dict[str, Any]) -> str:
    """Producers disagree on the name of the failure code; all three mean it."""
    return str(source.get("error_code") or source.get("error") or source.get("failure") or "").strip()


def _reports_failure(result: dict[str, Any]) -> bool:
    """True only when a result actually says the action did not work.

    ``ok`` is authoritative when a producer sets it; a shape that omits ``ok``
    is a failure only if it carries a failure code.
    """
    if result.get("ok") is False:
        return True
    if result.get("ok") is True:
        return False
    return bool(_error_code(result))


def _turn_action_source(
    core: dict[str, Any] | None, phone_action: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The result dict that decides this turn's action outcome.

    The deterministic lane reports every outcome it reaches — accepted, queued,
    failed — so it wins whenever it drove a tool. Kernel action cards already
    carry their own state to the HUD, so they only contribute here when they
    report a failure: a failed action must never be receipted as ok.
    """
    if isinstance(core, dict) and not core.get("conversational"):
        tool = str(core.get("tool") or core.get("operation") or "").strip()
        if tool and tool.upper() != "UNKNOWN":
            return core
    if isinstance(phone_action, dict) and _reports_failure(phone_action):
        return phone_action
    return None


def _validated_outcome(source: dict[str, Any]) -> dict[str, Any]:
    """Derive the persisted action outcome from a producer's result.

    The deterministic lane and Home Station dispatch fill these fields
    differently, so copying them turned a failed send into ``accepted: true``
    and a successful read into ``accepted: false``. The implications are
    restored instead: an executed action was accepted, a verified one was
    executed, and a failed one was never accepted or verified.
    """
    raw_ok = source.get("ok")
    ok = bool(raw_ok) if raw_ok is not None else not _reports_failure(source)
    accepted = bool(source.get("accepted", ok))
    executed = bool(source.get("executed"))
    verified = bool(source.get("verified")) and ok
    if verified:
        executed = True
    if executed:
        accepted = True
    if not ok:
        accepted = False
    status = str(source.get("status") or "").strip()
    return {
        "action_ok": ok,
        "action_status": (status or ("FAILED" if not ok else ""))[:32],
        "action_accepted": accepted,
        "action_executed": executed,
        "action_verified": verified,
        "action_queued": bool(source.get("queued")),
        "action_error_code": _error_code(source)[:64],
    }


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
    source = _turn_action_source(core, action)
    if source is not None:
        tool = str(source.get("tool") or source.get("operation") or "").strip()
        if tool and tool.upper() != "UNKNOWN":
            ev["action_tool"] = tool[:80]
        ev.update(_validated_outcome(source))
    row.evidence = ev


async def _phone_deterministic_lane(
    session: AsyncSession, *, device: Device, text: str, idempotency_key: str | None,
) -> dict[str, Any] | None:
    """Core reads and Home Station actions that do not need the mind.

    These are deterministic: they keep working when the model provider is down,
    rate-limited, or mid-incident. Keeping them reachable is what stops a
    provider outage from turning the owner's phone into a device that can only
    apologise — the owner still gets a real read or a real Mac action.
    """

    from .phone_core import maybe_phone_core_read
    from .phone_mac import maybe_phone_mac_act

    core = await maybe_phone_core_read(session, device=device, text=text)
    if core is None:
        core = await maybe_phone_mac_act(
            session, device=device, text=text, idempotency_key=idempotency_key,
        )
    return core


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
    kernel_error: str | None = None
    kernel_unavailable = False
    core: dict[str, Any] | None = None
    spoken = ""
    route = ""
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
        kernel_unavailable = bool(getattr(kernel, "unavailable", False))
        # A result object that predates or omits ``evidence`` must still produce
        # the spoken answer. Reading the attribute directly turned a partial
        # result into the generic unavailable line and hid the real cause.
        evidence_items = getattr(kernel, "evidence", None) or []
        phone_actions = [
            action for action in evidence_items
            if isinstance(action, dict) and isinstance(action.get("card"), dict)
        ]
        phone_results = [
            {
                name: value
                for name, value in result.items()
                if name in {
                    "ok", "error", "error_code", "failure", "spoken", "reply",
                    "action_id", "operation", "executed", "verified",
                }
            }
            for result in evidence_items
            if isinstance(result, dict)
        ]
    except Exception as exc:  # noqa: BLE001 - the phone still needs an answer
        # Never swallow this silently: a kernel failure used to become
        # "I can't think that through right now" with nothing recorded anywhere,
        # which made every phone-side outage undiagnosable.
        kernel_error = type(exc).__name__
        logger.exception(
            "phone turn kernel failed: device=%s transcript=%r",
            device.id,
            (row.transcript or "")[:120],
        )

    if kernel_error is not None or kernel_unavailable or not spoken:
        # The mind is unavailable. Fall back to the lane that does not need it,
        # so the owner still gets a real answer or a real action instead of a
        # dead end. This is the phone's safety net, not a second brain: it only
        # reads Core state or asks Home Station to run an existing tool.
        try:
            core = await _phone_deterministic_lane(
                session, device=device, text=row.transcript, idempotency_key=key,
            )
        except Exception:  # noqa: BLE001 - the fallback must not break the receipt
            logger.exception("phone deterministic lane failed: device=%s", device.id)
            core = None
        if core is not None and str(core.get("reply") or "").strip():
            spoken = str(core["reply"]).strip()
            route = str(core.get("route") or "HOME_STATION")
        elif not spoken:
            spoken = _PHONE_KERNEL_FALLBACK
            route = route or "unavailable"

    _stamp_takeover(
        row,
        spoken=spoken,
        route=route,
        core=core,
        phone_action=phone_actions[-1] if phone_actions else None,
    )
    if kernel_error is not None:
        row.evidence = {**row.evidence, "kernel_failed": True, "kernel_error": kernel_error}
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
        return await _replayed_receipt(session, device=device, row=existing)

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
        payload["phone_action"] = dict(evidence["phone_action"])
    if isinstance(evidence.get("phone_actions"), list):
        payload["phone_actions"] = [
            dict(action) for action in evidence["phone_actions"] if isinstance(action, dict)
        ]
    if isinstance(evidence.get("phone_results"), list):
        payload["phone_results"] = evidence["phone_results"]
    if evidence.get("action_tool"):
        payload["action_tool"] = str(evidence["action_tool"])
        payload["action_status"] = str(evidence.get("action_status") or "")
        payload["action_accepted"] = bool(evidence.get("action_accepted"))
        payload["action_executed"] = bool(evidence.get("action_executed"))
        payload["action_verified"] = bool(evidence.get("action_verified"))
        payload["action_queued"] = bool(evidence.get("action_queued"))
    if evidence.get("action_ok") is False:
        # The owner's action did not happen. Saying so at the top level is what
        # stops the client from rendering the failure as an accepted hand-off,
        # and the code is what lets it explain why.
        code = str(evidence.get("action_error_code") or "")
        payload["ok"] = False
        payload["action_accepted"] = False
        if code:
            payload["action_error_code"] = code
        payload["error_code"] = code or "ACTION_FAILED"
    # Surface a mind failure so a degraded turn is visible to the client and to
    # ops instead of looking like an ordinary refusal.
    if evidence.get("kernel_failed"):
        payload["kernel_failed"] = True
        payload["kernel_error"] = str(evidence.get("kernel_error") or "")
    return payload


async def _replayed_receipt(
    session: AsyncSession, *, device: Device, row: PhoneTurnReceipt,
) -> dict[str, Any]:
    """A replay is not evidence that an action was recovered.

    ``recovered`` is a claim about the action store, so it is set only where
    the store still holds that action for this phone. Every other field is the
    recorded state, unchanged.
    """
    from .durable_actions import load_action
    from .mobile_actions.store import get_action

    payload = public_receipt(row, replayed=True)

    async def confirmed(action: dict[str, Any]) -> dict[str, Any]:
        action_id = str(action.get("action_id") or "")
        if not action_id:
            return action
        stored = await load_action(session, action_id)
        if stored is None or str(stored.get("device_id") or "") != str(device.id):
            stored = get_action(action_id)
        if stored is None or str(stored.get("device_id") or "") != str(device.id):
            return action
        return {**action, "recovered": True}

    if isinstance(payload.get("phone_action"), dict):
        payload["phone_action"] = await confirmed(payload["phone_action"])
    if isinstance(payload.get("phone_actions"), list):
        payload["phone_actions"] = [
            await confirmed(action) for action in payload["phone_actions"] if isinstance(action, dict)
        ]
    return payload
