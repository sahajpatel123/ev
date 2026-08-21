"""Curated Realtime tool: phone_action. Never run_shortcut(name, arbitrary_input)."""

from __future__ import annotations

from typing import Any

from .registry import advertised_operations
from .service import create_phone_action
from .store import handshake_of

MOBILE_ACTION_CONTRACT = (
    "MOBILE ACTION CONTRACT: This iPhone acts through phone_action only. "
    "Never invent a run_shortcut function. Never invent phone numbers. "
    "Timers, reminders, calls, messages, and directions on this iPhone use "
    "phone_action, not Mac computer tools. Keep message recipient and body "
    "separate. For messages, the function asks for confirmation — wait; do not "
    "claim sent. If the function says requires_user_interaction, tell them to "
    "tap the card. If it says executed or created, you may confirm that. "
    "If it only opened system UI, say you opened it — not that the call connected "
    "or the message sent. If contacts are ambiguous, ask. If permission is "
    "required, say the iPhone needs that permission. Do not look up a home "
    "address from memory; ask where to go. Remote control of another iPhone "
    "is not available."
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
    result = create_phone_action(
        device_id=device_id,
        role=role,
        instance_id=instance_id,
        session_id=session_id,
        origin=origin,
        arguments=arguments if isinstance(arguments, dict) else {},
        transcript=transcript,
        device_label=device_label,
        confirm=bool((arguments or {}).get("confirm_action_id")),
    )
    card = result.get("card")
    if isinstance(card, dict):
        from .service import _push_live

        await _push_live(session_id, card)
    return result
