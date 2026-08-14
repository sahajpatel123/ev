"""Tool orchestration: registry + declarative dispatcher for EV's memory tools.

Every tool is a formally declared capability with an explicit input schema,
output shape, permission scope, read-only boundary, and undoability marker.
The dispatcher validates arguments and output shape, enforces the permission
matrix (sensitive tools need an explicit gate), and logs every invocation to
the access log so each call is authorized, auditable, and traceable.
"""

from __future__ import annotations

import ast
import math
import operator
import time
from collections.abc import Callable
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import health_radar, maker, people
from app.ev.actions import LIFE_ACTION_NAMES, autonomy_mode
from app.ev.research import list_sessions
from app.gateway.validation import validate_arguments, validate_output
from app.integrations import service as integrations
from app.integrations.life_helper import LifeHelperError, LifeHelperUnavailableError
from app.memory.retrieval import Retriever
from app.models import GearSnapshot, Integration, Memory
from app.schemas import ToolCallResponse
from app.search.providers import get_search_provider
from app.services.access_log import log_access

MAX_EXPRESSION_LENGTH = 200
MAX_EXPONENT = 100
MAX_RESULT_ABS = 1e12


def safe_calculate(expression: str) -> float:
    """Evaluate simple arithmetic with a whitelisted AST (no eval/exec)."""
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ValueError("Expression too long")
    allowed_nodes = (
        ast.Expression,
        ast.Constant,
        ast.BinOp,
        ast.UnaryOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Pow,
        ast.Mod,
        ast.USub,
        ast.UAdd,
    )
    tree = ast.parse(expression, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise ValueError("Unsupported expression")

    ops: dict[type, Callable[..., float]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.Mod: operator.mod,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def checked(value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Result is not finite")
        if abs(value) > MAX_RESULT_ABS:
            raise ValueError("Result out of range")
        return float(value)

    def evaluate(node: ast.AST) -> float:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("Only numbers are allowed")
            return float(node.value)
        if isinstance(node, ast.BinOp):
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, (ast.Div, ast.Mod)) and right == 0:
                raise ValueError("Division by zero")
            if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
                raise ValueError("Exponent too large")
            op = cast(Callable[[float, float], float], ops[type(node.op)])
            return checked(float(op(left, right)))
        if isinstance(node, ast.UnaryOp):
            unary_op = cast(Callable[[float], float], ops[type(node.op)])
            return checked(float(unary_op(evaluate(node.operand))))
        raise ValueError("Unsupported expression")

    return checked(evaluate(tree.body))


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_memory",
        "description": "Search EV's personal memory (facts, decisions, goals, preferences).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                "memory_type": {
                    "type": "string",
                    "enum": [
                        "decision",
                        "goal",
                        "preference",
                        "fact",
                        "observation",
                        "episodic",
                        "pattern",
                        "summary",
                        "lesson",
                    ],
                    "default": None,
                },
            },
            "required": ["query"],
        },
        "output": {"type": "object", "required": ["count", "results"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "search_decisions",
        "description": "Search past decisions (current and historical decision memory).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            },
            "required": ["query"],
        },
        "output": {"type": "object", "required": ["count", "results"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "search_timeline",
        "description": "Search the raw event timeline.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            },
            "required": ["query"],
        },
        "output": {"type": "object", "required": ["count", "results"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "get_person",
        "description": "Find where a person appears in the user's memory.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["name"],
        },
        "output": {"type": "object", "required": ["name", "total_events"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "get_project",
        "description": "Get a maker project with BOM and print queue.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["name"],
        },
        "output": {"type": "object", "required": ["project", "bom", "print_jobs"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "get_goals",
        "description": "List current goals.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object", "required": ["count", "goals"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "get_patterns",
        "description": "List detected behavior patterns, optionally by topic.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"topic": {"type": "string", "maxLength": 200, "default": None}},
        },
        "output": {"type": "object", "required": ["count", "patterns"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "calculate",
        "description": "Safe arithmetic calculation.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "expression": {"type": "string", "minLength": 1, "maxLength": MAX_EXPRESSION_LENGTH}
            },
            "required": ["expression"],
        },
        "output": {"type": "object", "required": ["expression", "result"]},
        "sensitive": False,
        "read_only": True,
        "permission": "compute:safe",
        "undoable": False,
    },
    {
        "name": "get_health_trends",
        "description": "Get health metric trends (sleep_hours, hrv_ms, resting_hr, steps, mood).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": ["sleep_hours", "hrv_ms", "resting_hr", "steps", "mood"],
                },
                "window_days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 14},
            },
            "required": ["metric"],
        },
        "output": {"type": "object", "required": ["metric"]},
        "sensitive": True,
        "read_only": True,
        "permission": "health:read",
        "undoable": False,
    },
    {
        "name": "get_gear_status",
        "description": "Latest gear telemetry for a device.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"device_id": {"type": "string", "maxLength": 200, "default": None}},
        },
        "output": {"type": "object", "required": ["found"]},
        "sensitive": False,
        "read_only": True,
        "permission": "gear:read",
        "undoable": False,
    },
    {
        "name": "get_upcoming_alerts",
        "description": "List pending EV alerts.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
        },
        "output": {"type": "object", "required": ["count", "alerts"]},
        "sensitive": False,
        "read_only": True,
        "permission": "alerts:read",
        "undoable": False,
    },
    {
        "name": "get_research",
        "description": "List research sessions, optionally open only.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": ["open", "concluded"], "default": None},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
        },
        "output": {"type": "object", "required": ["count", "sessions"]},
        "sensitive": False,
        "read_only": True,
        "permission": "research:read",
        "undoable": False,
    },
    {
        "name": "search_web",
        "description": (
            "Search the web for current external information. Every result carries "
            "a citation (title, url, snippet); never present uncited web content as memory."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["query"],
        },
        "output": {"type": "object", "required": ["count", "results"]},
        "sensitive": False,
        "read_only": True,
        "permission": "web:search",
        "undoable": False,
    },
    {
        "name": "get_weather",
        "description": (
            "Live weather and a 3-day forecast via Open-Meteo (no API key). "
            "Use for current conditions, rain, temperature, or forecast. "
            "Omit place to use the owner's coarse location."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "place": {"type": "string", "minLength": 1, "maxLength": 80},
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
            },
        },
        "output": {"type": "object", "required": ["ok", "count", "results"]},
        "sensitive": False,
        "read_only": True,
        "permission": "web:search",
        "undoable": False,
    },
    {
        "name": "resolve_contact",
        "description": (
            "Resolve a contact name (e.g. 'Mom') to a real contact record through "
            "the granted contacts bridge. Use this before send_message/place_call "
            "when the target is a person's name."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            },
            "required": ["name"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "contacts:read",
        "undoable": False,
    },
    {
        "name": "send_message",
        "description": (
            "Send a message to a contact or phone number through the granted "
            "messaging bridge (e.g. Messages). Under EV_OWNER_AUTONOMY=full this "
            "needs no approval inside granted scopes."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "to": {"type": "string", "minLength": 1, "maxLength": 256},
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                "channel": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "default": "messages",
                },
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["to", "text"],
        },
        "output": {"type": "object"},
        "sensitive": None,  # resolved by EV_OWNER_AUTONOMY
        "read_only": False,
        "permission": "message:send",
        "undoable": False,
    },
    {
        "name": "list_messages",
        "description": "List recent messages from the granted messaging bridge.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "channel": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "default": "messages",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "message:read",
        "undoable": False,
    },
    {
        "name": "place_call",
        "description": (
            "Place a phone call (or FaceTime when video=true) to a contact through "
            "the granted phone bridge."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "to": {"type": "string", "minLength": 1, "maxLength": 256},
                "video": {"type": "boolean", "default": False},
                "confirm": {"type": "boolean", "default": False},
            },
            "required": ["to"],
        },
        "output": {"type": "object"},
        "sensitive": None,  # resolved by EV_OWNER_AUTONOMY
        "read_only": False,
        "permission": "phone:act",
        "undoable": False,
    },
    {
        "name": "list_mail",
        "description": "List recent mail from the granted mail bridge.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
            },
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "mail:read",
        "undoable": False,
    },
    {
        "name": "open_url",
        "description": "Open a URL on the owner's default browser/device.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "url": {"type": "string", "minLength": 1, "maxLength": 2048}
            },
            "required": ["url"],
        },
        "output": {"type": "object"},
        "sensitive": None,  # resolved by EV_OWNER_AUTONOMY
        "read_only": False,
        "permission": "life:open_url",
        "undoable": False,
    },
    {
        "name": "set_reminder",
        "description": "Create a reminder for the owner (available when a reminders bridge exists).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": 2000},
                "when": {"type": "string", "maxLength": 128, "default": None},
            },
            "required": ["text"],
        },
        "output": {"type": "object"},
        "sensitive": None,  # resolved by EV_OWNER_AUTONOMY
        "read_only": False,
        "permission": "life:reminder",
        "undoable": False,
    },
    {
        "name": "present",
        "description": (
            "Open EVIE's native HUD windows on the owner's Mac. Use this "
            "instead of telling the owner to open a website. kind=auto lets "
            "surface intelligence pick size, time-type, and lookout placement. "
            "Kinds: card, briefing, list, conversation, map, chip, radar, "
            "vitals, horizon, scope, bench, trace, pulse, ticker, wire."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 120},
                "body": {"type": "string", "minLength": 1, "maxLength": 4000},
                "kind": {
                    "type": "string",
                    "maxLength": 32,
                    "default": "auto",
                },
                "size": {"type": "string", "maxLength": 16, "default": None},
                "time_type": {"type": "string", "maxLength": 16, "default": None},
                "placement": {"type": "string", "maxLength": 16, "default": None},
                "ttl_ms": {"type": "integer", "minimum": 0, "maximum": 3600000, "default": None},
                "items": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 240},
                    "maxItems": 12,
                    "default": None,
                },
                "recommendation": {"type": "string", "maxLength": 400, "default": None},
                "source": {"type": "string", "maxLength": 160, "default": None},
                "lookout": {"type": "boolean", "default": None},
                "window_id": {"type": "string", "maxLength": 64, "default": None},
            },
            "required": ["title", "body"],
        },
        "output": {"type": "object", "required": ["opened"]},
        "sensitive": False,
        "read_only": False,
        "permission": "hud:write",
        "undoable": True,
    },
    {
        "name": "set_assistant_name",
        "description": "Set the spoken nickname the assistant answers to.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 40}},
            "required": ["name"],
        },
        "output": {"type": "object", "required": ["ok", "name"]},
        "sensitive": False,
        "read_only": False,
        "permission": "assistant:profile",
        "undoable": True,
    },
    {
        "name": "reset_assistant_name",
        "description": "Reset the spoken nickname to EVIE.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object", "required": ["ok", "name"]},
        "sensitive": False,
        "read_only": False,
        "permission": "assistant:profile",
        "undoable": True,
    },
    {
        "name": "update_personality",
        "description": "Update personality sliders (humor, formality, verbosity, etc.).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "humor": {"type": "integer", "minimum": 0, "maximum": 5, "default": None},
                "formality": {"type": "integer", "minimum": 1, "maximum": 5, "default": None},
                "verbosity": {"type": "integer", "minimum": 1, "maximum": 5, "default": None},
                "directness": {"type": "integer", "minimum": 1, "maximum": 5, "default": None},
                "reason_for_change": {"type": "string", "maxLength": 200, "default": None},
            },
        },
        "output": {"type": "object", "required": ["ok", "profile"]},
        "sensitive": False,
        "read_only": False,
        "permission": "assistant:profile",
        "undoable": True,
    },
    {
        "name": "list_protocols",
        "description": "List unlocked and refused protocols.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "filter": {
                    "type": "string",
                    "enum": ["all", "enabled", "needs_setup", "locked", "refused"],
                    "default": "all",
                }
            },
        },
        "output": {"type": "object", "required": ["protocols"]},
        "sensitive": False,
        "read_only": True,
        "permission": "assistant:profile",
        "undoable": False,
    },
    {
        "name": "set_dedication",
        "description": "Store the short trust/dedication note.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "text": {"type": "string", "maxLength": 500, "default": None},
                "blob_id": {"type": "string", "maxLength": 256, "default": None},
            },
        },
        "output": {"type": "object", "required": ["ok"]},
        "sensitive": False,
        "read_only": False,
        "permission": "assistant:profile",
        "undoable": True,
    },
    {
        "name": "play_dedication",
        "description": "Play the stored dedication (TTS if text-only).",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object", "required": ["played"]},
        "sensitive": False,
        "read_only": True,
        "permission": "assistant:profile",
        "undoable": False,
    },
    {
        "name": "list_callouts",
        "description": "Replay recent status callouts — what just happened.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8}},
        },
        "output": {"type": "object", "required": ["count", "callouts"]},
        "sensitive": False,
        "read_only": True,
        "permission": "assistant:profile",
        "undoable": False,
    },
    {
        "name": "set_quiet_hours",
        "description": "Set quiet hours immediately (until a clock time, or start/end).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "until": {"type": "string", "maxLength": 8, "default": None},
                "start": {"type": "string", "maxLength": 8, "default": None},
                "end": {"type": "string", "maxLength": 8, "default": None},
            },
        },
        "output": {"type": "object", "required": ["start", "end"]},
        "sensitive": False,
        "read_only": False,
        "permission": "assistant:profile",
        "undoable": True,
    },
]


