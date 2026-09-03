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
from datetime import timedelta
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
from app.utils.text import utcnow

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


# --- EV VOICE CONTROL PLAN §4 (additive) ---
# UI-specific verbs map onto the existing computer primitives so ANY app is
# controllable by changing the query string, never by adding a per-app verb.
# double_click and drag are handled explicitly in ``_handle`` (multi-step).
UI_VERB_MAP: dict[str, tuple[str, dict[str, Any]]] = {
    "read": ("inspect_ui", {}),
    "see": ("screen_look", {}),
    "click": ("ui_action", {"action": "press"}),
    "double_click": ("ui_action", {"action": "double_click"}),
    "right_click": ("ui_action", {"action": "right_click"}),
    "type": ("ui_action", {"action": "type"}),
    "paste": ("ui_action", {"action": "paste"}),
    "key": ("ui_action", {"action": "keyboard"}),
    "scroll": ("ui_action", {"action": "scroll"}),
    "drag": ("ui_action", {"action": "drag"}),
}

# The complete UI-verb family. Kill-switch and surfaces key off this set.
UI_VERB_TOOLS: frozenset[str] = frozenset(UI_VERB_MAP)


TOOL_SPECS: list[dict[str, Any]] = [
    {
        # F4: explicit deep-history escape hatch over the F0+F1 retrieval
        # stack. Same substrate as search_memory; different model surface.
        "name": "recall",
        "description": (
            "Recall the owner's history: people, chats, photos, notes, mail, "
            "contacts, past conversations, decisions, and what was left "
            "unfinished. You already know this owner; that life is not new. "
            "Use when they ask about the past, someone they know, or whether "
            "you know them. Returns a small evidence pack; answer from it. If "
            "empty for the specific question, say you cannot find that "
            "particular record — never that you have no history with them."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "detail": {
                    "type": "string",
                    "enum": ["brief", "expanded", "source"],
                    "default": "expanded",
                },
            },
            "required": ["query"],
        },
        "output": {"type": "object", "required": ["count", "results"]},
        "sensitive": False,
        "read_only": True,
        "risk_class": "R0",
        "permission": "memory:read",
        "undoable": False,
    },
    {
        # F4: goal-level computer surface. The backend routes the goal
        # (semantic path, planner, executor); the model never picks
        # implementation details or assigns risk.
        "name": "computer",
        "description": (
            "Mac goal: open or close apps, open a URL, operate UI, or handle "
            "owner files in Desktop/Documents/Downloads. State the request in "
            "plain words. After a verified new_tab or close_tab, stop. After "
            "opening a site, finish the first requested click. Not for memory, "
            "weather, messages, timers, or writing programs — call code."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "goal": {"type": "string", "minLength": 1, "maxLength": 500},
                "target_app": {"type": "string", "maxLength": 80},
            },
            "required": ["goal"],
        },
        "output": {"type": "object", "required": ["ok"]},
        "sensitive": False,
        "read_only": False,
        "risk_class": "R1",
        "permission": "computer:control",
        "undoable": False,
    },
    {
        "name": "code",
        "description": (
            "Write, edit, or run software in an owner-allowed project. Call "
            "this when they ask to code, make a helper or script, fix a bug, "
            "add a test, run it, change the last script, or implement something "
            "in a real repo. Casual phrasing counts ('can you make me a python "
            "grader', 'run it', 'add a test'). Pass the full request as goal, "
            "including the project name when they named one. Do not type into "
            "an editor with computer/UI verbs."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "goal": {"type": "string", "minLength": 1, "maxLength": 4000},
            },
            "required": ["goal"],
        },
        "output": {"type": "object", "required": ["ok", "spoken"]},
        "sensitive": False,
        "read_only": False,
        "risk_class": "R1",
        "permission": "software:code",
        "undoable": False,
        "timeout_seconds": 300,
        "provider": "software",
    },
    {
        "name": "search_memory",
        "description": (
            "Search Evie's persistent owner memory, conversation history, "
            "camera observations (looks, clothing, objects, colors, saved "
            "clips), and life-archive shelves (contacts, calendar, notes, "
            "tasks, mail subjects, photos by date/album/filename). Call this "
            "when they ask what you talked about, what they decided, who is "
            "in their contacts, what is on the calendar, what they were "
            "wearing, when you last saw an object, what they asked you to "
            "remember from a look, whether you memorized or remembered something "
            "they showed, what they preferred or decided, what got solved, where "
            "they left off, or whether you remember a "
            "fact. Also use for people Evie knows from life or WhatsApp, "
            "old chat summaries, and iCloud Drive notes. Visual follow-ups search what you already saw on camera, "
            "including things they asked you to memorize, "
            "not Apple Photos. Also use when they ask if you know them, their "
            "life, or their history. Do not guess. Results are a small evidence "
            "pack, not the whole archive. Prefer exact owner utterances and "
            "dated camera observations. Do not say you have no record until "
            "this search returns empty evidence, and never generalize an empty "
            "lookup into not knowing them."
        ),
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
                        "open_loop",
                        "rejection",
                        "hypothesis",
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
            "Search the public web for current facts about anything the owner "
            "names or that you just identified with look (a product, "
            "place, person, or object). Pass a concrete query — the title, "
            "name, or words they used — not 'it' or 'this'. Summarize cited "
            "results in speech. Never say you cannot search when this function "
            "is listed."
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
        "risk_class": "R0",
        "confirmation": "none",
        "target_ownership": "public",
        "provider": "open-meteo",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
    },
    {
        "name": "heading_out",
        "description": (
            "One spoken leave-the-house beat: live weather, the next calendar "
            "commitment, when to leave, and optionally text someone they are "
            "late. Use when they say they are heading out, headed out, leaving "
            "the house, gotta go, on their way, time to go, or going out. Do "
            "not split this into weather, calendar, leave-by, and message "
            "calls. Pass notify_to and notify_text only when they asked to "
            "text or tell someone."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "notify_to": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 80,
                    "default": None,
                },
                "notify_text": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 400,
                    "default": None,
                },
                "place": {"type": "string", "minLength": 1, "maxLength": 80, "default": None},
            },
        },
        "output": {"type": "object", "required": ["ok", "spoken"]},
        "sensitive": False,
        "read_only": True,
        "permission": "web:search",
        "undoable": False,
        "risk_class": "R0",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
        "evidence": ["source", "timestamp", "weather", "leave_by"],
        "fallback": "report unavailable; do not fabricate weather or calendar",
        "idempotency": "natural",
        "cancellation": "not_applicable",
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
        "risk_class": "R2",
        "confirmation": "standing",
        "target_ownership": "owner",
        "provider": "messaging",
        "evidence": ["source", "timestamp"],
        "idempotency": "key",
        "cancellation": "not_applicable",
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
        "risk_class": "R0",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "messaging",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
        "required_scopes": ["message:read"],
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
        "risk_class": "R3",
        "confirmation": "fresh",
        "target_ownership": "owner",
        "provider": "phone",
        "evidence": ["source", "timestamp", "opened"],
        "idempotency": "key",
        "cancellation": "timeout",
        "required_scopes": ["phone:act"],
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
        "risk_class": "R0",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "mail",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
        "required_scopes": ["mail:read"],
    },
    {
        "name": "open_url",
        "description": (
            "Open an http or https link in the owner's default browser via the "
            "Mac life helper. Do not invent a URL. Do not claim success without "
            "opened evidence."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "url": {"type": "string", "minLength": 1, "maxLength": 2048}
            },
            "required": ["url"],
        },
        "output": {"type": "object"},
        "sensitive": None,
        "read_only": False,
        "permission": "life:open_url",
        "undoable": False,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "macos_life",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
        "required_scopes": ["life:open_url"],
    },
    {
        "name": "open_app",
        "description": (
            "Open or focus an application on the owner's Mac. Prefer this when "
            "they name Safari, Notes, Settings, TextEdit, Calculator, Chrome, "
            "Spotify, or any installed app. Resolve natural names. Verify it is "
            "running before claiming success. Then continue with inspect_ui / "
            "ui_action if they asked to do something inside the app."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 64}
            },
            "required": ["name"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "apps:act",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "macos_life",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "close_app",
        "required_scopes": ["apps:act"],
    },
    {
        "name": "close_app",
        "description": (
            "Quit a Mac app gracefully. Will not quit Finder or EV. If a save "
            "dialog appears, inspect_ui instead of force quitting."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 64}
            },
            "required": ["name"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "apps:act",
        "undoable": False,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "macos_life",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
        "required_scopes": ["apps:act"],
    },
    {
        "name": "computer_status",
        "description": (
            "Read whether Mac computer control is actually ready right now: "
            "connected client, Accessibility, screen vision, and the front app. "
            "Call this when the owner asks if you can control apps."
        ),
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "apps:act",
        "undoable": True,
        "risk_class": "R0",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "computer",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
        "required_scopes": ["apps:act"],
    },
    {
        "name": "list_apps",
        "description": (
            "List running or installed Mac apps. Use to resolve names like "
            "browser, settings, or code before opening."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "maxLength": 80},
                "running_only": {"type": "boolean"},
            },
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "apps:act",
        "undoable": True,
        "risk_class": "R0",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "computer",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
        "required_scopes": ["apps:act"],
    },
    {
        "name": "activate_app",
        "description": (
            "Bring a running Mac app or its window to the front. Opening is not "
            "the same as focusing."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 80},
                "bundle_id": {"type": "string", "maxLength": 200},
            },
            "required": ["name"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "apps:act",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "computer",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
        "required_scopes": ["apps:act"],
    },
    {
        "name": "inspect_ui",
        "description": (
            "Inspect the front Mac app window as compact accessibility UI with "
            "short-lived element refs (e12_1). Refs are snapshot-scoped and go "
            "stale after the next inspect. Pass query to search for a "
            "control by title/label (example: Bluetooth, document, Continue). "
            "Call this before clicking or typing, and again to verify."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "app": {"type": "string", "maxLength": 80},
                "name": {"type": "string", "maxLength": 80},
                "query": {"type": "string", "maxLength": 80},
                "level": {
                    "type": "string",
                    "enum": ["summary", "targeted", "expanded"],
                },
            },
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "apps:act",
        "undoable": True,
        "risk_class": "R0",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "computer",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
        "required_scopes": ["apps:act"],
    },
    {
        "name": "ui_action",
        "description": (
            "Perform a semantic UI action on a current inspect_ui element ref. "
            "Actions: press, focus, set_value, type, append, replace, paste, select, increment, "
            "decrement, expand, collapse, menu, scroll, keyboard, click_at, double_click, "
            "right_click, drag. type inserts at the caret. append adds. replace overwrites. "
            "paste with text types via clipboard; paste without text sends cmd+v. "
            "Prefer element refs over coordinates. click_at and drag require a recent "
            "screen_look frame_id and normalized 0-1 coordinates. drag is a real "
            "mouse-down/move/up, not a click."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "element_ref": {"type": "string", "maxLength": 24},
                "action": {
                    "type": "string",
                    "enum": [
                        "press",
                        "click",
                        "focus",
                        "set_value",
                        "type",
                        "type_text",
                        "append",
                        "replace",
                        "paste",
                        "select",
                        "increment",
                        "decrement",
                        "expand",
                        "collapse",
                        "scroll",
                        "keyboard",
                        "menu",
                        "click_at",
                        "double_click",
                        "right_click",
                        "drag",
                        "confirm",
                        "cancel",
                        "raise",
                    ],
                },
                "value": {"type": "string", "maxLength": 4000},
                "text": {"type": "string", "maxLength": 4000},
                "keys": {"type": "string", "maxLength": 64},
                "direction": {"type": "string", "maxLength": 16},
                "frame_id": {"type": "string", "maxLength": 40},
                "x_normalized": {"type": "number"},
                "y_normalized": {"type": "number"},
            },
            "required": ["action"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "apps:act",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "computer",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
        "required_scopes": ["apps:act"],
    },
    {
        "name": "screen_look",
        "description": (
            "Capture the front window (or display) as an image when "
            "accessibility is insufficient. A screenshot may be added to this "
            "conversation. Describe only what you can actually see."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["active_window", "app", "display"],
                },
                "app": {"type": "string", "maxLength": 80},
            },
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "apps:act",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "computer",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
        "required_scopes": ["apps:act"],
    },
    {
        "name": "app_action",
        "description": (
            "Semantic control for a supported Mac app. Prefer this over "
            "Accessibility clicking for Apple Music: find a playlist, list "
            "tracks, play a 1-based track index, pause, next, previous, and "
            "read player state. Preserve ordinals (first=1). Opening Music is "
            "not completion. Only claim playing when verified is true. If the "
            "playlist does not exist, report failure; do not invent one. "
            "Safari: if they name a URL or domain, navigate there; otherwise "
            "search only the words they asked to find, then navigate to open "
            "the first result when they asked for that. Verify URL. "
            "Safari/Chrome also: new_tab, close_tab, next_tab, previous_tab — "
            "run each once; after verified is true, do not repeat. "
            "Notes: create/append with the exact text in query or value, then verify. "
            "Calculator: put the expression in query (generic keyboard path). "
            "Unknown apps: inspect the UI and click, or capture the window and "
            "click visible text. Newly installed apps are resolved live."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "app": {"type": "string", "maxLength": 80},
                "action": {
                    "type": "string",
                    "enum": [
                        "play",
                        "play_playlist_track",
                        "pause",
                        "next",
                        "previous",
                        "status",
                        "find_playlist",
                        "list_tracks",
                        "list_playlists",
                        "search",
                        "navigate",
                        "create",
                        "append",
                        "read",
                        "open_item",
                        "open_folder",
                        "new_tab",
                        "close_tab",
                        "next_tab",
                        "previous_tab",
                    ],
                },
                "playlist": {"type": "string", "maxLength": 120},
                "index": {"type": "integer", "minimum": -1, "maximum": 500},
                "query": {"type": "string", "maxLength": 2000},
                "value": {"type": "string", "maxLength": 4000},
                "text": {"type": "string", "maxLength": 4000},
                "track": {"type": "string", "maxLength": 200},
            },
            "required": ["action"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "apps:act",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "computer",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
        "required_scopes": ["apps:act"],
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
        "risk_class": "R0",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
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
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "public",
        "provider": "local",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "cooperative",
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
                "confirm": {"type": "boolean", "default": False},
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
        "name": "look",
        "description": (
            "Obtain a current visual observation from the owner's MacBook camera "
            "when answering requires seeing the physical scene: the room, a "
            "person, what they are holding, a selfie, or something in front of "
            "the camera. Do not use this for the Mac screen, a window, the "
            "desktop, or which app is open — call computer for those. Do not "
            "guess. Do not claim you cannot see if this function is listed. Do "
            "not pass a permission argument. Owner visual perception is already "
            "authorized. If they say open the camera and remember what they are "
            "showing, or name an item in their hand, call look — not computer, "
            "not place_call, not Photo Booth. Use capture_photo to take and save a still of the room. "
            "Use record_video to save a clip. Do not open the Camera app for "
            "those jobs. After look returns, describe the attached image in "
            "natural speech — people, clothing, pose, objects, colors, and "
            "setting — not a label list. If they asked you to memorize or remember "
            "what they are showing, this look is stored across app restarts; say "
            "you will remember it and never say you cannot guarantee future recall."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prompt": {"type": "string", "maxLength": 400, "default": None},
                "attachment_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "default": None,
                },
                "focus": {
                    "type": "string",
                    "enum": ["auto", "text", "objects", "people"],
                    "default": "auto",
                },
                "detail": {
                    "type": "string",
                    "enum": ["auto", "low", "high"],
                    "default": "high",
                },
            },
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": True,
        "read_only": True,
        "permission": "vision:read",
        "undoable": False,
        "risk_class": "R1",
        "confirmation": "none",
        "provider": "vision",
        "fallback": "report unavailable; do not fabricate seeing the room",
        "evidence": ["source", "timestamp"],
    },
    {
        "name": "observe_camera",
        "description": (
            "Watch the owner's MacBook camera for a few seconds when a single "
            "frame is not enough (an LED changing, motion, turning something, "
            "swapping an object). Describe people, objects, and colors in each "
            "frame and what changed. Duration is bounded. Do not use this for "
            "ordinary one-frame look questions."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "duration_seconds": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 8,
                    "default": 4,
                },
                "objective": {"type": "string", "maxLength": 400, "default": None},
                "strategy": {
                    "type": "string",
                    "enum": ["interval", "change"],
                    "default": "interval",
                },
                "detail": {
                    "type": "string",
                    "enum": ["auto", "low", "high"],
                    "default": "high",
                },
            },
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": True,
        "read_only": True,
        "permission": "vision:read",
        "undoable": False,
        "risk_class": "R1",
        "confirmation": "none",
        "provider": "vision",
        "fallback": "report unavailable; do not fabricate a live video stream",
        "evidence": ["source", "timestamp"],
    },
    {
        "name": "capture_photo",
        "description": (
            "Take and save a still photo from the owner's MacBook camera. Use "
            "this when they ask to capture themselves, take a picture, snap a "
            "photo, or save a still. This is not look (describe only) and not "
            "record_video. Do not open the Camera app. After it returns, "
            "describe the photo you took, then mention where it was saved."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prompt": {"type": "string", "maxLength": 400, "default": None},
                "detail": {
                    "type": "string",
                    "enum": ["auto", "low", "high"],
                    "default": "high",
                },
            },
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": True,
        "read_only": False,
        "permission": "vision:read",
        "undoable": False,
        "risk_class": "R1",
        "confirmation": "none",
        "provider": "vision",
        "fallback": "report unavailable; do not fabricate a saved photo",
        "evidence": ["source", "timestamp", "saved_path"],
    },
    {
        "name": "record_video",
        "description": (
            "Record and save a short video from the owner's MacBook camera. Use "
            "this when they ask to record a video, film something, or capture a "
            "clip. This is not look and not observe_camera. Duration is bounded. "
            "Do not open the Camera app. After it returns, describe the recorded "
            "clip from the attached frames (people, clothing, motion), then "
            "mention where it was saved."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "duration_seconds": {
                    "type": "number",
                    "minimum": 2,
                    "maximum": 30,
                    "default": 8,
                },
                "prompt": {"type": "string", "maxLength": 400, "default": None},
                "detail": {
                    "type": "string",
                    "enum": ["auto", "low", "high"],
                    "default": "high",
                },
            },
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": True,
        "read_only": False,
        "permission": "vision:read",
        "undoable": False,
        "risk_class": "R1",
        "confirmation": "none",
        "provider": "vision",
        "fallback": "report unavailable; do not fabricate a saved recording",
        "evidence": ["source", "timestamp", "saved_path"],
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
        "risk_class": "R0",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
        "evidence": ["source", "timestamp"],
        "idempotency": "natural",
        "cancellation": "not_applicable",
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
                "to": {"type": "string", "maxLength": 256, "default": None},
                "subject": {"type": "string", "maxLength": 512, "default": None},
            },
            "required": ["mail_id"],
        },
        "output": {"type": "object", "required": ["spoken"]},
        "sensitive": True,
        "read_only": False,
        "permission": "mail:write",
        "undoable": True,
        "risk_class": "R2",
        "confirmation": "standing",
        "target_ownership": "owner",
        "provider": "local",
        "evidence": ["source", "timestamp"],
        "idempotency": "key",
        "cancellation": "timeout",
        "required_scopes": ["mail:write"],
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
        "risk_class": "R3",
        "confirmation": "fresh",
        "target_ownership": "owner",
        "provider": "drone",
        "evidence": ["source", "timestamp"],
        "idempotency": "none",
        "cancellation": "required",
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
    {
        "name": "life_project_create",
        "description": "Create a persistent project (an area of coordinated work, e.g. Personal Fitness, Evie).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 256},
                "priority": {"type": "string", "enum": ["CRITICAL", "HIGH", "NORMAL", "LOW"]},
                "description": {"type": "string", "maxLength": 4000},
            },
            "required": ["title"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "life:state",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
    },
    {
        "name": "life_project_update",
        "description": "Update a project's status, priority, title, or description. Reference by id or exact title.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "minLength": 1, "maxLength": 256},
                "status": {"type": "string", "enum": ["ACTIVE", "PAUSED", "COMPLETED", "ARCHIVED"]},
                "priority": {"type": "string", "enum": ["CRITICAL", "HIGH", "NORMAL", "LOW"]},
                "title": {"type": "string", "maxLength": 256},
                "description": {"type": "string", "maxLength": 4000},
            },
            "required": ["project"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "life:state",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
    },
    {
        "name": "life_project_query",
        "description": "Query projects. Optional title filter, priority filter.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {"type": "string", "maxLength": 256},
                "priority": {"type": "string", "enum": ["CRITICAL", "HIGH", "NORMAL", "LOW"]},
                "include_completed": {"type": "boolean", "default": False},
            },
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "life:read",
        "risk_class": "R0",
        "confirmation": "none",
        "provider": "local",
    },
    {
        "name": "life_goal_create",
        "description": "Create a goal (an outcome to accomplish). Optionally link to a project by id or title.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 512},
                "project": {"type": "string", "maxLength": 256},
                "priority": {"type": "string", "enum": ["CRITICAL", "HIGH", "NORMAL", "LOW"]},
                "success_criteria": {"type": "string", "maxLength": 4000},
            },
            "required": ["title"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "life:state",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
    },
    {
        "name": "life_goal_update",
        "description": "Update goal state (ACTIVE/BLOCKED/COMPLETED/CANCELLED/PAUSED/PLANNED), progress note, next action, or blocked reason.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "goal_id": {"type": "string"},
                "title_query": {"type": "string", "maxLength": 512},
                "state": {"type": "string", "enum": ["PLANNED", "ACTIVE", "BLOCKED", "PAUSED", "COMPLETED", "CANCELLED"]},
                "progress_note": {"type": "string", "maxLength": 2000},
                "next_action": {"type": "string", "maxLength": 2000},
                "blocked_reason": {"type": "string", "maxLength": 2000},
                "priority": {"type": "string", "enum": ["CRITICAL", "HIGH", "NORMAL", "LOW"]},
            },
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "life:state",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
    },
    {
        "name": "life_goal_add_step",
        "description": "Add a concrete step (subordinate work item) to a goal, referenced by goal_id or exact title.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "goal_id": {"type": "string"},
                "title_query": {"type": "string", "maxLength": 512},
                "title": {"type": "string", "minLength": 1, "maxLength": 512},
            },
            "required": ["title"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "life:state",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
    },
    {
        "name": "life_goal_query",
        "description": "Query goals. Optional state filter, project filter, or title substring filter.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "state": {"type": "string", "enum": ["PLANNED", "ACTIVE", "BLOCKED", "PAUSED", "COMPLETED", "CANCELLED"]},
                "project": {"type": "string", "maxLength": 256},
                "goal_id": {"type": "string"},
                "title": {"type": "string", "maxLength": 512},
                "title_query": {"type": "string", "maxLength": 512},
                "query": {"type": "string", "maxLength": 512},
            },
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "life:read",
        "risk_class": "R0",
        "confirmation": "none",
        "provider": "local",
    },
    {
        "name": "life_commitment_create",
        "description": "Record a commitment (a promise/obligation with optional due date).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "description": {"type": "string", "minLength": 1, "maxLength": 2000},
                "due_at": {"type": "string", "maxLength": 128},
                "project": {"type": "string", "maxLength": 256},
            },
            "required": ["description"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "life:state",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
    },
    {
        "name": "life_commitment_update",
        "description": "Update a commitment's status (FULFILLED/CANCELLED/MISSED). Reference by id or description substring.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "commitment_id": {"type": "string"},
                "description": {"type": "string", "maxLength": 2000},
                "status": {"type": "string", "enum": ["FULFILLED", "CANCELLED", "MISSED"]},
            },
            "required": ["status"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "life:state",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
    },
    {
        "name": "life_commitment_query",
        "description": "Query commitments. Optional description substring, project, or status filter. Returns due dates.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "maxLength": 512},
                "description": {"type": "string", "maxLength": 512},
                "project": {"type": "string", "maxLength": 256},
                "status": {"type": "string", "enum": ["OPEN", "FULFILLED", "CANCELLED", "MISSED"]},
                "include_completed": {"type": "boolean", "default": False},
            },
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": True,
        "permission": "life:read",
        "risk_class": "R0",
        "confirmation": "none",
        "provider": "local",
    },
    {
        "name": "life_relationship_set",
        "description": "Record or update the owner's relationship to a person (friend/family/partner/parent/sibling/child/colleague/classmate/professional_contact/other). Explicit only.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "person": {"type": "string", "minLength": 1, "maxLength": 256},
                "relation": {"type": "string", "enum": ["friend", "family", "partner", "parent", "sibling", "child", "colleague", "classmate", "professional_contact", "other"]},
            },
            "required": ["person", "relation"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "life:state",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
    },
    {
        "name": "mission_control",
        "description": "Mission Control v0.1: situation summary (top priority, active goals, blockers, commitments) and/or what-changed query. query='status' is a pure read. query='changes' returns changes since the owner's last check and advances that checkpoint; pass an explicit 'since' for a pure historical window.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "enum": ["status", "changes"], "default": "status"},
                "since": {"type": "string", "maxLength": 64},
            },
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "undoable": True,
        "permission": "life:read",
        "risk_class": "R1",
        "confirmation": "none",
        "provider": "local",
    },
    {
        "name": "evie_turn",
        "description": "Evie's high-level turn controller (G1.3). Call this for EVERY owner turn that may involve projects, goals, commitments, status, what-changed, or any canonical state. Luna interprets, Evie Core owns truth. Use the canonical owner transcript text; do NOT paraphrase.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "owner_turn": {"type": "string", "minLength": 1, "maxLength": 2000, "description": "Canonical owner transcript text (owner speech only, final)"},
                "turn_id": {"type": "string", "maxLength": 128, "description": "Optional canonical turn ID referencing durable transcript"},
                "session_id": {"type": "string", "maxLength": 128},
            },
            "required": ["owner_turn"],
        },
        "output": {"type": "object"},
        "sensitive": False,
        "read_only": False,
        "permission": "life:state",
        "undoable": True,
        "risk_class": "R1",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
    },
    # --- EV VOICE CONTROL PLAN (foundation, additive) --------------------------
    # Past/history retrieval, deliberately separate from event/reminder tools.
    # See docs/VOICE_CONTROL_PLAN.md §2.
    {
        "name": "recall_history",
        "description": (
            "Retrieve the owner's PAST history: decisions, preferences, goals, "
            "facts, observations, lessons, patterns, and what was said or "
            "decided before. Use ONLY for the past (what they did, decided, "
            "preferred, or thought, with time context like in March, last month, "
            "when did I, back in 2026). Also use for contacts, notes, tasks, "
            "mail subjects, photos by date or album, people Evie knows, "
            "WhatsApp threads, and iCloud Drive notes; those return a small "
            "evidence pack, never the whole archive. If a SHADOW MEMORY block is "
            "already on this turn, answer from it first. For FUTURE reminders, alerts, calendar, "
            "or timers use get_upcoming_alerts or calendar_read instead. "
            "Results are chunked evidence with provenance and scores; if count "
            "is zero, say you cannot find that particular record — never that "
            "you have no history with them — and do not say that when SHADOW "
            "MEMORY, the relationship card, or this tool already returned "
            "matching lines. Pass a page cursor to fetch "
            "the next chunk page."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 500,
                    "description": "What to recall, e.g. 'why did I choose SQLite'.",
                },
                "k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 8,
                    "description": "Chunk page size; 8 is optimal for voice.",
                },
                "time_range": {
                    "type": "string",
                    "enum": [
                        "all_time",
                        "recent_week",
                        "recent_month",
                        "last_3_months",
                        "last_year",
                    ],
                    "default": "all_time",
                },
                "start_date": {
                    "type": "string",
                    "description": "ISO date (2026-03-01) for the start of a custom window.",
                },
                "end_date": {
                    "type": "string",
                    "description": "ISO date (2026-03-31) for the end of a custom window.",
                },
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
                    "description": "Only return chunks of this type.",
                },
                "as_of": {
                    "type": "string",
                    "description": "ISO date-time; version-window time travel (what was true then).",
                },
                "chunk_mode": {
                    "type": "string",
                    "enum": ["brief", "full"],
                    "default": "brief",
                    "description": "brief = voice-fast chunks; full = complete text.",
                },
                "cursor": {
                    "type": "string",
                    "description": "Opaque next_cursor from a previous call to fetch the next page.",
                },
            },
            "required": ["query"],
        },
        "output": {"type": "object", "required": ["count", "results"]},
        "sensitive": False,
        "read_only": True,
        "risk_class": "R0",
        "permission": "memory:read",
        "undoable": False,
    },
    # UI-specific verbs: read/see/click/type/... control ANY app by query, not
    # per-app verbs. See docs/VOICE_CONTROL_PLAN.md §4. These route onto the
    # existing computer primitives (inspect_ui / screen_look / ui_action).
    {
        "name": "read",
        "description": (
            "Read the current UI of the front Mac app (Accessibility tree) and "
            "return compact element refs like e12_1. Call this before clicking "
            "or typing anything on the Mac. query narrows to a control (Bluetooth, "
            "Empty Trash, lofi); omit query for the whole snapshot."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "maxLength": 200},
                "level": {
                    "type": "string",
                    "enum": ["summary", "targeted", "expanded"],
                    "default": "summary",
                },
                "app": {"type": "string", "maxLength": 80},
            },
        },
        "sensitive": False,
        "read_only": True,
        "risk_class": "R0",
        "permission": "apps:act",
        "undoable": False,
        "target_ownership": "owner",
        "provider": "computer",
    },
    {
        "name": "see",
        "description": (
            "Capture the front window (or a display) as an image when Accessibility "
            "cannot answer (canvas apps, Chrome, Figma). Describe only what you "
            "can actually see in the provided frame."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["active_window", "app", "display"],
                    "default": "active_window",
                },
                "app": {"type": "string", "maxLength": 80},
            },
        },
        "sensitive": False,
        "read_only": True,
        "risk_class": "R1",
        "permission": "apps:act",
        "undoable": False,
        "target_ownership": "owner",
        "provider": "computer",
    },
    {
        "name": "click",
        "description": (
            "Click a UI element returned by read (pass ref) or at normalized "
            "coordinates on a recent see frame (action click_at + frame_id + x + y). "
            "Never claim a click happened unless the result verifies it."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "ref": {"type": "string", "maxLength": 24, "description": "Element ref from read."},
                "action": {
                    "type": "string",
                    "enum": ["press", "click", "click_at"],
                    "default": "press",
                },
                "frame_id": {"type": "string", "maxLength": 40, "description": "Required for click_at."},
                "x": {"type": "number", "description": "Normalized 0-1 x for click_at."},
                "y": {"type": "number", "description": "Normalized 0-1 y for click_at."},
            },
        },
        "sensitive": False,
        "read_only": False,
        "risk_class": "R1",
        "permission": "apps:act",
        "undoable": True,
        "target_ownership": "owner",
        "provider": "computer",
    },
    {
        "name": "double_click",
        "description": "Double-click a UI element returned by read (opens files and folders).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "ref": {"type": "string", "minLength": 1, "maxLength": 24},
            },
            "required": ["ref"],
        },
        "sensitive": False,
        "read_only": False,
        "risk_class": "R1",
        "permission": "apps:act",
        "undoable": True,
        "target_ownership": "owner",
        "provider": "computer",
    },
    {
        "name": "right_click",
        "description": "Open the context menu for a UI element returned by read.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "ref": {"type": "string", "minLength": 1, "maxLength": 24},
            },
            "required": ["ref"],
        },
        "sensitive": False,
        "read_only": False,
        "risk_class": "R1",
        "permission": "apps:act",
        "undoable": True,
        "target_ownership": "owner",
        "provider": "computer",
    },
    {
        "name": "type",
        "description": (
            "Type text into the focused field, or into the ref returned by read. "
            "mode type inserts at the caret; append adds; replace overwrites."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                "ref": {"type": "string", "maxLength": 24},
                "mode": {
                    "type": "string",
                    "enum": ["type", "append", "replace"],
                    "default": "type",
                },
            },
            "required": ["text"],
        },
        "sensitive": False,
        "read_only": False,
        "risk_class": "R1",
        "permission": "apps:act",
        "undoable": True,
        "target_ownership": "owner",
        "provider": "computer",
    },
    {
        "name": "paste",
        "description": (
            "Paste into the focused field or a ref from read. Omit text to paste "
            "the current clipboard (cmd+v). Pass text to type that string via paste."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                "ref": {"type": "string", "maxLength": 24},
            },
        },
        "sensitive": False,
        "read_only": False,
        "risk_class": "R1",
        "permission": "apps:act",
        "undoable": True,
        "target_ownership": "owner",
        "provider": "computer",
    },
    {
        "name": "key",
        "description": (
            "Press a keyboard hotkey or key: cmd+space, cmd+s, enter, esc, tab. "
            "This opens Spotlight, confirms dialogs, and operates menus in any app."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "keys": {"type": "string", "minLength": 1, "maxLength": 64},
                "ref": {"type": "string", "maxLength": 24},
            },
            "required": ["keys"],
        },
        "sensitive": False,
        "read_only": False,
        "risk_class": "R1",
        "permission": "apps:act",
        "undoable": True,
        "target_ownership": "owner",
        "provider": "computer",
    },
    {
        "name": "scroll",
        "description": "Scroll the front window (or a ref from read) in a direction.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                    "default": "down",
                },
                "ref": {"type": "string", "maxLength": 24},
            },
        },
        "sensitive": False,
        "read_only": False,
        "risk_class": "R1",
        "permission": "apps:act",
        "undoable": True,
        "target_ownership": "owner",
        "provider": "computer",
    },
    {
        "name": "drag",
        "description": (
            "Drag a ref from read to normalized x/y on a recent see frame "
            "(frame_id required). Real mouse-down, move, mouse-up. Refuses "
            "honestly when frame_id is missing; never guesses coordinates."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "ref": {"type": "string", "minLength": 1, "maxLength": 24},
                "frame_id": {"type": "string", "minLength": 1, "maxLength": 40},
                "x": {"type": "number", "description": "Normalized 0-1 destination x."},
                "y": {"type": "number", "description": "Normalized 0-1 destination y."},
            },
            "required": ["ref", "frame_id", "x", "y"],
        },
        "sensitive": False,
        "read_only": False,
        "risk_class": "R1",
        "permission": "apps:act",
        "undoable": True,
        "target_ownership": "owner",
        "provider": "computer",
    },
    *FLEET_TOOL_SPECS,
]


