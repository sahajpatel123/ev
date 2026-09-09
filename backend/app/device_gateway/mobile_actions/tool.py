"""Curated Realtime tool: phone_action. Never run_shortcut(name, arbitrary_input)."""

from __future__ import annotations

from typing import Any

from .engine import create_phone_action
from .registry import advertised_operations
from .store import handshake_of

MOBILE_ACTION_CONTRACT = (
    "MOBILE ACTION CONTRACT: Safari Evie cannot run iPhone Clock or Reminders. "
    "Timers, reminders, opening Calculator/Safari/Spotify, mail, calendar, and "
    "Mac apps run on Home Station. Prefer evie_home_action or evie_state_query "
    "with their exact words; Home Station executes the real Mac tool. "
    "phone_action is only for a native iPhone broker (maps URLs, tel/sms when "
    "a number is given). Never invent a run_shortcut function. Never invent "
    "phone numbers. Never tell them to open a separate iPhone app for a timer "
    "or reminder — Home Station does it. Keep message recipient and body separate. "
    "If awaiting confirmation, wait for yes/no. Never claim a call connected or "
    "a message sent unless the tool result says so. Remote control of another "
    "iPhone is not available."
)

PHONE_ACTION_DESCRIPTION = (
    "Act on this iPhone through approved local capabilities: timer, reminder, "
    "alarm, call or FaceTime a contact, message a contact, open Maps or start "
    "directions, calendar, share, copy, and (if available) Focus or media. "
    "Use contact_query for names like Mom — do not invent numbers. "
    "Put the exact message text in message. Put durations in duration_seconds "
    "or duration_minutes. Put places in destination. Target this_phone unless "
    "the owner named the other phone, which v1 cannot wake remotely. "
    "Never use this for payments, passwords, deletions, or Wi-Fi."
)


def phone_action_parameters(device: Any | None = None) -> dict[str, Any]:
    device_id = str(getattr(device, "id", "") or "")
    handshake = handshake_of(device_id) if device_id else {}
    operations = list(advertised_operations(handshake=handshake))
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "operation": {"type": "string", "enum": operations},
            "target_device": {
                "type": "string",
                "enum": ["this_phone", "primary", "secondary"],
            },
            "contact_query": {"type": "string", "maxLength": 80},
            "message": {"type": "string", "maxLength": 500},
            "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
            "duration_minutes": {"type": "number", "minimum": 0.25, "maximum": 1440},
            "title": {"type": "string", "maxLength": 200},
            "when_iso": {"type": "string", "maxLength": 40},
            "destination": {"type": "string", "maxLength": 200},
            "phone_number": {"type": "string", "maxLength": 32},
            "focus": {"type": "string", "maxLength": 40},
            "text": {"type": "string", "maxLength": 4000},
            "media_action": {
                "type": "string",
                "enum": ["play", "pause", "next", "previous"],
            },
            "confirm_action_id": {"type": "string", "maxLength": 80},
            "list": {"type": "string", "maxLength": 80},
            "location": {"type": "string", "maxLength": 120},
            "app_id": {"type": "string", "maxLength": 80},
        },
        "required": ["operation"],
    }


def phone_action_function_spec(device: Any | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "name": "phone_action",
        "description": PHONE_ACTION_DESCRIPTION,
        "parameters": phone_action_parameters(device),
    }


def phone_action_tool_spec() -> dict[str, Any]:
    """Registry-shaped spec for get_spec / POL-adjacent catalogs. Not Mac-exposed."""

    return {
        "name": "phone_action",
        "description": PHONE_ACTION_DESCRIPTION,
        "parameters": phone_action_parameters(),
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "phone:act",
        "undoable": True,
        "risk_class": "R2",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "phone",
        "evidence": ["receipt"],
        "idempotency": "key",
        "cancellation": "timeout",
        "required_scopes": ["phone:act"],
    }


async def _home_station_phone_action(
    *,
    device_id: str,
    arguments: dict[str, Any],
    transcript: str,
) -> dict[str, Any] | None:
    """Safari PWA has no native Clock; run the same job on Home Station."""

    from uuid import UUID

    from app.db import SessionLocal
    from app.device_gateway.phone_mac import maybe_phone_mac_act, utterance_from_phone_action
    from app.models import Device

    text = utterance_from_phone_action(arguments, transcript)
    if not text:
        return None
    try:
        did = UUID(str(device_id))
    except (TypeError, ValueError):
        return None
    async with SessionLocal() as db:
        device = await db.get(Device, did)
        if device is None or device.revoked_at is not None:
            return None
        acted = await maybe_phone_mac_act(db, device=device, text=text)
        if acted is None:
            return None
        await db.commit()
    return {
        "ok": True,
        "accepted": True,
        "executed": bool(acted.get("executed")),
        "verified": bool(acted.get("verified")),
        "operation": str(acted.get("tool") or arguments.get("operation") or "home_station"),
        "spoken": acted.get("reply"),
        "home_station": True,
        "route": acted.get("route"),
        "requires_user_interaction": False,
        "method": "home_station",
        "must_continue": True,
        "completion_claim_allowed": False,
    }


async def dispatch_phone_action(
    *,
    device_id: str,
    role: str,
    instance_id: str,
    session_id: str | None,
    origin: str,
    arguments: dict[str, Any],
    transcript: str = "",
    device_label: str = "This iPhone",
) -> dict[str, Any]:
    from .engine import apply_confirmation_utterance
    from .trust import classify_utterance

    args = arguments if isinstance(arguments, dict) else {}
    pending = apply_confirmation_utterance(
        device_id=device_id,
        origin=origin,
        text=transcript,
        session_id=session_id,
    )
    kind = classify_utterance(transcript)
    if pending is not None and kind != "unrelated":
        result = pending
    else:
        result = create_phone_action(
            device_id=device_id,
            role=role,
            instance_id=instance_id,
            session_id=session_id,
            origin=origin,
            arguments=args,
            transcript=transcript,
            device_label=device_label,
            confirm=bool(args.get("confirm_action_id")),
        )
        if (
            str(result.get("failure") or "") == "NATIVE_SHELL_REQUIRED"
            or str(result.get("method") or "") == "pwa_local"
        ):
            home = await _home_station_phone_action(
                device_id=device_id,
                arguments=args,
                transcript=transcript,
            )
            if home is not None:
                if result.get("ok") and result.get("card"):
                    spoken = str(home.get("spoken") or "").rstrip()
                    extra = (
                        " Tap Start timer on this iPhone for a local alert — Evie's timer, not Clock."
                        if str(args.get("operation") or "") == "create_timer"
                        else " Tap Save reminder on this iPhone for a local alert — not Reminders.app."
                        if str(args.get("operation") or "") == "create_reminder"
                        else ""
                    )
                    if extra and "local alert" not in spoken.lower():
                        home["spoken"] = (spoken + extra).strip()
                    home["phone_action"] = result
                    home["card"] = result.get("card")
                    home["method"] = result.get("method") or home.get("method")
                result = home
        action_id = str(result.get("action_id") or "")
        if action_id:
            from app.db import SessionLocal
            from app.device_gateway.durable_actions import upsert_action

            from .store import get_action

            row = get_action(action_id)
            if row:
                async with SessionLocal() as db:
                    await upsert_action(db, row)
                    await db.commit()
    card = result.get("card")
    if isinstance(card, dict):
        from .service import _push_live

        await _push_live(session_id, card)
    return result