_LIFE_BRIDGES: dict[str, tuple[str, str, str]] = {
    "resolve_contact": ("contacts", "contacts.resolve", "contacts:read"),
    "send_message": ("messaging", "messaging.send", "messaging:act"),
    "list_messages": ("messaging", "messaging.list_messages", "messaging:read"),
    "place_call": ("phone", "phone.call", "phone:act"),
    "list_mail": ("mail", "mail.list", "mail:read"),
}


def _resolved_sensitive(spec: dict) -> bool:
    """Life actions require per-call approval only under EV_OWNER_AUTONOMY=confirm_all."""

    if spec["name"] in LIFE_ACTION_NAMES and spec.get("sensitive") is None:
        return autonomy_mode() == "confirm_all"
    return bool(spec.get("sensitive", False))


def _life_unavailable(reason: str, *, next_step: str, error: str | None = None) -> dict:
    """Capability-theater contract: degraded=true + exact next_step."""

    return {
        "ok": False,
        "degraded": True,
        "next_step": next_step,
        "reason": reason,
        "error": error or reason,
    }


def get_spec(name: str) -> dict | None:
    """Return the declared spec for a tool name, or None when unknown."""
    for spec in TOOL_SPECS:
        if spec["name"] == name:
            resolved = dict(spec)
            resolved["sensitive"] = _resolved_sensitive(spec)
            return resolved
    return None