_LIFE_BRIDGES: dict[str, tuple[str, str, str]] = {
    "resolve_contact": ("contacts", "contacts.resolve", "contacts:read"),
    "create_contact": ("contacts", "contacts.create", "contacts:act"),
    "save_contact": ("contacts", "contacts.create", "contacts:act"),
    "update_contact": ("contacts", "contacts.update", "contacts:act"),
    "send_message": ("messaging", "messaging.send", "messaging:act"),
    "list_messages": ("messaging", "messaging.list_messages", "messaging:read"),
    "place_call": ("phone", "phone.call", "phone:act"),
    "list_mail": ("mail", "mail.list", "mail:read"),
    "send_mail": ("mail", "mail.send", "mail:act"),
    "send_email": ("mail", "mail.send", "mail:act"),
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
    """Return the declared spec for a tool or action name.

    ``list_tools()`` stays the model catalog. Action-only names such as
    ``execute_command`` resolve here so HUD resume and ``/v1/tools/execute``
    share ``dispatch`` / ``evaluate_policy`` without a fourth registry.
    """
    if name in UI_VERB_TOOLS and not _ui_verbs_enabled():
        # EV VOICE CONTROL PLAN kill-switch: disabled UI verbs resolve as
        # unknown so policy never admits them.
        return None
    from app.ev.policy import annotate_spec

    for spec in TOOL_SPECS:
        if spec["name"] == name:
            resolved = dict(spec)
            resolved["sensitive"] = _resolved_sensitive(spec)
            return annotate_spec(resolved)
    from app.ev.actions import get_action_spec

    action = get_action_spec(name)
    if action is None:
        return None
    resolved = dict(action)
    resolved.setdefault("parameters", action.get("payload") or {"type": "object"})
    resolved["sensitive"] = True
    return annotate_spec(resolved)


def _ui_verbs_enabled() -> bool:
    """EV VOICE CONTROL PLAN: kill-switch for the UI verb family."""

    from app.config import settings

    return bool(getattr(settings, "ui_verb_tools_enabled", True))


def list_tools() -> list[dict]:
    from app.ev.policy import annotate_spec

    return [
        annotate_spec({**spec, "sensitive": _resolved_sensitive(spec)})
        for spec in TOOL_SPECS
        if not (spec.get("name") in UI_VERB_TOOLS and not _ui_verbs_enabled())
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
        if name == "code":
            return (
                f"I couldn't finish that coding job. {next_step}"
                if next_step
                else "I couldn't finish that coding job."
            )
        return f"I couldn't finish that yet. {next_step}".strip()

    if name == "set_reminder":
        text = str(payload.get("text") or payload.get("reminder") or "that").strip()
        return f"Reminder set: {text}."
    if name == "code":
        files = [str(item) for item in (payload.get("files_changed") or []) if item]
        folder = str(payload.get("workspace") or payload.get("project") or "the coding workspace")
        if files:
            return f"I wrote {', '.join(files[:4])} in {folder}."
        return "I finished that coding job."
    if name == "present" or "opened" in payload:
        if payload.get("opened"):
            return "Opened that on your screen."
        reason = next_step or str(payload.get("reason") or "").strip()
        return f"I couldn't open that on screen yet. {reason}".strip()

    if name in {"send_mail", "send_email"} or payload.get("action") == "mail.send":
        recipient = str(payload.get("to") or payload.get("recipient") or "recipient")
        return f"Sent email to {recipient}."

    if name in {"save_contact", "create_contact", "update_contact"} or payload.get("action") in {"contacts.create", "contacts.update"}:
        c = payload.get("contact") if isinstance(payload.get("contact"), dict) else payload
        cname = str(c.get("full_name") or c.get("name") or payload.get("name") or "the contact")
        return f"Saved contact for {cname}."

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


def _ROUTER_SET() -> frozenset[str]:
    """Lazy router set import (avoids cycles at module import time)."""

    from app.ev.capability_router import ROUTER_TOOLS

    return ROUTER_TOOLS


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
    channel: str | None = None,
    confirmation=None,
    live_session_id: str | None = None,
    audit_endpoint: str = "POST /v1/gateway/tools",
) -> ToolCallResponse:
    """Validate, authorize, execute, shape-check, and log one tool invocation."""

    from app.ev.delegates import scope_blocked
    from app.ev.policy import (
        IGNORED_ARGUMENT_KEYS,
        Confirmation,
        attach_evidence,
        authorize,
        canonical_target,
        derive_risk_class,
        infer_channel,
        not_connected_payload,
        should_enforce,
    )
    from app.ev.training_wheels import ensure_seed_gates, refuse_if_locked
    from app.ev.voice_life import WEAPON_RE, consume_life_reverify

    started = time.perf_counter()
    # Confidence is model metadata, not a capability argument. Keep it out of
    # the adapter/schema path as well as out of authorization decisions.
    dispatch_arguments = {
        key: value
        for key, value in dict(arguments or {}).items()
        if key not in IGNORED_ARGUMENT_KEYS
    }
    spec = get_spec(name)
    if spec is not None and name in {
        "look",
        "observe_camera",
        "capture_photo",
        "record_video",
    }:
        from app.ev.camera_runtime import coerce_vision_arguments

        dispatch_arguments = coerce_vision_arguments(
            name,
            dispatch_arguments,
            ((spec.get("parameters") or {}).get("properties") or {}),
        )
    status = "ok"
    error: str | None = None
    result: dict | None = None
    await ensure_seed_gates(session)
    auth_channel = infer_channel(actor, channel)

    if spec is not None and name == "actuate":
        spec = dict(spec)
        spec["permission"] = actuate_permission(str((dispatch_arguments or {}).get("verb") or ""))

    weapon_text = ""
    if name == "actuate":
        weapon_text = str((dispatch_arguments or {}).get("verb") or "")
    elif name == "drone":
        weapon_text = str((dispatch_arguments or {}).get("command") or "")

    policy_confirmation = confirmation
    # ``allow_sensitive`` is the existing explicit-permission switch used by
    # the gateway for bounded sensitive reads. Treat it as an explicit R2
    # approval for the owner, but never as the independent fresh factor needed
    # by R3/R4 capabilities.
    if (
        policy_confirmation is None
        and allow_sensitive
        and actor in {"master", "owner"}
        and spec is not None
        and derive_risk_class(spec, name) == "R2"
    ):
        issued_at = utcnow()
        policy_confirmation = Confirmation(
            factor="http_approve",
            confirmed=True,
            target=canonical_target(name, dispatch_arguments),
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=120),
        )

    decision = await authorize(
        session,
        name,
        actor=actor,
        arguments=dispatch_arguments,
        device_id=device_id,
        channel=auth_channel,
        confirmation=policy_confirmation,
        reverify_token=reverify_token,
        spec=spec,
        session_id=live_session_id,
    )

    if spec is None and decision.effect != "refuse":
        status = "error"
        error = f"Unknown tool '{name}'"
    elif weapon_text and WEAPON_RE.search(weapon_text):
        status = "denied"
        error = "refused"
        result = {
            "ok": False,
            "error": "refused",
            "refused": "weapons",
            "spoken": "I will not run kill or weapon verbs.",
        }
    else:
        bio = None
        refuse = None
        scoped = None
        if spec is not None:
            bio = await consume_life_reverify(
                session,
                actor=actor,
                device_id=device_id,
                reverify_token=reverify_token,
                name=name,
                args=dispatch_arguments,
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
        # Keep the existing explicit-sensitive permission boundary visible to
        # callers. A standing/R2 confirmation decision is not permission to
        # execute a sensitive tool when the request did not opt into it.
        elif (
            spec is not None
            and spec["sensitive"]
            and not allow_sensitive
            and decision.effect in {"allow", "confirm"}
        ):
            status = "denied"
            error = f"Permission denied: '{name}' requires explicit permission before execution"
        elif should_enforce(decision, name=name, channel=auth_channel) and not decision.allowed:
            result = (
                not_connected_payload(decision)
                if decision.effect == "not_connected"
                else decision.to_result()
            )
            if decision.effect == "reject":
                status = "error"
                error = f"Unknown tool '{name}'"
            elif decision.effect == "confirm":
                status = "denied"
                error = "confirmation_required"
                if decision.independent_confirmation:
                    result = await _park_independent_hold(
                        session,
                        name=name,
                        arguments=dispatch_arguments,
                        decision=decision,
                        actor=actor,
                        device_id=device_id,
                        live_session_id=live_session_id,
                        request_id=request_id,
                        channel=auth_channel,
                    )
                else:
                    from app.voice.live.layer import hold_result

                    result = hold_result(decision, name=name, arguments=dispatch_arguments)
            elif decision.effect == "not_connected":
                status = "ok"
                error = None
                result = not_connected_payload(decision)
            elif decision.effect == "refuse":
                status = "denied"
                error = "refused"
            else:
                status = "denied"
                error = decision.reason
        elif spec is not None:
            effective, issues = validate_arguments(dispatch_arguments, spec["parameters"])
            if issues:
                status = "rejected"
                error = "Invalid arguments: " + "; ".join(issues)
            else:
                # F3 capability router (flag: off | shadow | on). SHADOW only
                # predicts + records; legacy stays authoritative. ON adds a
                # fenced generic-computer fallback for app navigation when the
                # semantic path failed BEFORE any dispatch.
                router_outcome: dict | None = None
                _router_mode = "off"
                if name in _ROUTER_SET():
                    from app.ev.capability_router import (
                        ActionGoal,
                        Rationale,
                        RouteKind,
                        note_route_outcome,
                        route_action,
                        router_mode,
                    )

                    _router_mode = router_mode()
                    if _router_mode != "off":
                        goal = ActionGoal(
                            goal=str(effective.get("goal") or effective.get("name") or effective.get("query") or name),
                            owner_turn_id=str(live_session_id or request_id or ""),
                            actor=actor,
                            device_scope="owner" if actor in {"master", "owner"} else str(actor),
                            target=name,
                            arguments=dict(effective),
                        )
                        route = await route_action(goal, session=session)
                        router_outcome = {
                            "route": route,
                            "goal": goal,
                            "fallback": None,
                        }
                        if _router_mode == "shadow":
                            # Predict-only; legacy executes as always.
                            pass
                try:
                    result = await _handle(
                        session,
                        name,
                        effective,
                        actor=actor,
                        live_session_id=live_session_id,
                        device_id=device_id,
                        request_id=request_id,
                        channel=auth_channel,
                    )
                    if _router_mode == "on" and router_outcome is not None:
                        route = router_outcome["route"]
                        degraded = isinstance(result, dict) and (
                            result.get("degraded") or result.get("error") == "not_connected"
                        )
                        if (
                            route.route_kind == RouteKind.GENERIC_COMPUTER
                            or (degraded and route.rationale_code == Rationale.SEMANTIC_ADAPTER_AVAILABLE
                                and name in {"open_app", "activate_app"})
                        ) and degraded:
                            # Semantic path failed BEFORE dispatch (not_connected):
                            # fenced generic-computer fallback is legal.
                            from app.ev.computer_executor import execute_tool_via_executor
                            from app.voice.live.layer import live_for_device, live_for_session

                            live = live_for_session(str(live_session_id) if live_session_id else None) or (
                                live_for_device(str(device_id)) if device_id else None
                            )
                            exec_result = await execute_tool_via_executor(
                                name, effective, live=live, actor=actor,
                            ) if live is not None else None
                            if exec_result is not None and exec_result.raw.get("ok"):
                                from app.ev.computer import _shape_lifecycle, stamp_computer_receipt

                                shaped = _shape_lifecycle(
                                    name, effective, exec_result.raw, source="capability_router"
                                )
                                shaped = stamp_computer_receipt(
                                    shaped, None, name=name,
                                    executed=exec_result.executed,
                                    verified=exec_result.verified,
                                    request_id=request_id,
                                )
                                result = shaped
                                router_outcome["fallback"] = "before_dispatch"
                            elif exec_result is not None:
                                router_outcome["fallback"] = (
                                    "before_dispatch" if exec_result.fallback_allowed else "forbidden"
                                )
                    if router_outcome is not None:
                        from app.ev.capability_router import note_route_outcome

                        note_route_outcome(
                            execution_id=str(request_id or ""),
                            attempted=bool(isinstance(result, dict) and result.get("executed")),
                            verified=bool(isinstance(result, dict) and result.get("verified")),
                            error=str((result or {}).get("error")) if isinstance(result, dict) else None,
                            fallback=router_outcome.get("fallback"),
                        )
                    if decision.routed:
                        if isinstance(result, dict) and (
                            result.get("degraded") or result.get("error") == "not_connected"
                        ):
                            result = {**result, "error": "not_connected", "degraded": True}
                        else:
                            result = attach_evidence(result, decision)
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
        endpoint=audit_endpoint,
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
            "risk_class": decision.risk_class,
            "policy_effect": decision.effect,
            "channel": auth_channel,
            "confirmation_required": decision.confirmation_required,
            "confirmation_policy": decision.confirmation_policy,
            "confirmation_factor": getattr(policy_confirmation, "factor", None),
            "provider": decision.provider,
            "target": decision.target,
            "device_id": str(device_id) if device_id else None,
            "live_session_id": live_session_id,
            "argument_keys": sorted(dispatch_arguments),
            "result": {
                "ok": result.get("ok") if isinstance(result, dict) else None,
                "error": result.get("error") if isinstance(result, dict) else error,
                "evidence": result.get("evidence") if isinstance(result, dict) else None,
            },
        },
    )
    return response


