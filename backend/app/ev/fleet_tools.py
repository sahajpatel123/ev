"""Tool specs and handlers for house/lab/devices (items 11–25)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

TIMER_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "id": {"type": "string"},
        "timer_id": {"type": "string"},
        "fire_at": {"type": "string"},
        "status": {"type": "string"},
        "text": {"type": "string"},
        "spoken": {"type": "string"},
        "error": {"type": "string"},
        "evidence": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "timestamp": {"type": "string"},
                "accepted": {"type": "boolean"},
                "observed": {"type": "boolean"},
                "timer_id": {"type": "string"},
                "fire_at": {"type": "string"},
                "status": {"type": "string"},
            },
        },
        "idempotent_replay": {"type": "boolean"},
    },
}

FLEET_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "home_status",
        "description": "Read smart-home entity state, optionally by area.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"area": {"type": "string", "maxLength": 128, "default": None}},
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "home:read",
        "undoable": False,
        "risk_class": "R0",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
    },
    {
        "name": "home_act",
        "description": (
            "Change an owner home entity. This pass completes light control "
            "with accepted and observed evidence. Locks and covers stay on "
            "the existing path and are not expanded."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "entity": {"type": "string", "minLength": 1, "maxLength": 200},
                "action": {"type": "string", "minLength": 1, "maxLength": 64},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["entity", "action"],
        },
        "output": {"type": "object"},
        "sensitive": True,
        "read_only": False,
        "permission": "home:act",
        "undoable": True,
        "risk_class": "R3",
        "confirmation": "fresh",
        "target_ownership": "owner",
        "provider": "smart_home",
        "evidence": ["source", "timestamp", "accepted_state", "observed_state"],
        "idempotency": "natural",
        "cancellation": "timeout",
        "required_scopes": ["home:act"],
    },
    {
        "name": "actuate",
        "description": "Allowlisted onboard actuator: volume, lookout, HUD, home, drone.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "verb": {"type": "string", "minLength": 1, "maxLength": 64},
                "args": {"type": "object", "default": None},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["verb"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "actuator:software",
        "undoable": True,
    },
    {
        "name": "start_timer",
        "description": (
            "Start a durable timer that rings and shows a HUD when it fires. "
            "For a one-minute timer pass minutes=1. Do not narrate a timer "
            "instead of calling this."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "minutes": {"type": "number", "minimum": 0, "default": None},
                "at": {"type": "string", "maxLength": 64, "default": None},
                "text": {"type": "string", "maxLength": 500, "default": None},
                "idempotency_key": {"type": "string", "maxLength": 128, "default": None},
            },
        },
        "output": TIMER_OUTPUT_SCHEMA,
        "sensitive": False,
        "read_only": False,
        "permission": "assistant:profile",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
        "evidence": ["source", "timestamp"],
        "idempotency": "key",
        "cancellation": "cancel_timer",
    },
    {
        "name": "cancel_timer",
        "description": "Cancel a pending owner timer by id or matching text.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "maxLength": 64, "default": None},
                "text": {"type": "string", "maxLength": 500, "default": None},
            },
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "assistant:profile",
        "undoable": False,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
    },
    {
        "name": "list_timers",
        "description": "List pending owner timers.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "assistant:profile",
        "undoable": False,
        "risk_class": "R0",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
    },
    {
        "name": "snooze_timer",
        "description": "Delay a pending timer or restart a fired one.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "maxLength": 64, "default": None},
                "text": {"type": "string", "maxLength": 500, "default": None},
                "minutes": {"type": "number", "minimum": 0.5, "default": 5},
            },
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "assistant:profile",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
        "evidence": ["source", "timestamp"],
        "idempotency": "key",
        "cancellation": "cancel_timer",
    },
    {
        "name": "session_elapsed",
        "description": "How long the current voice session has been open.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "assistant:profile",
        "undoable": False,
    },
    {
        "name": "indoor_route",
        "description": "Walk an owner-authored indoor map to a room.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"to_room": {"type": "string", "minLength": 1, "maxLength": 128}},
            "required": ["to_room"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "assistant:profile",
        "undoable": False,
    },
    {
        "name": "calendar_add",
        "description": "Create a calendar event after write-scope re-consent.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 256},
                "start": {"type": "string", "minLength": 1, "maxLength": 64},
                "end": {"type": "string", "minLength": 1, "maxLength": 64},
                "location": {"type": "string", "maxLength": 512, "default": None},
                "confirm": {"type": "boolean", "default": False},
                "idempotency_key": {"type": "string", "maxLength": 128, "default": None},
            },
            "required": ["title", "start", "end"],
        },
        "output": {"type": "object"},
        "sensitive": None,
        "read_only": False,
        "permission": "calendar:write",
        "undoable": True,
        "risk_class": "R2",
        "confirmation": "standing",
        "target_ownership": "owner",
        "provider": "calendar",
        "evidence": ["source", "timestamp"],
        "idempotency": "key",
        "cancellation": "timeout",
        "required_scopes": ["calendar:write"],
    },
    {
        "name": "calendar_read",
        "description": "Read the owner's calendar signals (next event, leave-by). Never writes.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
        "output": {"type": "object", "required": ["ok", "spoken"]},
        "sensitive": False,
        "read_only": True,
        "permission": "calendar:read",
        "undoable": False,
        "risk_class": "R0",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "calendar",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
        "required_scopes": ["calendar:read"],
    },

    {
        "name": "ticket_hold",
        "description": "Draft a ticket search URL and optional calendar hold. Never buys.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 256},
                "title": {"type": "string", "maxLength": 256, "default": None},
                "start": {"type": "string", "maxLength": 64, "default": None},
                "end": {"type": "string", "maxLength": 64, "default": None},
                "price": {"type": "string", "maxLength": 32, "default": None},
            },
            "required": ["query"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "web:search",
        "undoable": True,
    },
    {
        "name": "ticket_buy",
        "description": "Never silent-buy. Requires explicit confirm and a payment token.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 256},
                "confirm": {"type": "boolean", "default": False},
                "payment_token": {"type": "string", "maxLength": 256, "default": None},
            },
            "required": ["query"],
        },
        "output": {"type": "object"},
        "sensitive": True,
        "read_only": False,
        "permission": "ticket:buy",
        "undoable": False,
        "risk_class": "R4",
        "confirmation": "fresh",
        "target_ownership": "owner",
        "provider": "tickets",
        "evidence": ["source", "timestamp"],
        "idempotency": "key",
        "cancellation": "required",
    },
    {
        "name": "gear_explain",
        "description": "List modes a piece of gear understands.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"device": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["device"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "gear:read",
        "undoable": False,
    },
    {
        "name": "gear_set_mode",
        "description": "Switch a device into a named mode. Unknown modes refuse with the list.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "device": {"type": "string", "minLength": 1, "maxLength": 200},
                "mode": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "required": ["device", "mode"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "gear:write",
        "undoable": True,
    },
    {
        "name": "list_empties",
        "description": "List BOM and battery items below their thresholds.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "gear:read",
        "undoable": False,
    },
    {
        "name": "delegate_grant",
        "description": "Time-box calendar/research/briefing read for a person. Never a second owner.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 200},
                "scopes": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 64},
                    "maxItems": 8,
                },
                "not_after": {"type": "string", "maxLength": 64, "default": None},
                "device_id": {"type": "string", "maxLength": 64, "default": None},
            },
            "required": ["name", "scopes"],
        },
        "output": {"type": "object"},
        "sensitive": True,
        "read_only": False,
        "permission": "owner:delegate",
        "undoable": True,
    },
    {
        "name": "delegate_revoke",
        "description": "Revoke a person's boxed share.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["name"],
        },
        "output": {"type": "object"},
        "sensitive": True,
        "read_only": False,
        "permission": "owner:delegate",
        "undoable": False,
    },
    {
        "name": "list_locked",
        "description": "What Training Wheels and refused protocols still lock.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "assistant:profile",
        "undoable": False,
    },
    {
        "name": "lock_everything",
        "description": "Panic the fleet from a still-trusted device or the master key.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"confirm": {"type": "boolean", "default": False}},
        },
        "output": {"type": "object"},
        "sensitive": True,
        "read_only": False,
        "permission": "owner:panic",
        "undoable": False,
    },
    {
        "name": "whereabouts",
        "description": "Where a person was last remembered. Never mixed with live share.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["name"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
]


FLEET_TOOL_NAMES = {spec["name"] for spec in FLEET_TOOL_SPECS}


def actuate_permission(verb: str) -> str:
    lowered = (verb or "").strip().lower()
    if lowered == "drone.cmd":
        return "actuator:drone"
    if lowered == "home_act":
        return "home:act"
    return "actuator:software"


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def handle_fleet_tool(
    session: AsyncSession,
    name: str,
    args: dict,
    *,
    actor: str,
) -> dict | None:
    if name == "place_call":
        from app.ev.voice_life import place_call

        return await place_call(session, args, actor=actor)
    if name == "home_status":
        from app.ev.home import home_status

        return await home_status(session, area=args.get("area"))
    if name == "home_act":
        from app.ev.home import home_act

        return await home_act(
            session,
            str(args["entity"]),
            str(args["action"]),
            confirm=bool(args.get("confirm")),
            actor=actor,
        )
    if name == "actuate":
        from app.ev.voice_life import actuate

        return await actuate(
            session,
            str(args["verb"]),
            args.get("args") or {},
            confirm=bool(args.get("confirm")),
            actor=actor,
        )
    if name == "start_timer":
        from app.ev.timers import start_timer

        return await start_timer(
            session,
            minutes=_optional_float(args.get("minutes")),
            at=args.get("at"),
            text=str(args.get("text") or ""),
            actor=actor,
            idempotency_key=args.get("idempotency_key"),
        )
    if name == "cancel_timer":
        from app.ev.timers import cancel_timer

        return await cancel_timer(
            session,
            timer_id=args.get("id"),
            text=args.get("text"),
            actor=actor,
        )
    if name == "list_timers":
        from app.ev.timers import list_timers

        return await list_timers(session)
    if name == "snooze_timer":
        from app.ev.timers import snooze_timer

        return await snooze_timer(
            session,
            timer_id=args.get("id"),
            text=args.get("text"),
            minutes=float(args.get("minutes") or 5),
            actor=actor,
        )
    if name == "session_elapsed":
        from app.ev.timers import session_elapsed

        return await session_elapsed(session)
    if name == "indoor_route":
        from app.ev.travel import indoor_route

        return await indoor_route(session, str(args["to_room"]))
    if name == "calendar_add":
        from app.ev.calendar_write import calendar_add

        return await calendar_add(
            session,
            title=str(args["title"]),
            start=str(args["start"]),
            end=str(args["end"]),
            location=args.get("location"),
            confirm=bool(args.get("confirm")),
            actor=actor,
            idempotency_key=args.get("idempotency_key"),
        )
    if name == "calendar_read":
        return await _calendar_read(session, limit=int(args.get("limit") or 20))
    if name == "ticket_hold":
        from app.ev.calendar_write import ticket_hold

        return await ticket_hold(
            session,
            query=str(args["query"]),
            title=args.get("title"),
            start=args.get("start"),
            end=args.get("end"),
            price=args.get("price"),
            actor=actor,
        )
    if name == "ticket_buy":
        from app.ev.calendar_write import ticket_buy

        return await ticket_buy(
            session,
            query=str(args["query"]),
            confirm=bool(args.get("confirm")),
            payment_token=args.get("payment_token"),
            actor=actor,
        )
    if name == "gear_explain":
        from app.ev.workshop import gear_explain

        return await gear_explain(session, str(args["device"]))
    if name == "gear_set_mode":
        from app.ev.workshop import gear_set_mode

        return await gear_set_mode(session, str(args["device"]), str(args["mode"]))
    if name == "list_empties":
        from app.ev.workshop import list_empties

        return await list_empties(session)
    if name == "delegate_grant":
        from app.ev.delegates import grant

        return await grant(
            session,
            name=str(args["name"]),
            scopes=list(args.get("scopes") or []),
            not_after=args.get("not_after"),
            device_id=args.get("device_id"),
            actor=actor,
        )
    if name == "delegate_revoke":
        from app.ev.delegates import revoke

        return await revoke(session, name=str(args["name"]), actor=actor)
    if name == "list_locked":
        from app.ev.training_wheels import list_locked

        return await list_locked(session)
    if name == "lock_everything":
        from app.ev.fleet import lock_all

        trusted = actor == "master" or actor.startswith("device:")
        return await lock_all(session, actor=actor, trusted=trusted)
    if name == "whereabouts":
        from app.ev.travel import whereabouts_honest

        return await whereabouts_honest(session, str(args["name"]))
    return None


async def _calendar_read(session: AsyncSession, *, limit: int = 20) -> dict:
    """Owner calendar signals from the existing calendar feed. Honest if missing."""

    from sqlalchemy import select

    from app.ev import calendar as calendar_feed
    from app.models import Integration
    from app.utils.text import utcnow

    integration = (
        await session.execute(
            select(Integration).where(
                Integration.adapter == "calendar",
                Integration.status == "active",
            ).limit(1)
        )
    ).scalars().first()
    if integration is None:
        return {
            "ok": False,
            "error": "not_connected",
            "degraded": True,
            "provider": "calendar",
            "spoken": "Calendar is not connected.",
            "next_step": (
                "install the calendar integration and grant scope 'calendar:read' "
                "(POST /v1/integrations with adapter=calendar)"
            ),
        }
    signals = await calendar_feed.calendar_signals(session, limit=min(limit, 500))
    next_event = signals.get("next_event") or {}
    summary = str(next_event.get("summary") or "").strip()
    spoken = f"Next: {summary}." if summary else "No upcoming calendar events."
    raw_source = signals.get("source")
    source = raw_source if isinstance(raw_source, dict) else {}
    integration_config = integration.config if isinstance(integration.config, dict) else {}
    return {
        "ok": True,
        "next_event": next_event or None,
        "leave_by": signals.get("leave_by"),
        "today": signals.get("today"),
        "signals": {
            "next_event": next_event or None,
            "leave_by": signals.get("leave_by"),
            "today": signals.get("today"),
        },
        "spoken": spoken,
        "evidence": {
            "source": source.get("kind") or "calendar",
            "timestamp": utcnow().isoformat(),
            "event_ids": source.get("event_ids") or [],
            "provider": integration_config.get("provider"),
        },
    }