def list_tools() -> list[dict]:
    return [
        {**spec, "sensitive": _resolved_sensitive(spec)}
        for spec in TOOL_SPECS
    ]


def life_success_reply(result: dict, *, tool_name: str | None = None) -> str:
    """Spoken confirmation shaped from the real tool result — not send-only."""

    payload = result.get("result") if isinstance(result, dict) and "result" in result else result
    payload = payload if isinstance(payload, dict) else {}
    name = (tool_name or payload.get("_tool") or payload.get("tool") or "").strip()
    degraded = bool(payload.get("degraded") or payload.get("ok") is False)
    next_step = str(
        payload.get("next_step") or payload.get("reason") or payload.get("error") or ""
    ).strip()

    if degraded:
        if name == "set_reminder" or "reminder" in next_step.lower():
            return (
                f"I couldn't set that reminder yet. {next_step}"
                if next_step
                else "I couldn't set that reminder yet."
            )
        if name == "present" or "overlay" in next_step.lower() or "hud" in next_step.lower():
            return (
                f"I couldn't open that on screen yet. {next_step}"
                if next_step
                else "I couldn't open that on screen yet."
            )
        if name in {"send_message", "place_call"}:
            return (
                f"I couldn't finish that yet. {next_step}"
                if next_step
                else "I couldn't finish that yet."
            )
        return f"I couldn't finish that yet. {next_step}".strip()

    if name == "set_reminder":
        text = str(payload.get("text") or payload.get("reminder") or "that").strip()
        return f"Reminder set: {text}."
    if name == "present" or "opened" in payload:
        if payload.get("opened"):
            return "Opened that on your screen."
        reason = next_step or str(payload.get("reason") or "").strip()
        return f"I couldn't open that on screen yet. {reason}".strip()

    delivery = payload.get("delivery") or {}
    evidence = delivery.get("evidence") or {}
    target = str(
        evidence.get("recipient")
        or evidence.get("to")
        or payload.get("to")
        or "the recipient"
    )
    channel = str(
        evidence.get("channel")
        or payload.get("channel")
        or payload.get("kind")
        or "the channel"
    )
    sent_at = (
        evidence.get("sent_at")
        or evidence.get("dialed_at")
        or evidence.get("completed_at")
        or payload.get("sent_at")
    )
    time_part = f" at {sent_at}" if sent_at else ""
    return f"Sent to {target} via {channel}{time_part}."