async def _park_independent_hold(
    session: AsyncSession,
    *,
    name: str,
    arguments: dict,
    decision,
    actor: str,
    device_id=None,
    live_session_id: str | None,
    request_id: str | None,
    channel: str,
) -> dict:
    """Park a HUD ticket and keep the realtime loop alive. Never waits."""

    from app.ev.confirm import attach_hold_to_live, park_confirmation, pol_meta
    from app.voice.live.layer import hold_result

    parked = await park_confirmation(
        session,
        name=name,
        arguments=arguments,
        decision=decision,
        actor=actor,
        device_id=device_id,
        live_session_id=live_session_id,
        request_id=request_id,
        channel=channel,
    )
    payload = hold_result(decision, name=name, arguments=arguments)
    payload["action_id"] = str(parked.id)
    meta = pol_meta(parked.payload)
    if meta.get("expires_at"):
        payload["expires_at"] = meta["expires_at"]
    hud = payload.get("hud")
    if isinstance(hud, dict):
        card_meta = dict(hud.get("meta") or {})
        card_meta["action_id"] = str(parked.id)
        if meta.get("expires_at"):
            card_meta["expires_at"] = meta["expires_at"]
        hud["meta"] = card_meta
    try:
        from app.ev.workbench import cache_hud

        hud = payload.get("hud")
        if isinstance(hud, dict):
            payload["hud"] = await cache_hud(session, hud, source="approval_hold")
    except Exception:  # noqa: BLE001 - HUD must not block the hold
        pass
    await attach_hold_to_live(
        payload,
        live_session_id=live_session_id,
        device_id=device_id,
    )
    return payload


