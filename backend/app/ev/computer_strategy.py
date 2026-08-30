"""Computer-control strategy: adapters, routing, budgets, envelopes, schema truth.

The Realtime model still plans. This module is the server-side discipline so
Evie does not rediscover Music via 24 Accessibility clicks, treat ok=true as
goal completion, or ship a stale provider schema as current.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

REQUIRED_COMPUTER_TOOLS = (
    "computer_status",
    "list_apps",
    "open_app",
    "activate_app",
    "close_app",
    "inspect_ui",
    "ui_action",
    "screen_look",
    "app_action",
)

REQUIRED_SCHEMA_PROPERTIES = {
    "inspect_ui": ("app", "name", "query"),
    "app_action": ("app", "action", "playlist", "index"),
    "ui_action": ("action", "element_ref"),
    "screen_look": ("app",),
    "open_app": ("name",),
}

STRATEGY_ORDER = (
    "semantic_adapter",
    "native_api",
    "apple_events",
    "accessibility",
    "keyboard",
    "screen_vision",
    "coordinate",
)

BUDGET_CAPS = {
    "semantic": 4,
    "ax": 8,
    "keyboard": 6,
    "vision": 6,
    "coordinate": 3,
    "recovery": 3,
    "global": 24,
}

NON_PROGRESS_SWITCH_AFTER = 2

MILESTONES = (
    "NEW",
    "APP_RESOLVED",
    "APP_OPEN",
    "CONTROL_METHOD_SELECTED",
    "TARGET_FOUND",
    "ACTION_EXECUTED",
    "STATE_CHANGED",
    "VERIFICATION_OBTAINED",
    "GOAL_COMPLETE",
)

APP_ADAPTERS: dict[str, dict[str, Any]] = {
    "Music": {
        "bundle_ids": ("com.apple.Music",),
        "preferred": "semantic_adapter",
        "semantic_adapter": "music",
        "supported_actions": (
            "play",
            "play_playlist_track",
            "pause",
            "next",
            "previous",
            "status",
            "find_playlist",
            "list_tracks",
            "list_playlists",
        ),
        "verification": "semantic_player_state",
        "fallbacks": ("accessibility", "screen_vision", "coordinate"),
    },
    "Safari": {
        "bundle_ids": ("com.apple.Safari",),
        "preferred": "semantic_adapter",
        "semantic_adapter": "safari",
        "supported_actions": ("search", "navigate", "status", "open_item"),
        "verification": "current_url",
        "fallbacks": ("accessibility", "keyboard", "screen_vision", "coordinate"),
    },
    "Notes": {
        "bundle_ids": ("com.apple.Notes",),
        "preferred": "semantic_adapter",
        "semantic_adapter": "notes",
        "supported_actions": ("create", "append", "read", "status"),
        "verification": "note_body",
        "fallbacks": ("accessibility", "keyboard", "screen_vision"),
    },
    "Finder": {
        "bundle_ids": ("com.apple.finder",),
        "preferred": "semantic_adapter",
        "semantic_adapter": "finder",
        "supported_actions": ("open_item", "open_folder", "status"),
        "verification": "selection",
        "fallbacks": ("accessibility", "keyboard", "screen_vision"),
    },
}

PLAY_ACTIONS = frozenset(
    {"play", "play_track", "play_playlist", "play_playlist_track"}
)
APP_ACTION_ALIASES = {
    "play_playlist_track": "play",
    "play_track": "play",
    "play_playlist": "play",
    "now_playing": "status",
    "current": "status",
    "open_first_result": "navigate",
    "open_newest": "open_item",
    "create_note": "create",
    "append_note": "append",
    "read_note": "read",
}

_SUCCESS_CLAIM_RE = re.compile(
    r"\b("
    r"it'?s playing|i(?:'m| am) playing|now playing|"
    r"i (?:opened|sent|changed|created|deleted|clicked|typed|played)|"
    r"it'?s (?:open|on|done|ready)|i turned (?:it )?on"
    r")\b",
    re.I,
)
_BUDGET_SPEECH_RE = re.compile(r"\b(action budget|out of budget|step budget)\b", re.I)
_COMPUTER_TASK_RE = re.compile(
    r"\b("
    r"music|safari|notes|finder|calculator|downloads|playlist|"
    r"inspect(?: the)? ui|screen look|click|type into|scroll|"
    r"using calculator|open (?:the )?(?:app|application)"
    r")\b",
    re.I,
)
_APP_NAME_RE = re.compile(
    r"\b(music|safari|notes|finder|calculator|textedit|mail|messages|calendar)\b",
    re.I,
)


def normalize_app_action(action: str | None) -> str:
    raw = str(action or "").strip().lower()
    return APP_ACTION_ALIASES.get(raw, raw)


def adapter_for(app: str | None, bundle_id: str | None = None) -> dict[str, Any] | None:
    needle = str(app or "").strip()
    bundle = str(bundle_id or "").strip().lower()
    if needle:
        for name, spec in APP_ADAPTERS.items():
            if needle.lower() == name.lower() or needle.lower() in name.lower():
                return {"app": name, **spec}
    if bundle:
        for name, spec in APP_ADAPTERS.items():
            if bundle in {item.lower() for item in spec["bundle_ids"]}:
                return {"app": name, **spec}
    return None


def control_for_app(app: str | None, bundle_id: str | None = None) -> dict[str, Any]:
    spec = adapter_for(app, bundle_id)
    if spec is None:
        return {
            "preferred": "accessibility",
            "semantic_adapter": None,
            "supported_actions": [],
            "fallbacks": ["accessibility", "keyboard", "screen_vision", "coordinate"],
            "verification": "inspect_ui_or_screen",
        }
    return {
        "preferred": spec["preferred"],
        "semantic_adapter": spec["semantic_adapter"],
        "supported_actions": list(spec["supported_actions"]),
        "fallbacks": list(spec["fallbacks"]),
        "verification": spec["verification"],
    }


def preferred_strategy_for_goal(*, app: str | None, bundle_id: str | None = None) -> str:
    spec = adapter_for(app, bundle_id)
    if spec is not None:
        return "semantic"
    return "ax"


def classify_tool_strategy(name: str, arguments: dict[str, Any] | None = None) -> str:
    args = arguments or {}
    action = str(args.get("action") or "").lower()
    app = str(args.get("app") or "").lower()
    if name == "app_action":
        if "calculator" in app:
            return "keyboard"
        return "semantic"
    if name == "screen_look":
        return "vision"
    if name == "ui_action" and action in {"click_at", "screen_click"}:
        return "coordinate"
    if name == "ui_action" and action in {"keyboard", "paste", "menu"}:
        return "keyboard"
    if name in {"inspect_ui", "ui_action"}:
        return "ax"
    if name in {"open_app", "activate_app", "close_app", "list_apps", "computer_status"}:
        return "lifecycle"
    return "other"


def next_strategy(current: str) -> str | None:
    order = ("semantic", "ax", "keyboard", "vision", "coordinate")
    try:
        index = order.index(current)
    except ValueError:
        return "ax"
    if index + 1 < len(order):
        return order[index + 1]
    return None


def looks_like_computer_task(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if _APP_NAME_RE.search(raw) or _COMPUTER_TASK_RE.search(raw):
        return True
    return bool(re.search(r"\b(open|launch|quit|play the|find my)\b", raw, re.I))


def computer_tools_from_specs(tools: list[dict] | tuple[dict, ...] | None) -> list[dict]:
    out: list[dict] = []
    for item in tools or ():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name in REQUIRED_COMPUTER_TOOLS:
            out.append(item)
    out.sort(key=lambda item: str(item.get("name") or ""))
    return out


def computer_tool_schema_hash(tools: list[dict] | tuple[dict, ...] | None) -> str:
    canonical: list[dict[str, Any]] = []
    for item in computer_tools_from_specs(tools):
        parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
        canonical.append(
            {
                "name": item.get("name"),
                "properties": sorted(
                    str(key)
                    for key in (parameters.get("properties") or {})
                    if isinstance(parameters.get("properties"), dict)
                ),
                "required": sorted(
                    str(key) for key in (parameters.get("required") or [])
                    if isinstance(parameters.get("required"), list)
                ),
            }
        )
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def evaluate_provider_computer_schema(
    *,
    advertised_tools: list[dict] | tuple[dict, ...] | None,
    acknowledged_names: list[str] | tuple[str, ...] | None,
    acknowledged_schemas: list[dict] | tuple[dict, ...] | None,
) -> dict[str, Any]:
    advertised = computer_tools_from_specs(advertised_tools)
    local_hash = computer_tool_schema_hash(advertised)
    names = {str(name) for name in (acknowledged_names or ()) if name}
    missing = [name for name in REQUIRED_COMPUTER_TOOLS if name not in names]
    schema_by_name = {
        str(item.get("name")): item
        for item in (acknowledged_schemas or ())
        if isinstance(item, dict) and item.get("name")
    }
    property_gaps: dict[str, list[str]] = {}
    for tool, required in REQUIRED_SCHEMA_PROPERTIES.items():
        meta = schema_by_name.get(tool) or {}
        present = set(meta.get("property_names") or [])
        gap = [key for key in required if key not in present]
        if gap:
            property_gaps[tool] = gap
    match = not missing and not property_gaps and bool(names)
    return {
        "computer_tool_schema_hash": local_hash,
        "tool_schema_match": match,
        "missing_tools": missing,
        "property_gaps": property_gaps,
        "computer_control_ready": match,
        "provider_tools_confirmed": bool(names) and not missing,
    }


def computer_envelope(
    result: dict[str, Any] | None,
    *,
    method: str | None = None,
    progress: str | None = None,
    failure_code: str | None = None,
    recoverable: bool | None = None,
    suggested_fallbacks: list[str] | None = None,
) -> dict[str, Any]:
    out = dict(result or {})
    ok = out.get("ok") is True
    executed = bool(out.get("executed", ok))
    verified = bool(out.get("verified"))
    goal = out.get("goal") if isinstance(out.get("goal"), dict) else {}
    terminal = str(goal.get("status") or "") in {"complete", "failed", "cancelled"}
    goal_complete = bool(goal.get("status") == "complete" and (goal.get("verified") or verified))
    must_continue = bool(out.get("must_continue"))
    if "must_continue" not in out:
        must_continue = (not terminal) and (not verified) and not out.get("cancelled")
    code = failure_code or (None if ok or verified else str(out.get("error") or "") or "failed")
    if code in {"", "None"}:
        code = None
    if recoverable is None:
        recoverable = bool(must_continue) and code not in {
            "cancelled",
            "playlist_not_found",
            "not_found",
            "permission_denied",
            "accessibility_denied",
        }
    spoken = str(out.get("spoken") or "")
    if _BUDGET_SPEECH_RE.search(spoken):
        spoken = "I couldn't get that done."
        out["spoken"] = spoken
    out["ok"] = ok
    out["executed"] = executed
    out["verified"] = verified
    out["goal_progress"] = progress or out.get("goal_progress") or goal.get("status") or "planning"
    out["goal_complete"] = bool(out.get("goal_complete", goal_complete))
    out["complete"] = bool(out["goal_complete"])
    out["must_continue"] = must_continue and not out["goal_complete"]
    out["method"] = method or out.get("method") or "unknown"
    out.setdefault("observed_state", out.get("observed") or goal.get("observed") or {})
    out["failure"] = {
        "code": None if (ok and verified) or out["goal_complete"] else code,
        "recoverable": bool(recoverable) and not out["goal_complete"],
    }
    if suggested_fallbacks is not None:
        out["suggested_fallbacks"] = list(suggested_fallbacks)
    else:
        out.setdefault("suggested_fallbacks", [])
    return out


def speech_claims_success(text: str) -> bool:
    return bool(_SUCCESS_CLAIM_RE.search(text or ""))


def speech_is_grounded(text: str, *, verified: bool, goal_complete: bool, failed: bool) -> bool:
    """False-success gate: a success claim requires verified completion."""

    if not speech_claims_success(text):
        return True
    if failed:
        return False
    return bool(verified and goal_complete)


def user_facing_terminal_speech(failure_code: str | None, fallback: str | None = None) -> str:
    code = str(failure_code or "")
    mapping = {
        "playlist_not_found": "I couldn't find that playlist.",
        "not_found": "I couldn't find that.",
        "cancelled": "Stopped.",
        "step_budget": "I couldn't get that done.",
        "strategy_budget_exhausted": "I couldn't get that done.",
        "cycle_detected": "That path wasn't working, so I stopped.",
        "ordinal_mismatch": "That wasn't the requested track.",
        "find_only": "You asked me to find it, not play it.",
    }
    if code in mapping:
        return mapping[code]
    return fallback or "I couldn't finish that."


def progress_milestone_for(name: str, result: dict[str, Any], *, previous: str | None) -> str:
    if result.get("goal_complete") or (
        isinstance(result.get("goal"), dict) and result["goal"].get("status") == "complete"
    ):
        return "GOAL_COMPLETE"
    if result.get("verified"):
        return "VERIFICATION_OBTAINED"
    if name in {"open_app", "activate_app"} and result.get("ok"):
        return "APP_OPEN"
    if name == "app_action" and result.get("error") not in {"playlist_not_found", "not_found"}:
        if result.get("playlist") or result.get("tracks") or result.get("playlists"):
            return "TARGET_FOUND"
        if result.get("executed"):
            return "ACTION_EXECUTED"
        return "CONTROL_METHOD_SELECTED"
    if name == "inspect_ui":
        if result.get("target_found"):
            return "TARGET_FOUND"
        return "APP_RESOLVED" if previous in {None, "NEW"} else (previous or "APP_RESOLVED")
    if name == "ui_action" and result.get("executed"):
        return "ACTION_EXECUTED"
    if name == "screen_look" and result.get("ok"):
        return "ACTION_EXECUTED"
    return previous or "NEW"


def is_progress(previous: str | None, current: str | None) -> bool:
    if not current or current == previous:
        return False
    try:
        return MILESTONES.index(current) > MILESTONES.index(previous or "NEW")
    except ValueError:
        return current != previous
