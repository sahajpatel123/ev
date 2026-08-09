"""Formal action registry: declared capabilities with schemas and permissions."""

from __future__ import annotations

from app.gateway.validation import validate_arguments


ACTION_SPECS = [
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
            return spec
    return None


def validate_action_payload(name: str, payload: dict) -> list[str]:
    """Validate an action payload against its declared schema (no mutation)."""
    spec = get_action_spec(name)
    if spec is None:
        return [f"unknown action type '{name}'"]
    _effective, issues = validate_arguments(payload, spec["payload"])
    return issues


def list_action_specs() -> list[dict]:
    return list(ACTION_SPECS)