async def _run_execute_command(session: AsyncSession, args: dict, *, actor: str) -> dict:
    """R4 sandbox run with idempotent replay. Never a raw shell."""

    import asyncio

    from app.ev.actuator import (
        CALL_IDEMPOTENCY_TTL,
        evidence_base,
        fingerprint,
        prior_result,
        record_actuator,
        with_timeout,
    )
    from app.tools.operations import operation_for_command
    from app.tools.sandbox import SandboxError, run_command

    command = str(args.get("command") or "")
    operation = operation_for_command(command)
    if operation is None:
        return {
            "ok": False,
            "error": "operation_not_allowed",
            "spoken": "That software operation is not allowlisted.",
            "command": command,
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
    cwd = args.get("cwd")
    timeout = int(args.get("timeout_seconds") or 30)
    key = fingerprint("execute_command", command, cwd)
    replayed = await prior_result(
        session, name="execute_command", key=key, max_age=CALL_IDEMPOTENCY_TTL
    )
    if replayed is not None:
        return replayed

    async def _run() -> dict:
        return await asyncio.to_thread(
            run_command, command, cwd=cwd, timeout_seconds=timeout
        )

    try:
        timed = await with_timeout(_run(), seconds=float(timeout) + 1.0)
    except SandboxError as exc:
        failed = {
            "ok": False,
            "error": "sandbox",
            "spoken": str(exc),
            "command": command,
            "exit_code": -1,
            "stdout": "",
            "stderr": str(exc),
            "stdout_truncated": False,
            "stderr_truncated": False,
        }
        await record_actuator(
            session, name="execute_command", actor=actor, key=key, result=failed, target=command
        )
        return failed
    if isinstance(timed, dict) and timed.get("error") in {"timeout", "cancelled"}:
        await record_actuator(
            session, name="execute_command", actor=actor, key=key, result=timed, target=command
        )
        return timed
    raw = timed if isinstance(timed, dict) else {}
    ok = int(raw.get("exit_code", 1)) == 0
    result = {
        **raw,
        "ok": ok,
        "spoken": "Done." if ok else "That command failed inside the sandbox.",
        "evidence": evidence_base(
            source="sandbox",
            accepted=ok,
            observed=ok,
            command=command,
            exit_code=raw.get("exit_code"),
        ),
    }
    await record_actuator(
        session, name="execute_command", actor=actor, key=key, result=result, target=command
    )
    return result


async def _run_code_goal(
    session: AsyncSession,
    args: dict,
    *,
    actor: str,
    channel: str | None = None,
    live_session_id: str | None = None,
) -> dict:
    """Luna coding broker: goal in, verified files/runs out. Not a raw shell."""

    from app.ev.actuator import evidence_base, fingerprint, record_actuator
    from app.ev.luna_code import run_code_job

    goal = str(args.get("goal") or "").strip()
    if not goal:
        return {
            "ok": False,
            "degraded": True,
            "error": "missing_goal",
            "spoken": "Tell me what to write or run.",
        }
    key = fingerprint("code", goal, actor)
    result = await run_code_job(
        goal,
        actor=actor,
        channel=channel,
        session_key=str(live_session_id or "") or None,
    )
    ok = bool(result.get("ok"))
    shaped = {
        **result,
        "ok": ok,
        "spoken": str(result.get("spoken") or "")[:700],
        "evidence": evidence_base(
            source="luna_code",
            accepted=ok,
            observed=ok,
            files=result.get("files_changed"),
            brain=result.get("brain"),
        ),
    }
    await record_actuator(
        session, name="code", actor=actor, key=key, result=shaped, target=goal[:200]
    )
    return shaped


async def _run_computer_goal(
    session: AsyncSession,
    args: dict,
    *,
    actor: str,
    live_session_id: str | None,
    device_id=None,
    request_id: str | None = None,
) -> dict:
    """F4 `computer` tool: goal in, verified result out.

    The model states the GOAL; this handler routes it through the Capability
    Router (semantic path first, F2 executor fallback, planner for multi-step).
    Memory and canonical-state questions are REFUSED with a redirect so the
    `computer` surface never becomes an opaque do-anything (§6 law).
    """

    goal_text = str(args.get("goal") or "").strip()
    if not goal_text:
        return {"ok": False, "error": "missing_goal", "spoken": "What should I do on the Mac?"}

    from app.ev.laptop_files import (
        is_system_confirmation,
        looks_like_file_followup,
        resolve_file_computer_goal,
    )

    if is_system_confirmation(goal_text):
        return {
            "ok": True,
            "executed": False,
            "verified": False,
            "ignored": "system_confirmation",
            "spoken": "",
        }

    from app.ev.computer_runtime import ensure_state
    from app.ev.computer_strategy import (
        resolve_generic_computer_goal,
        resolve_in_app_computer_goal,
        resolve_screen_observation_goal,
        wants_first_on_page_item,
        wants_play_media,
    )

    state = ensure_state(live_session_id)
    orig = str(getattr(state, "original_owner_request", "") or "") if state is not None else ""
    # The owner's words win. Joining a model rewrite onto them glues leftovers
    # such as "youtube.com. Open" into the fake host youtube.com.open.
    route_text = orig or goal_text
    if orig and (wants_play_media(orig) or wants_first_on_page_item(orig)):
        route_text = orig
    if is_system_confirmation(orig):
        orig = ""
        route_text = goal_text

    from app.ev.luna_code import looks_like_code_request

    # Mini often calls computer for software work. The generic "write …"
    # matcher would type into the front app. Luna is the coding broker.
    code_goal = None
    if looks_like_code_request(orig):
        code_goal = orig
    elif looks_like_code_request(goal_text):
        code_goal = goal_text
    if code_goal:
        return await _run_code_goal(
            session,
            {"goal": code_goal},
            actor=actor,
            channel="voice" if (actor == "voice" or live_session_id) else "action",
            live_session_id=live_session_id,
        )

    last_path = str(args.get("last_path") or getattr(state, "last_file_path", None) or "").strip() or None
    if not last_path:
        from app.ev.desk_scene import referent_file_path

        found = referent_file_path()
        if found is not None:
            last_path = str(found)
            if state is not None:
                state.last_file_path = last_path
    file_goal = resolve_file_computer_goal(
        goal_text, args.get("target_app"), last_path=last_path
    )
    if file_goal is not None:
        route_text = goal_text
    elif orig and not looks_like_file_followup(goal_text, last_path=last_path):
        file_goal = resolve_file_computer_goal(
            orig, args.get("target_app"), last_path=last_path
        )
        if file_goal is not None:
            route_text = orig
    if file_goal is not None:
        from app.ev.computer import handle_computer_tool

        capability, inner_args = file_goal
        inner_args.setdefault("goal", route_text)
        if live_session_id:
            inner_args.setdefault("session_id", live_session_id)
        return await handle_computer_tool(
            session,
            capability,
            inner_args,
            actor=actor,
            live_session_id=live_session_id,
            device_id=device_id,
            request_id=request_id,
        )

    if looks_like_file_followup(goal_text, last_path=last_path):
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "must_continue": False,
            "goal_complete": True,
            "error": "file_referent_missing",
            "spoken": "I don't have that file in reach. Say add it to the note on the desktop.",
        }

    observation = resolve_screen_observation_goal(route_text, args.get("target_app"))
    if observation is not None:
        from app.ev.computer import handle_computer_tool

        capability, inner_args = observation
        inner_args.setdefault("goal", goal_text)
        return await handle_computer_tool(
            session,
            capability,
            inner_args,
            actor=actor,
            live_session_id=live_session_id,
            device_id=device_id,
            request_id=request_id,
        )

    from app.ev.computer_strategy import looks_like_web_research, web_search_query_from_text

    if looks_like_web_research(route_text):
        query = web_search_query_from_text(route_text) or goal_text
        return await _handle(
            session,
            "search_web",
            {"query": query, "limit": 5},
            actor=actor,
            live_session_id=live_session_id,
            device_id=device_id,
            request_id=request_id,
            channel=None,
        )

    in_app = resolve_in_app_computer_goal(route_text, args.get("target_app"))
    if in_app is None:
        in_app = resolve_generic_computer_goal(route_text, args.get("target_app"))
    if in_app is not None:
        from app.ev.computer import handle_computer_tool

        capability, inner_args = in_app
        inner_args.setdefault("goal", route_text)
        return await handle_computer_tool(
            session,
            capability,
            inner_args,
            actor=actor,
            live_session_id=live_session_id,
            device_id=device_id,
            request_id=request_id,
        )

    from app.ev.capability_router import RouteKind, goal_from_transcript, route_action

    goal = goal_from_transcript(
        goal_text,
        actor=actor,
        turn_id=str(live_session_id or request_id or "") or None,
    )
    if args.get("target_app") and not goal.arguments.get("name"):
        goal.arguments["name"] = str(args["target_app"])
    route = await route_action(goal, session=session)

    from app.ev.computer_strategy import looks_like_computer_task as _mac_goal

    if (route.route_kind == RouteKind.MEMORY or route.capability == "search_memory") and not _mac_goal(goal_text):
        return {
            "ok": False,
            "error": "not_a_computer_goal",
            "redirect": "recall",
            "spoken": "That's a memory question — let me recall it properly.",
        }
    if route.route_kind == RouteKind.CORE and not _mac_goal(goal_text):
        return {
            "ok": False,
            "error": "not_a_computer_goal",
            "redirect": "evie_turn",
            "spoken": "That's about your projects or commitments — handling it through your real state.",
        }

    # Execute the routed capability through the normal authorized pipeline.
    capability = route.capability
    inner_args = dict(goal.arguments)
    if goal.semantic_intent is None:
        # Unknown plane for a computer-surfaced goal: keep it in the computer
        # family rather than guessing a domain.
        from app.ev.computer_executor import executor_mode

        if executor_mode() == "off":
            return {
                "ok": False,
                "error": "computer_executor_disabled",
                "spoken": "Mac computer control is not enabled right now.",
            }
        from app.ev.computer import handle_computer_tool
        from app.ev.computer_strategy import look_should_use_screen

        fallback = (
            "screen_look"
            if look_should_use_screen(goal_text)
            else (
                "ui_action"
                if any(word in goal_text.lower() for word in ("click", "type", "press", "scroll"))
                else "open_app"
            )
        )
        return await handle_computer_tool(
            session,
            fallback,
            {**inner_args, "goal": goal_text},
            actor=actor,
            live_session_id=live_session_id,
            device_id=device_id,
        )
    if capability in {"open_app", "close_app", "activate_app", "list_apps", "computer_status", "ui_action", "app_action"}:
        from app.ev.computer import handle_computer_tool

        inner_args.setdefault("goal", goal_text)
        return await handle_computer_tool(
            session, capability, inner_args,
            actor=actor, live_session_id=live_session_id, device_id=device_id,
        )
    if capability == "search_web" and not str(inner_args.get("query") or "").strip():
        from app.ev.computer_strategy import web_search_query_from_text

        inner_args["query"] = web_search_query_from_text(goal_text) or goal_text
    from app.voice.live.layer import live_for_device, live_for_session

    live = live_for_session(str(live_session_id) if live_session_id else None) or (
        live_for_device(str(device_id)) if device_id else None
    )
    if live is None and capability in {
        "open_app", "close_app", "activate_app", "open_url", "list_apps",
        "inspect_ui", "screen_look", "computer_status", "app_action",
    }:
        return await handle_computer_tool(
            session, capability, inner_args,
            actor=actor, live_session_id=live_session_id, device_id=device_id,
        )
    return await dispatch(
        session,
        capability,
        inner_args,
        actor=actor,
        live_session_id=live_session_id,
        device_id=device_id,
        request_id=f"computer-{goal_text[:24]}",
        audit_endpoint="POST /v1/gateway/tools(computer)",
    )