async def dispatch(
    session: AsyncSession,
    name: str,
    arguments: dict,
    *,
    actor: str = "master",
    allow_sensitive: bool = False,
    request_id: str | None = None,
) -> ToolCallResponse:
    """Validate, authorize, execute, shape-check, and log one tool invocation."""

    started = time.perf_counter()
    spec = get_spec(name)
    status = "ok"
    error: str | None = None
    result: dict | None = None

    if spec is None:
        status = "error"
        error = f"Unknown tool '{name}'"
    elif spec["sensitive"] and not allow_sensitive:
        status = "denied"
        error = f"Permission denied: '{name}' requires explicit permission before execution"
    else:
        effective, issues = validate_arguments(arguments, spec["parameters"])
        if issues:
            status = "rejected"
            error = "Invalid arguments: " + "; ".join(issues)
        else:
            try:
                result = await _handle(session, name, effective, actor=actor)
                output_issues = validate_output(result, spec.get("output") or {})
                if output_issues:
                    status = "error"
                    error = "Output validation failed: " + "; ".join(output_issues)
                    result = None
            except KeyError as exc:
                status = "error"
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 - tool boundary
                status = "error"
                error = f"{type(exc).__name__}: {exc}"

    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    response = ToolCallResponse(
        name=name,
        ok=status == "ok",
        result=result,
        error=error,
        latency_ms=latency_ms,
        request_id=request_id,
        actor=actor,
    )
    await log_access(
        session,
        actor=actor,
        action="tool_call",
        endpoint="POST /v1/gateway/tools",
        resource_type="tool",
        resource_ids=[name],
        request_id=request_id,
        details={
            "status": status,
            "latency_ms": latency_ms,
            "permission": spec["permission"] if spec else None,
            "sensitive": bool(spec and spec["sensitive"]),
            "read_only": bool(spec and spec["read_only"]),
            "error": error,
        },
    )
    return response


