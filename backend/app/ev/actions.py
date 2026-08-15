"""Formal action registry: declared capabilities with schemas and permissions."""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.gateway.validation import validate_arguments

LIFE_ACTION_NAMES = frozenset(
    {
        "send_message",
        "list_messages",
        "resolve_contact",
        "place_call",
        "list_mail",
        "open_url",
        "set_reminder",
    }
)

AUTONOMY_VALUES = ("full", "confirm_unknown", "confirm_all")


def autonomy_mode() -> str:
    """EV_OWNER_AUTONOMY, normalized; unknown values fail closed to ``full``."""

    mode = (settings.owner_autonomy or "full").strip().lower()
    return mode if mode in AUTONOMY_VALUES else "full"


def life_action_requires_approval(name: str) -> bool:
    """Per-action approval requirement under the current autonomy mode.

    ``full`` and ``confirm_unknown`` do not add a per-action approval step:
    the standing scopes / contact allowlist are enforced by the CONDUIT life
    policy inside the adapter. ``confirm_all`` requires explicit approval for
    every life action.
    """

    if name in LIFE_ACTION_NAMES:
        return autonomy_mode() == "confirm_all"
    spec = next((item for item in ACTION_SPECS if item["name"] == name), None)
    return bool(spec and spec.get("requires_approval", False))


def life_agency_prompt(name: str | None = None) -> str:
    """System-prompt block for life agency (injected when life tools are offered)."""

    from app.ev.assistant import spoken_name

    who = spoken_name(name)
    return (
        f"LIFE AGENCY. You are {who}, the owner's agent, and the owner has standing "
        "authority: when the owner tells you to do something and a granted life "
        "bridge exists, DO it.\n"
        "- Execute life actions (messages, calls, mail, contacts) through the "
        "granted bridges. Under EV_OWNER_AUTONOMY=full, no per-action approval "
        "is required inside granted scopes.\n"
        "- If a bridge is missing or a permission/scope is denied, explain "
        "exactly WHAT must be granted and WHICH helper is required (integration "
        "slug + scope, or helper command). Never invent a theatrical or moral "
        "refusal.\n"
        "- When an action succeeds, confirm briefly with evidence: "
        "recipient/target, channel, and time."
    )


ACTION_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_memory",
        "description": "Run a bounded personal-memory search (read-only model capability).",
        "payload": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                "memory_type": {"type": "string", "maxLength": 64, "default": None},
            },
        },
        "output": {"type": "object", "required": ["count", "results"]},
        "requires_approval": False,
        "undoable": False,
        "permission": "memory:read",
        "read_only": True,
    },
    {
        "name": "hud_card",
        "description": "Render a HUD quick card (ev.hud.card.v1) on the active surface.",
        "payload": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 120},
                "kind": {"type": "string", "maxLength": 64},
                "card": {"type": "string", "maxLength": 64},
                "item": {"type": "string", "maxLength": 128},
                "priority": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
        },
        "output": {"type": "object"},
        "requires_approval": False,
        "undoable": True,
        "permission": "hud:write",
        "read_only": False,
    },
    {
        "name": "present",
        "description": (
            "Open EVIE's native HUD windows on the owner's Mac to show something. "
            "Use this instead of telling the owner to open a web page. "
            "kind=auto lets intelligence pick size, time-type, and lookout."
        ),
        "payload": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "body": {"type": "string", "minLength": 1, "maxLength": 4000},
                "kind": {"type": "string", "maxLength": 32, "default": "auto"},
                "size": {"type": "string", "maxLength": 16},
                "time_type": {"type": "string", "maxLength": 16},
                "placement": {"type": "string", "maxLength": 16},
                "ttl_ms": {"type": "integer", "minimum": 0, "maximum": 3600000},
                "items": {"type": "array", "items": {"type": "string"}},
                "questions": {"type": "array", "items": {"type": "string"}},
                "response": {"type": "string", "maxLength": 4000},
                "layout": {"type": "string", "maxLength": 16},
                "recommendation": {"type": "string", "maxLength": 400},
                "source": {"type": "string", "maxLength": 160},
                "lookout": {"type": "boolean"},
                "window_id": {"type": "string", "maxLength": 64},
            },
            "required": ["title", "body"],
        },
        "output": {"type": "object", "required": ["opened"]},
        "requires_approval": False,
        "undoable": True,
        "permission": "hud:write",
        "read_only": False,
    },
    {
        "name": "notification",
        "description": "Deliver a notification to a registered device.",
        "payload": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "maxLength": 120},
                "text": {"type": "string", "maxLength": 2000},
                "body": {"type": "string", "maxLength": 2000},
                "priority": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["text"],
        },
        "output": {"type": "object"},
        "requires_approval": True,
        "undoable": False,
        "permission": "notify:send",
        "read_only": False,
    },
    {
        "name": "fleet_task",
        "description": "Dispatch a permissioned task to one or more registered devices.",
        "payload": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "task_type": {"type": "string", "minLength": 1, "maxLength": 64},
                "device_ids": {"type": "array", "items": {"type": "string"}},
                "params": {"type": "object"},
            },
            "required": ["task_type"],
        },
        "output": {"type": "object"},
        "requires_approval": True,
        "undoable": False,
        "permission": "fleet:dispatch",
        "read_only": False,
    },
    {
        "name": "web_search",
        "description": "Run a permissioned web search with citations (provider-gated).",
        "payload": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
        },
        "output": {"type": "object", "required": ["results"]},
        "requires_approval": True,
        "undoable": False,
        "permission": "web:search",
        "read_only": True,
    },
    {
        "name": "send_message",
        "description": "Send a message through a permissioned channel (chat, SMS, etc.).",
        "payload": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "channel": {"type": "string", "minLength": 1, "maxLength": 64},
                "to": {"type": "string", "maxLength": 256},
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            "required": ["channel"],
        },
        "output": {"type": "object"},
        "requires_approval": True,
        "undoable": False,
        "permission": "message:send",
        "read_only": False,
    },
    {
        "name": "execute_command",
        "description": "Execute a command in a sandboxed, permissioned environment.",
        "payload": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 4000},
                "cwd": {"type": "string", "maxLength": 512},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300, "default": 30},
            },
            "required": ["command"],
        },
        "output": {"type": "object"},
        "requires_approval": True,
        "undoable": False,
        "permission": "shell:execute",
        "read_only": False,
    },
]


def get_action_spec(name: str) -> dict | None:
    """Return the declared spec for an action name, or None when unknown."""
    for spec in ACTION_SPECS:
        if spec["name"] == name:
            resolved = dict(spec)
            resolved["requires_approval"] = life_action_requires_approval(name)
            return resolved
    return None


def validate_action_payload(name: str, payload: dict) -> list[str]:
    """Validate an action payload against its declared schema (no mutation)."""
    spec = get_action_spec(name)
    if spec is None:
        return [f"unknown action type '{name}'"]
    _effective, issues = validate_arguments(payload, spec["payload"])
    return issues


def list_action_specs() -> list[dict]:
    return [
        {**spec, "requires_approval": life_action_requires_approval(spec["name"])}
        for spec in ACTION_SPECS
    ]
