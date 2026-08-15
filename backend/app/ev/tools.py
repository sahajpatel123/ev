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
from app.ev.fleet_tools import FLEET_TOOL_SPECS, actuate_permission, handle_fleet_tool
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
                "to": {"type": "string", "minLength": 1, "maxLength": 256, "default": None},
                "name": {"type": "string", "minLength": 1, "maxLength": 256, "default": None},
                "destination": {"type": "string", "minLength": 1, "maxLength": 256, "default": None},
                "kind": {"type": "string", "enum": ["tel", "facetime"], "default": "tel"},
                "video": {"type": "boolean", "default": False},
                "confirm": {"type": "boolean", "default": False},
            },
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
                "questions": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 240},
                    "maxItems": 6,
                    "default": None,
                },
                "response": {"type": "string", "maxLength": 4000, "default": None},
                "layout": {"type": "string", "maxLength": 16, "default": None},
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
        "name": "calibrate",
        "description": "Run self-diagnostics (database, embeddings, gateway, retrieval, storage, printer/radio if present).",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object", "required": ["spoken", "hud"]},
        "sensitive": False,
        "read_only": True,
        "permission": "diagnostics:read",
        "undoable": False,
    },
    {
        "name": "research",
        "description": "Open or continue a research session with cited sources.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"question": {"type": "string", "minLength": 1, "maxLength": 2000}},
            "required": ["question"],
        },
        "output": {"type": "object", "required": ["answer", "citations"]},
        "sensitive": False,
        "read_only": False,
        "permission": "research:write",
        "undoable": False,
    },
    {
        "name": "print_start",
        "description": "Queue and optionally start a 3D print. Requires confirm and training wheels.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "maxLength": 200, "default": None},
                "gcode": {"type": "string", "maxLength": 200, "default": None},
                "confirm": {"type": "boolean", "default": False},
            },
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": True,
        "read_only": False,
        "permission": "printer:act",
        "undoable": False,
    },
    {
        "name": "estimate_print",
        "description": "Estimate print time and filament from an uploaded STL/STEP/SVG.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"attachment_id": {"type": "string", "minLength": 1}},
            "required": ["attachment_id"],
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": False,
        "read_only": True,
        "permission": "printer:read",
        "undoable": False,
    },
    {
        "name": "gear_power",
        "description": "Report battery and storage for a device, or last telemetry sample during a test.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"device": {"type": "string", "maxLength": 200, "default": None}},
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": False,
        "read_only": True,
        "permission": "gear:read",
        "undoable": False,
    },
    {
        "name": "health_how_do_i_look",
        "description": "Speak readiness and flags from the latest health snapshot. Not a diagnosis.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": True,
        "read_only": True,
        "permission": "health:read",
        "undoable": False,
    },
    {
        "name": "head_injury_screen",
        "description": "Scripted head-injury symptom check with a fixed medical disclaimer. Never diagnoses.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "abort": {"type": "boolean", "default": False},
                "call_someone": {"type": "boolean", "default": False},
            },
        },
        "output": {"type": "object", "required": ["spoken", "disclaimer"]},
        "sensitive": True,
        "read_only": False,
        "permission": "health:read",
        "undoable": False,
    },
    {
        "name": "brief_me",
        "description": "Speak a condensed tactical brief and emit the full HUD briefing card.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"topic": {"type": "string", "maxLength": 500, "default": None}},
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": False,
        "read_only": True,
        "permission": "tactical:read",
        "undoable": False,
    },
    {
        "name": "brief_share",
        "description": "Share the current brief with a delegate who has briefing:read.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"delegate": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["delegate"],
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": True,
        "read_only": False,
        "permission": "tactical:share",
        "undoable": False,
    },
    {
        "name": "where_is",
        "description": "Locate an opted-in teammate, or say memory-only. Never hunt strangers.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["name"],
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": False,
        "read_only": True,
        "permission": "people:read",
        "undoable": False,
    },
    {
        "name": "camera_replay",
        "description": "Replay an owner-added camera. Never discover cameras on the LAN.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "camera": {"type": "string", "minLength": 1, "maxLength": 128},
                "at": {"type": "string", "maxLength": 64, "default": None},
            },
            "required": ["camera"],
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": True,
        "read_only": True,
        "permission": "camera:read",
        "undoable": False,
    },
    {
        "name": "watchlist_add",
        "description": "Add a topic to the owner alert watchlist.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "value": {"type": "string", "minLength": 1, "maxLength": 256},
                "kind": {
                    "type": "string",
                    "enum": ["topic", "project", "person", "product", "company", "deadline", "date"],
                    "default": "topic",
                },
            },
            "required": ["value"],
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": False,
        "read_only": False,
        "permission": "alerts:write",
        "undoable": True,
    },
    {
        "name": "alerts_digest",
        "description": "List pending watchlist and radar alerts.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": False,
        "read_only": True,
        "permission": "alerts:read",
        "undoable": False,
    },
    {
        "name": "media_check",
        "description": "Best-effort media authenticity. Never claims a video is real.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"attachment_id": {"type": "string", "minLength": 1}},
            "required": ["attachment_id"],
        },
        "output": {"type": "object", "required": ["spoken", "label"]},
        "sensitive": False,
        "read_only": True,
        "permission": "vision:read",
        "undoable": False,
    },
    {
        "name": "set_voice",
        "description": "Change TTS voice or rate for accessibility. Not an interrogation mode.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "voice_id": {"type": "string", "maxLength": 64, "default": None},
            },
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": False,
        "read_only": False,
        "permission": "assistant:profile",
        "undoable": True,
    },
    {
        "name": "public_lookup",
        "description": "Look up public records on allowlisted sources (Wikipedia, SEC, gazettes).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 400},
                "kind": {"type": "string", "enum": ["org", "law", "filing"], "default": "org"},
            },
            "required": ["query"],
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": False,
        "read_only": True,
        "permission": "web:search",
        "undoable": False,
    },
    {
        "name": "find_gear",
        "description": "Find an owner-registered beacon or last-seen EV device. Refuses person hunts.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"label": {"type": "string", "minLength": 1, "maxLength": 128}},
            "required": ["label"],
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": False,
        "read_only": True,
        "permission": "gear:read",
        "undoable": False,
    },
    {
        "name": "estimate_structure",
        "description": "Low-confidence size guess from a photo. Not structural analysis.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "attachment_id": {"type": "string", "minLength": 1},
                "reference_length": {"type": "number", "default": None},
            },
            "required": ["attachment_id"],
        },
        "output": {"type": "object", "required": ["spoken", "disclaimer"]},
        "sensitive": False,
        "read_only": True,
        "permission": "vision:read",
        "undoable": False,
    },
    {
        "name": "why_did_you_ping",
        "description": "Explain the last fused sense callout and cite source ids.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": False,
        "read_only": True,
        "permission": "alerts:read",
        "undoable": False,
    },
    {
        "name": "whats_on_my_plate",
        "description": "Aggregate calendar, mail, GitHub, and watchlist deadlines.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": False,
        "read_only": True,
        "permission": "life:read",
        "undoable": False,
    },
    {
        "name": "draft_reply",
        "description": "Draft a mail reply. Sending requires owner confirm and helper sent=true.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mail_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "body": {"type": "string", "maxLength": 8000, "default": None},
                "confirm": {"type": "boolean", "default": False},
                "send": {"type": "boolean", "default": False},
            },
            "required": ["mail_id"],
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": True,
        "read_only": False,
        "permission": "mail:write",
        "undoable": True,
    },
    {
        "name": "drone",
        "description": "Command an owner-paired drone: takeoff, hover, land, rtl. No weapons.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 32},
                "confirm": {"type": "boolean", "default": False},
                "lat": {"type": "number", "default": None},
                "lon": {"type": "number", "default": None},
            },
            "required": ["command"],
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": True,
        "read_only": False,
        "permission": "drone:act",
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
    *FLEET_TOOL_SPECS,
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
    if payload.get("spoken"):
        return str(payload["spoken"])
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
    device_id=None,
    reverify_token: str | None = None,
) -> ToolCallResponse:
    """Validate, authorize, execute, shape-check, and log one tool invocation."""

    from app.ev.delegates import scope_blocked
    from app.ev.training_wheels import ensure_seed_gates, refuse_if_locked
    from app.ev.voice_life import WEAPON_RE, consume_life_reverify

    started = time.perf_counter()
    spec = get_spec(name)
    status = "ok"
    error: str | None = None
    result: dict | None = None
    await ensure_seed_gates(session)

    if spec is not None and name == "actuate":
        spec = dict(spec)
        spec["permission"] = actuate_permission(str((arguments or {}).get("verb") or ""))

    if spec is None:
        status = "error"
        error = f"Unknown tool '{name}'"
    elif name == "actuate" and WEAPON_RE.search(str((arguments or {}).get("verb") or "")):
        status = "denied"
        error = "refused"
        result = {
            "ok": False,
            "error": "refused",
            "spoken": "I will not run kill or weapon verbs.",
        }
    elif spec["sensitive"] and not allow_sensitive:
        status = "denied"
        error = f"Permission denied: '{name}' requires explicit permission before execution"
    else:
        bio = await consume_life_reverify(
            session,
            actor=actor,
            device_id=device_id,
            reverify_token=reverify_token,
            name=name,
            args=arguments or {},
        )
        refuse = await refuse_if_locked(session, spec)
        scoped = await scope_blocked(
            session,
            actor=actor,
            permission=str(spec["permission"]),
            name=name,
            device_id=device_id,
        )
        if bio is not None:
            status = "denied"
            error = "biometric_required"
            result = bio
        elif refuse is not None:
            status = "denied"
            error = str(refuse.get("error") or "training_wheels")
            result = refuse
        elif scoped is not None:
            status = "denied"
            error = str(scoped.get("error") or "delegate_scope")
            result = scoped
        else:
            effective, issues = validate_arguments(arguments, spec["parameters"])
            if issues:
                status = "rejected"
                error = "Invalid arguments: " + "; ".join(issues)
            else:
                try:
                    result = await _handle(session, name, effective, actor=actor)
                    if result is not None:
                        from app.ev.workbench import push_status_hud

                        await push_status_hud(session, name, result)
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
    fleet = await handle_fleet_tool(session, name, args, actor=actor)
    if fleet is not None:
        return fleet
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
        payload = [
            {"title": r.title, "url": r.url, "snippet": r.snippet}
            for r in results
        ]
        from app.ev.workbench import weather_hud

        first = payload[0] if payload else {}
        spoken = str(first.get("snippet") or first.get("title") or "No weather.")
        return {
            "ok": True,
            "count": len(results),
            "place": place or None,
            "results": payload,
            "spoken": spoken,
            "hud": weather_hud(payload),
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
        from uuid import uuid4

        from app.ev.briefing import extract_reminder_when
        from app.ev.timers import start_timer
        from app.models import Alert

        text = str(args.get("text") or "").strip()
        when = args.get("when") or extract_reminder_when(text)
        blob = f"{when or ''} {text}"
        minutes: float | None = None
        at: str | None = None
        relative = extract_reminder_when(blob)
        if relative and relative.lower().startswith("in "):
            parts = relative.split()
            try:
                amount = float(parts[1])
            except (IndexError, ValueError, TypeError):
                amount = None
            unit = parts[2].lower() if len(parts) > 2 else "minutes"
            if amount is not None:
                hours = unit.startswith("hour") or unit.startswith("hr")
                minutes = amount * 60 if hours else amount
        elif relative:
            at = relative
        elif when:
            at = str(when)
        if minutes is not None or at:
            timed = await start_timer(session, minutes=minutes, at=at, text=text)
            if timed.get("ok"):
                return {
                    "ok": True,
                    "text": text,
                    "when": timed.get("fire_at"),
                    "id": timed.get("id"),
                    "spoken": timed.get("spoken") or f"Reminder set: {text}.",
                }
        alert = Alert(
            kind="reminder",
            title="Reminder",
            body=text[:2000],
            priority=0.6,
            tier="useful",
            status="pending",
            source="set_reminder",
            fingerprint=uuid4().hex,
            rationale="Owner asked to be reminded.",
            details={"text": text, "when": when},
        )
        session.add(alert)
        await session.flush()
        return {
            "ok": True,
            "text": text,
            "id": str(alert.id),
            "stored": "alert",
            "spoken": f"Reminder set: {text}.",
        }
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
    if name == "calibrate":
        from app.ev.workbench import handle_calibrate

        return await handle_calibrate(session)
    if name == "research":
        from app.ev.workbench import handle_research

        return await handle_research(session, str(args["question"]))
    if name == "print_start":
        from app.ev.hardware import print_start

        return await print_start(
            session,
            project=args.get("project"),
            gcode=args.get("gcode"),
            confirm=bool(args.get("confirm")),
            actor=actor,
        )
    if name == "estimate_print":
        from app.ev.hardware import estimate_print

        return await estimate_print(session, str(args["attachment_id"]))
    if name == "gear_power":
        from app.ev.workbench import handle_gear_power

        return await handle_gear_power(session, args.get("device"))
    if name == "health_how_do_i_look":
        from app.ev.workbench import handle_how_do_i_look

        return await handle_how_do_i_look(session)
    if name == "head_injury_screen":
        from app.ev.workbench import handle_head_injury_screen

        return await handle_head_injury_screen(
            session,
            abort=bool(args.get("abort")),
            call_someone=bool(args.get("call_someone")),
        )
    if name == "brief_me":
        from app.ev.workbench import handle_brief_me

        return await handle_brief_me(session, args.get("topic"))
    if name == "brief_share":
        from app.ev.workbench import handle_brief_share

        return await handle_brief_share(session, str(args["delegate"]))
    if name == "where_is":
        from app.ev.workbench import handle_where_is

        return await handle_where_is(session, str(args["name"]))
    if name == "camera_replay":
        from app.ev.hardware import camera_replay

        return await camera_replay(
            session, camera=str(args["camera"]), at=args.get("at"), actor=actor
        )
    if name == "watchlist_add":
        from app.ev.workbench import handle_watchlist_add

        return await handle_watchlist_add(
            session, str(args["value"]), kind=str(args.get("kind") or "topic")
        )
    if name == "alerts_digest":
        from app.ev.workbench import handle_alerts_digest

        return await handle_alerts_digest(session)
    if name == "media_check":
        from app.ev.hardware import media_check

        return await media_check(session, str(args["attachment_id"]))
    if name == "set_voice":
        from app.ev.workbench import handle_set_voice

        return await handle_set_voice(session, args.get("voice_id"))
    if name == "public_lookup":
        from app.ev.workbench import handle_public_lookup

        return await handle_public_lookup(
            session, str(args["query"]), kind=str(args.get("kind") or "org")
        )
    if name == "find_gear":
        from app.ev.hardware import find_gear

        return await find_gear(session, str(args["label"]))
    if name == "estimate_structure":
        from app.ev.hardware import estimate_structure

        return await estimate_structure(
            session,
            str(args["attachment_id"]),
            reference_length=args.get("reference_length"),
        )
    if name == "why_did_you_ping":
        from app.ev.workbench import handle_why_did_you_ping

        return await handle_why_did_you_ping(session)
    if name == "whats_on_my_plate":
        from app.ev.workbench import handle_whats_on_my_plate

        return await handle_whats_on_my_plate(session)
    if name == "draft_reply":
        from app.ev.workbench import handle_draft_reply

        return await handle_draft_reply(
            session,
            str(args["mail_id"]),
            body=args.get("body"),
            confirm=bool(args.get("confirm")),
            send=bool(args.get("send")),
        )
    if name == "drone":
        from app.ev.hardware import drone_command

        return await drone_command(
            session,
            str(args["command"]),
            confirm=bool(args.get("confirm")),
            lat=args.get("lat"),
            lon=args.get("lon"),
            actor=actor,
        )
    if name == "present":
        from app.notify.presence import open_presence

        kind = str(args.get("kind") or "auto")
        opened = await open_presence(
            title=str(args["title"]),
            body=str(args["body"]),
            kind=kind,
            size=args.get("size"),
            time_type=args.get("time_type"),
            placement=args.get("placement"),
            ttl_ms=args.get("ttl_ms"),
            items=args.get("items") or [],
            questions=args.get("questions") or [],
            response=args.get("response"),
            layout=args.get("layout"),
            recommendation=args.get("recommendation"),
            source=args.get("source"),
            lookout=args.get("lookout"),
            window_id=args.get("window_id"),
            auto=kind.lower() in {"auto", "decide"},
            message=str(args["title"]) + " " + str(args["body"]),
        )
        from app.ev.training_wheels import mark_step_from_event

        await mark_step_from_event(session, "first_hud")
        return opened
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