async def _run_ui_sequence(
    session: AsyncSession,
    steps: list[tuple[str, dict[str, Any]]],
    *,
    actor: str,
    live_session_id: str | None,
    device_id,
    request_id: str | None,
    label: str,
) -> dict:
    """EV VOICE CONTROL PLAN: run a fixed sequence of computer primitives.

    Steps run strictly in order and stop at the first failure (a deliberate
    user-requested sequence such as double-click or drag — never a blind
    retry of a side-effecting action). Returns a truthful merged receipt.
    """

    from app.ev.computer import handle_computer_tool as _computer_handle

    outcomes: list[dict] = []
    for canonical, step_args in steps:
        outcome = await _computer_handle(
            session,
            canonical,
            step_args,
            actor=actor,
            live_session_id=live_session_id,
            device_id=str(device_id) if device_id else None,
            request_id=request_id,
        )
        outcome = outcome if isinstance(outcome, dict) else {"ok": False}
        outcomes.append(outcome)
        if not bool(outcome.get("ok")):
            break
    executed = any(bool(o.get("executed")) for o in outcomes)
    verified = bool(outcomes and outcomes[-1].get("verified"))
    ok = bool(outcomes) and all(bool(o.get("ok")) for o in outcomes)
    last_spoken = next(
        (str(o.get("spoken")) for o in reversed(outcomes) if o.get("spoken")), None
    )
    failed = next(
        (str(o.get("error")) for o in outcomes if not bool(o.get("ok"))), None
    )
    return {
        "ok": ok,
        "executed": executed,
        "verified": verified,
        "label": label,
        "steps": outcomes,
        "error": failed,
        "spoken": last_spoken
        or (f"{label}: completed and verified." if ok else f"{label} did not complete."),
    }