async def _handle(session: AsyncSession, name: str, args: dict, *, actor: str) -> dict:
    retriever = Retriever(session)
    if name == "search_memory":
        memory_hits = await retriever.search(
            str(args.get("query", "")),
            k=int(args.get("k", 10)),
            access="model",
            memory_types=[args["memory_type"]] if args.get("memory_type") else None,
        )
        return {
            "count": len(memory_hits),
            "results": [
                {
                    "id": h.memory_id,
                    "text": h.text,
                    "memory_type": h.memory_type,
                    "score": h.score,
                    "date": h.event_time.isoformat() if h.event_time else None,
                    "provenance": h.source_event_ids,
                }
                for h in memory_hits[: int(args.get("k", 10))]
            ],
        }
    if name == "search_decisions":
        decision_hits = await retriever.search(
            str(args.get("query", "")),
            k=int(args.get("k", 10)),
            access="model",
            memory_types=["decision"],
        )
        return {
            "count": len(decision_hits),
            "results": [
                {
                    "id": h.memory_id,
                    "text": h.text,
                    "memory_type": h.memory_type,
                    "score": h.score,
                    "date": h.event_time.isoformat() if h.event_time else None,
                    "provenance": h.source_event_ids,
                }
                for h in decision_hits[: int(args.get("k", 10))]
            ],
        }
    if name == "search_timeline":
        timeline_hits = await retriever.search_events(
            str(args.get("query", "")), k=int(args.get("k", 10)), access="model"
        )
        return {"count": len(timeline_hits), "results": timeline_hits}
    if name == "get_person":
        info = await people.whereabouts(session, str(args["name"]))
        return info.model_dump()
    if name == "get_project":
        project = await maker.find_project_by_name(session, str(args["name"]))
        if project is None:
            raise KeyError(f"No project matching '{args['name']}'")
        return {
            "project": {
                "id": str(project.id),
                "name": project.name,
                "status": project.status,
                "current_step": project.current_step,
                "next_step": maker.next_step(project),
            },
            "bom": [
                {
                    "id": str(item.id),
                    "name": item.name,
                    "qty": item.qty,
                    "unit": item.unit,
                    "location": item.location,
                    "reorder_at": item.reorder_at,
                }
                for item in await maker.list_bom(session, project.id)
            ],
            "print_jobs": [
                {
                    "id": str(job.id),
                    "name": job.name,
                    "status": job.status,
                }
                for job in await maker.list_print_jobs(session, project.id)
            ],
        }
    if name == "get_goals":
        rows = (
            await session.execute(
                select(Memory).where(
                    Memory.memory_type == "goal",
                    Memory.is_current.is_(True),
                    Memory.redacted.is_(False),
                )
            )
        ).scalars().all()
        return {"count": len(rows), "goals": [{"id": str(m.id), "text": m.text} for m in rows]}
    if name == "get_patterns":
        stmt = (
            select(Memory)
            .where(
                Memory.memory_type == "pattern",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
            .order_by(Memory.confidence.desc())
        )
        rows = list((await session.execute(stmt)).scalars().all())
        topic = (args.get("topic") or "").lower()
        if topic:
            rows = [m for m in rows if topic in (m.payload or {}).get("topic", "").lower()]
        return {
            "count": len(rows),
            "patterns": [
                {
                    "id": str(m.id),
                    "text": m.text,
                    "kind": (m.payload or {}).get("kind"),
                    "confidence": m.confidence,
                    "evidence": (m.payload or {}).get("evidence", []),
                }
                for m in rows
            ],
        }
    if name == "calculate":
        return {"expression": args["expression"], "result": safe_calculate(str(args["expression"]))}
    if name == "get_health_trends":
        trend = await health_radar.trend(
            session,
            metric=str(args["metric"]),
            window_days=int(args.get("window_days", 14)),
        )
        return trend
    if name == "get_gear_status":
        gear_stmt = select(GearSnapshot).order_by(GearSnapshot.reported_at.desc()).limit(1)
        if args.get("device_id"):
            gear_stmt = gear_stmt.where(GearSnapshot.device_id == str(args["device_id"]))
        row = (await session.execute(gear_stmt)).scalars().first()
        if row is None:
            return {"found": False}
        return {
            "found": True,
            "device_id": row.device_id,
            "reported_at": row.reported_at.isoformat(),
            "battery_percent": row.battery_percent,
            "storage_free_bytes": row.storage_free_bytes,
            "cpu_percent": row.cpu_percent,
            "memory_used_percent": row.memory_used_percent,
        }
    if name == "get_upcoming_alerts":
        from app.ev import alert_radar

        alerts = await alert_radar.list_alerts(session, status="pending", limit=int(args.get("limit", 10)))
        return {
            "count": len(alerts),
            "alerts": [
                {
                    "id": str(a.id),
                    "kind": a.kind,
                    "title": a.title,
                    "body": a.body,
                    "priority": a.priority,
                    "tier": a.tier,
                }
                for a in alerts
            ],
        }
    if name == "get_research":
        sessions = await list_sessions(session, status=args.get("status"), limit=int(args.get("limit", 10)))
        return {
            "count": len(sessions),
            "sessions": [
                {
                    "id": str(s.id),
                    "question": s.question,
                    "status": s.status,
                    "conclusion": s.conclusion,
                }
                for s in sessions
            ],
        }
    if name == "search_web":
        provider = get_search_provider()
        if provider is None:
            return {
                **_life_unavailable(
                    "web search is disabled",
                    next_step=(
                        "set EV_SEARCH_PROVIDER=live (Open-Meteo weather, no key) "
                        "or EV_SEARCH_PROVIDER=brave with EV_BRAVE_SEARCH_API_KEY"
                    ),
                ),
                "count": 0,
                "results": [],
            }
        results = await provider.search(
            str(args["query"]),
            limit=int(args.get("limit", 5)),
        )
        return {
            "ok": True,
            "count": len(results),
            "results": [
                {"title": r.title, "url": r.url, "snippet": r.snippet}
                for r in results
            ],
        }
    if name == "get_weather":
        from app.search.live import weather_results

        place = str(args.get("place") or "").strip()
        query = str(args.get("query") or place or "weather").strip()
        if place and "weather" not in query.lower():
            query = f"weather in {place}"
        results = await weather_results(query, limit=3)
        return {
            "ok": True,
            "count": len(results),
            "place": place or None,
            "results": [
                {"title": r.title, "url": r.url, "snippet": r.snippet}
                for r in results
            ],
        }
    if name in _LIFE_BRIDGES:
        return await _dispatch_life_action(session, name, args, actor=actor)
    if name == "open_url":
        return _life_unavailable(
            "no open-url bridge is installed",
            next_step=(
                "grant an open-url bridge: SUIT can add an EVLifeHelper "
                "'apps.activate'/'open.url' command or CONDUIT a device_proxy "
                "action, then wire it in app/integrations/adapters.py"
            ),
        )
    if name == "set_reminder":
        return _life_unavailable(
            "no reminders bridge is installed",
            next_step=(
                "grant a reminders bridge (CONDUIT calendar/reminders adapter or "
                "Agent 14 routines), then wire set_reminder to it"
            ),
        )
    if name == "set_assistant_name":
        from app.ev.assistant import set_nickname

        decision = await set_nickname(session, str(args["name"]))
        return {"ok": decision.ok, "name": decision.name, "reason": decision.reason}
    if name == "reset_assistant_name":
        from app.ev.assistant import reset_nickname

        decision = await reset_nickname(session)
        return {"ok": True, "name": decision.name}
    if name == "update_personality":
        from app.ev.personality import get_current, to_dict, update
        from app.schemas import PersonalityUpdate

        current = to_dict(await get_current(session))
        for key in ("humor", "formality", "verbosity", "directness"):
            if args.get(key) is not None:
                current[key] = int(args[key])
        profile = await update(
            session,
            PersonalityUpdate(
                **current,
                reason_for_change=args.get("reason_for_change") or "tool",
            ),
        )
        return {"ok": True, "profile": to_dict(profile)}
    if name == "list_protocols":
        from app.ev.protocols import protocol_sheet, protocols_to_dicts

        items = await protocol_sheet(session)
        wanted = args.get("filter") or "all"
        if wanted != "all":
            items = [item for item in items if item.status == wanted]
        return {"protocols": protocols_to_dicts(items)}
    if name == "set_dedication":
        from app.ev.assistant import set_dedication

        dedication = await set_dedication(
            session, text=args.get("text"), blob_id=args.get("blob_id")
        )
        return {
            "ok": True,
            "text": dedication.dedication_text,
            "blob_id": dedication.dedication_blob_id,
        }
    if name == "play_dedication":
        from app.ev.assistant import play_dedication

        return await play_dedication(session, auto=False)
    if name == "list_callouts":
        from app.ev.callouts import list_callouts

        callouts = await list_callouts(session, limit=int(args.get("limit") or 8))
        return {
            "count": len(callouts),
            "callouts": [
                {
                    "text": row.text,
                    "source": row.source,
                    "spoken": row.spoken,
                    "created_at": row.created_at.isoformat(),
                }
                for row in callouts
            ],
        }
    if name == "set_quiet_hours":
        from app.notify.proactive import persist_quiet_hours, set_quiet_hours

        hours = set_quiet_hours(
            until=args.get("until"),
            start=args.get("start"),
            end=args.get("end"),
        )
        await persist_quiet_hours(session)
        return hours
    if name == "present":
        from app.notify.presence import open_presence

        kind = str(args.get("kind") or "auto")
        return await open_presence(
            title=str(args["title"]),
            body=str(args["body"]),
            kind=kind,
            size=args.get("size"),
            time_type=args.get("time_type"),
            placement=args.get("placement"),
            ttl_ms=args.get("ttl_ms"),
            items=args.get("items") or [],
            recommendation=args.get("recommendation"),
            source=args.get("source"),
            lookout=args.get("lookout"),
            window_id=args.get("window_id"),
            auto=kind.lower() in {"auto", "decide"},
            message=str(args["title"]) + " " + str(args["body"]),
        )
    raise KeyError(f"Unknown tool '{name}'")


async def _active_life_integration(session: AsyncSession, adapter_slug: str) -> Integration | None:
    """The active CONDUIT integration backing one life bridge, if any."""

    row = (
        await session.execute(
            select(Integration)
            .where(
                Integration.adapter == adapter_slug,
                Integration.status == "active",
            )
            .order_by(Integration.created_at.asc())
            .limit(1)
        )
    ).scalars().first()
    return row


async def _dispatch_life_action(
    session: AsyncSession,
    name: str,
    args: dict,
    *,
    actor: str,
) -> dict:
    """Call the CONDUIT adapter for one life action; never fake success."""

    slug, action, scope = _LIFE_BRIDGES[name]
    integration = await _active_life_integration(session, slug)
    if integration is None:
        return _life_unavailable(
            f"no {slug} bridge is installed",
            next_step=(
                f"install the {slug} integration and grant scope '{scope}' "
                f"(POST /v1/integrations with adapter={slug})"
            ),
        )
    try:
        outcome = await integrations.execute_action(
            session,
            integration.id,
            action,
            args,
            actor=actor,
        )
    except LifeHelperUnavailableError as exc:
        return _life_unavailable(
            f"{slug} bridge is unavailable",
            next_step=str(exc),
            error=str(exc),
        )
    except PermissionError as exc:
        return _life_unavailable(
            f"{slug} bridge permission denied",
            next_step=str(exc),
            error=str(exc),
        )
    except LifeHelperError as exc:
        return _life_unavailable(
            f"{slug} bridge failed",
            next_step=str(exc),
            error=str(exc),
        )
    except (LookupError, ValueError) as exc:
        return _life_unavailable(
            f"{slug} bridge error",
            next_step=str(exc),
            error=str(exc),
        )
    payload = getattr(outcome, "result", None) or {}
    return {"ok": True, **payload}
