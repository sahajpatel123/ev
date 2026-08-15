"""Tool specs and handlers for house/lab/devices (items 11–25)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

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
    },
    {
        "name": "home_act",
        "description": "Change a home entity (light, lock, cover). Locks need confirm.",
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
        "description": "Start a durable timer that speaks when it fires.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "minutes": {"type": "number", "minimum": 0, "default": None},
                "at": {"type": "string", "maxLength": 64, "default": None},
                "text": {"type": "string", "maxLength": 500, "default": None},
            },
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "assistant:profile",
        "undoable": True,
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
            },
            "required": ["title", "start", "end"],
        },
        "output": {"type": "object"},
        "sensitive": None,
        "read_only": False,
        "permission": "calendar:write",
        "undoable": True,
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
            minutes=args.get("minutes"),
            at=args.get("at"),
            text=str(args.get("text") or ""),
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
        )
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