def _owner_visual_haystack(
    args: dict,
    live_session_id: str | None,
    device_id,
) -> str:
    parts = [
        str(args.get("prompt") or ""),
        str(args.get("objective") or ""),
        str(args.get("goal") or ""),
    ]
    try:
        from app.voice.live.layer import live_for_device, live_for_session

        live = live_for_session(live_session_id) or live_for_device(
            str(device_id) if device_id else None
        )
    except Exception:
        live = None
    grok = getattr(live, "grok_voice", None) if live is not None else None
    transcript = str(getattr(grok, "_last_input_transcript", "") or "")
    if transcript:
        parts.append(transcript)
    return " ".join(part for part in parts if part.strip())


async def _reroute_visual_to_files(
    session: AsyncSession,
    args: dict,
    *,
    actor: str,
    live_session_id: str | None,
    device_id,
    request_id: str | None,
) -> dict | None:
    """Mini often calls look/observe for desktop files. Touch disk, not camera."""

    hay = _owner_visual_haystack(args, live_session_id, device_id)
    from app.ev.laptop_files import is_system_confirmation, looks_like_file_task

    if is_system_confirmation(hay):
        return {
            "ok": True,
            "executed": False,
            "verified": False,
            "ignored": "system_confirmation",
            "spoken": "",
        }
    if looks_like_file_task(hay):
        return await _run_computer_goal(
            session,
            {"goal": hay, "target_app": args.get("target_app")},
            actor=actor,
            live_session_id=live_session_id,
            device_id=device_id,
            request_id=request_id,
        )
    return None


async def _reroute_look_to_screen(
    session: AsyncSession,
    args: dict,
    *,
    actor: str,
    live_session_id: str | None,
    device_id,
    request_id: str | None,
) -> dict | None:
    files = await _reroute_visual_to_files(
        session,
        args,
        actor=actor,
        live_session_id=live_session_id,
        device_id=device_id,
        request_id=request_id,
    )
    if files is not None:
        return files
    from app.ev.computer_strategy import look_should_use_screen

    hay = _owner_visual_haystack(args, live_session_id, device_id)
    from app.memory.visual import wants_keep_visible

    if wants_keep_visible(hay):
        return None
    if not look_should_use_screen(hay):
        return None
    from app.ev.computer import handle_computer_tool

    return await handle_computer_tool(
        session,
        "screen_look",
        {"target": "active_window", "goal": hay},
        actor=actor,
        live_session_id=live_session_id,
        device_id=device_id,
        request_id=request_id,
    )


async def _handle(
    session: AsyncSession,
    name: str,
    args: dict,
    *,
    actor: str,
    live_session_id: str | None = None,
    device_id=None,
    request_id: str | None = None,
    channel: str | None = None,
) -> dict:
    fleet = await handle_fleet_tool(session, name, args, actor=actor)
    if fleet is not None:
        return fleet
    if name == "execute_command":
        return await _run_execute_command(session, args, actor=actor)
    if name == "recall":
        # F4: deep-history escape hatch over the proven F0+F1 retrieval stack.
        from app.memory.select import explicit_recall_payload

        detail = str(args.get("detail") or "expanded")
        k = {"brief": 4, "expanded": 10, "source": 14}.get(detail, 10)
        return await explicit_recall_payload(
            session,
            str(args.get("query", "")),
            k=k,
        )
    if name == "computer":
        return await _run_computer_goal(
            session, args, actor=actor,
            live_session_id=live_session_id, device_id=device_id,
            request_id=request_id,
        )
    if name == "code":
        return await _run_code_goal(
            session,
            args,
            actor=actor,
            channel=channel,
            live_session_id=live_session_id,
        )
    if name == "recall_history":
        # EV VOICE CONTROL PLAN: chunked past-history retrieval with time
        # ranges, as_of time travel, brief/full chunks, cursor pagination.
        from app.memory.history import recall_history as _recall_history

        return await _recall_history(
            session,
            str(args.get("query") or ""),
            k=int(args.get("k") or 8),
            time_range=str(args.get("time_range") or "all_time"),
            start_date=args.get("start_date"),
            end_date=args.get("end_date"),
            memory_type=args.get("memory_type"),
            as_of=args.get("as_of"),
            chunk_mode=str(args.get("chunk_mode") or "brief"),
            cursor=args.get("cursor"),
        )
    if name in UI_VERB_TOOLS:
        # EV VOICE CONTROL PLAN §4: UI-specific verbs map onto the existing
        # computer primitives; no per-app verb is ever added to the registry.
        from app.ev.computer import handle_computer_tool as _computer_handle

        if not _ui_verbs_enabled():
            return {
                "ok": False,
                "executed": False,
                "verified": False,
                "error": "ui_verbs_disabled",
                "spoken": "UI verb tools are disabled for this build.",
            }
        canonical, defaults = UI_VERB_MAP[name]
        merged = dict(defaults)
        for key, value in args.items():
            if value is None:
                continue
            if key == "ref":
                merged["element_ref"] = value
            elif key == "x":
                merged["x_normalized"] = float(value)
            elif key == "y":
                merged["y_normalized"] = float(value)
            elif key == "mode" and canonical == "ui_action":
                merged["action"] = value
            else:
                merged[key] = value
        return await _computer_handle(
            session,
            canonical,
            merged,
            actor=actor,
            live_session_id=live_session_id,
            device_id=str(device_id) if device_id else None,
            request_id=request_id,
        )
    retriever = Retriever(session)
    if name == "search_memory":
        from app.memory.select import explicit_recall_payload

        payload = await explicit_recall_payload(
            session,
            str(args.get("query", "")),
            k=int(args.get("k", 10)),
            memory_type_hint=str(args["memory_type"]) if args.get("memory_type") else None,
        )
        return payload
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
        payload = [
            {"title": r.title, "url": r.url, "snippet": r.snippet}
            for r in results
        ]
        spoken_bits = [
            str(item.get("snippet") or item.get("title") or "").strip()
            for item in payload[:2]
        ]
        spoken = " ".join(bit for bit in spoken_bits if bit)[:500]
        return {
            "ok": True,
            "count": len(results),
            "results": payload,
            "spoken": spoken or "I found sources, but they had no summary text.",
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
    if name == "heading_out":
        from app.ev.workbench import handle_heading_out

        return await handle_heading_out(
            session,
            notify_to=args.get("notify_to"),
            notify_text=args.get("notify_text"),
            place=args.get("place"),
            actor=actor,
            live_session_id=live_session_id,
            device_id=device_id,
        )
    if name in _LIFE_BRIDGES:
        return await _dispatch_life_action(
            session, name, args, actor=actor, policy_checked=True
        )
    if name == "open_url":
        from app.ev.computer import open_url_via_live_or_helper

        return await open_url_via_live_or_helper(
            session,
            args,
            actor=actor,
            live_session_id=live_session_id,
            device_id=str(device_id) if device_id else None,
        )
    if name in {
        "open_app",
        "close_app",
        "activate_app",
        "list_apps",
        "computer_status",
        "inspect_ui",
        "ui_action",
        "screen_look",
        "app_action",
    }:
        from app.ev.computer import handle_computer_tool

        return await handle_computer_tool(
            session,
            name,
            args,
            actor=actor,
            live_session_id=live_session_id,
            device_id=str(device_id) if device_id else None,
            request_id=request_id,
        )
    if name == "evie_turn":
        from app.ev.turn_controller import TurnController

        controller = TurnController(
            session, actor=actor, device_id=str(device_id) if device_id else None, session_id=live_session_id
        )
        owner_turn = str(args.get("owner_turn") or "").strip()
        if not owner_turn and not args.get("turn_id"):
            return {"ok": False, "error": "missing_owner_turn", "spoken": "I didn't catch that."}
        turn_id = str(args.get("turn_id") or "").strip() or None
        result = await controller.handle_turn(owner_turn or "", turn_id=turn_id)
        return {
            "ok": result.ok,
            "route": result.route,
            "operation": result.operation,
            "canonical_data": result.canonical_data,
            "entity_refs": result.entity_refs,
            "owner_message": result.owner_message,
            "spoken": result.owner_message or (result.clarification_question if result.needs_clarification else "") or (result.error or ""),
            "needs_clarification": result.needs_clarification,
            "clarification_question": result.clarification_question,
            "error": result.error,
            "approval_required": result.approval_required,
            "latency_ms": result.latency_ms,
            "turn_result": result.model_dump(),
        }
    if name.startswith("life_") or name == "mission_control":
        from app.life.dispatch import handle_life_tool

        return await handle_life_tool(session, name, dict(args or {}), actor=actor)
    if name == "set_reminder":
        from uuid import uuid4

        from app.ev.briefing import extract_reminder_when
        from app.ev.resolve import parse_owner_when
        from app.ev.timers import start_timer
        from app.models import Alert
        from app.utils.text import utcnow as _utcnow

        text = str(args.get("text") or "").strip()
        now = _utcnow()
        when = args.get("when") or extract_reminder_when(text)
        fire_at = None
        for candidate in (when, extract_reminder_when(f"{when or ''} {text}"), text):
            if not candidate:
                continue
            fire_at = parse_owner_when(str(candidate), now=now)
            if fire_at is not None:
                break
        if fire_at is not None:
            timed = await start_timer(
                session,
                at=fire_at.isoformat(),
                text=text,
                actor=actor,
            )
            if timed.get("ok"):
                return {
                    "ok": True,
                    "text": text,
                    "when": timed.get("fire_at"),
                    "id": timed.get("id"),
                    "spoken": timed.get("spoken") or f"Reminder set: {text}.",
                    "evidence": timed.get("evidence"),
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
        from app.ev.actuator import evidence_base
        from app.utils.text import utcnow as _utcnow

        return {
            "ok": True,
            "text": text,
            "id": str(alert.id),
            "stored": "alert",
            "spoken": f"Reminder set: {text}.",
            "evidence": evidence_base(
                source="alert",
                accepted=True,
                observed=True,
                now=_utcnow(),
                alert_id=str(alert.id),
            ),
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

        return await handle_research(session, str(args["question"]), actor=actor)
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
    if name == "look":
        rerouted = await _reroute_look_to_screen(
            session,
            args,
            actor=actor,
            live_session_id=live_session_id,
            device_id=device_id,
            request_id=request_id,
        )
        if rerouted is not None:
            return rerouted
        from app.ev.look import live_owner_transcript, look_with_timeout

        prompt = (
            args.get("prompt")
            or args.get("objective")
            or live_owner_transcript(
                live_session_id, str(device_id) if device_id else None
            )
            or None
        )
        return await look_with_timeout(
            session,
            actor=actor,
            prompt=prompt,
            attachment_id=args.get("attachment_id"),
            focus=str(args.get("focus") or "auto"),
            detail=str(args.get("detail") or "high"),
            live_session_id=live_session_id,
            device_id=str(device_id) if device_id else None,
            request_id=request_id,
        )
    if name == "observe_camera":
        rerouted = await _reroute_visual_to_files(
            session,
            args,
            actor=actor,
            live_session_id=live_session_id,
            device_id=device_id,
            request_id=request_id,
        )
        if rerouted is not None:
            return rerouted
        from app.ev.look import observe_camera_with_timeout

        return await observe_camera_with_timeout(
            session,
            actor=actor,
            duration_seconds=args.get("duration_seconds"),
            objective=args.get("objective"),
            strategy=str(args.get("strategy") or "interval"),
            detail=str(args.get("detail") or "high"),
            live_session_id=live_session_id,
            device_id=str(device_id) if device_id else None,
            request_id=request_id,
        )
    if name == "capture_photo":
        rerouted = await _reroute_look_to_screen(
            session,
            args,
            actor=actor,
            live_session_id=live_session_id,
            device_id=device_id,
            request_id=request_id,
        )
        if rerouted is not None:
            return rerouted
        from app.ev.look import capture_photo_with_timeout

        return await capture_photo_with_timeout(
            session,
            actor=actor,
            prompt=args.get("prompt"),
            detail=str(args.get("detail") or "high"),
            live_session_id=live_session_id,
            device_id=str(device_id) if device_id else None,
            request_id=request_id,
        )
    if name == "record_video":
        from app.ev.look import record_video_with_timeout

        return await record_video_with_timeout(
            session,
            actor=actor,
            duration_seconds=args.get("duration_seconds"),
            prompt=args.get("prompt"),
            detail=str(args.get("detail") or "high"),
            live_session_id=live_session_id,
            device_id=str(device_id) if device_id else None,
            request_id=request_id,
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
            session,
            str(args["query"]),
            kind=str(args.get("kind") or "org"),
            actor=actor,
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

        return await handle_whats_on_my_plate(session, actor=actor)
    if name == "draft_reply":
        from app.ev.workbench import handle_draft_reply

        return await handle_draft_reply(
            session,
            str(args["mail_id"]),
            body=args.get("body"),
            confirm=bool(args.get("confirm")),
            send=bool(args.get("send")),
            to_addr=args.get("to"),
            subject=args.get("subject"),
            actor=actor,
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
    policy_checked: bool = False,
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
        if policy_checked:
            outcome = await integrations.execute_action_after_policy(
                session,
                integration.id,
                action,
                args,
                actor=actor,
            )
        else:
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
