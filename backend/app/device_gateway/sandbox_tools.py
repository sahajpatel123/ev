"""Sandbox-safe live tools. Isolated from production memory tools."""

from __future__ import annotations

import hashlib
import json
from typing import Any

SANDBOX_SAFE_LIVE_TOOLS = frozenset(
    {
        "open_app",
        "activate_app",
        "close_app",
        "list_apps",
        "computer_status",
        "look",
        "phone_action",
    }
)

SANDBOX_LIVE_INSTRUCTIONS = (
    "You are in cross-platform sandbox mode. Do not claim access to owner "
    "personal memory. Use only listed sandbox-safe functions. Origin device "
    "and action target are separate: reply on the phone; act on the Mac only "
    "when asked. Phone timers, reminders, calls, messages, and Maps use "
    "phone_action on this iPhone — not Mac computer tools. Never claim "
    "completion without verified function evidence. look uses this phone's "
    "camera unless the owner explicitly asks for the Mac camera."
)

_LOOK_DESCRIPTION = (
    "Capture one frame from the current phone camera (or the Mac camera only "
    "if the owner explicitly asked for the Mac). Use for look at this / what "
    "do you see. Do not guess. Do not claim you cannot see if this function "
    "is listed."
)


def sandbox_live_tool_specs(device=None) -> list[dict[str, Any]]:
    from app.ev.tools import get_spec

    specs: list[dict[str, Any]] = []
    for name in sorted(SANDBOX_SAFE_LIVE_TOOLS):
        if name == "phone_action":
            from app.device_gateway.mobile_actions.tool import phone_action_function_spec

            specs.append(phone_action_function_spec(device))
            continue
        spec = get_spec(name)
        if not spec:
            continue
        description = spec.get("description") or name
        if name == "look":
            description = _LOOK_DESCRIPTION
        specs.append(
            {
                "type": "function",
                "name": name,
                "description": description,
                "parameters": spec.get("parameters")
                or {"type": "object", "properties": {}, "additionalProperties": False},
            }
        )
    return specs


def sandbox_tool_schema_hash(specs: list[dict] | None = None) -> str:
    payload = specs if specs is not None else sandbox_live_tool_specs()
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def tool_schema_generation() -> str:
    return f"sandbox-{sandbox_tool_schema_hash()}"


def is_sandbox_safe_tool(name: str) -> bool:
    return (name or "").strip() in SANDBOX_SAFE_LIVE_TOOLS


_PROVIDER_EFFECTIVE_HASH: str | None = None
_PROVIDER_EFFECTIVE_NAMES: tuple[str, ...] = ()


def note_provider_effective(names: list[str] | tuple[str, ...] | None, specs: list[dict] | None = None) -> None:
    """Record the provider-acknowledged sandbox catalog. No secrets."""

    global _PROVIDER_EFFECTIVE_HASH, _PROVIDER_EFFECTIVE_NAMES
    cleaned = tuple(sorted({str(name).strip() for name in (names or ()) if str(name).strip()}))
    _PROVIDER_EFFECTIVE_NAMES = cleaned
    _PROVIDER_EFFECTIVE_HASH = sandbox_tool_schema_hash(specs) if specs is not None else None


def provider_effective_snapshot() -> dict[str, object]:
    local = sandbox_live_tool_specs()
    local_names = tuple(sorted(spec["name"] for spec in local))
    local_hash = sandbox_tool_schema_hash(local)
    expected = tuple(sorted(SANDBOX_SAFE_LIVE_TOOLS))
    local_ready = local_names == expected
    provider_verified = (
        expected == _PROVIDER_EFFECTIVE_NAMES
        and (_PROVIDER_EFFECTIVE_HASH is None or local_hash == _PROVIDER_EFFECTIVE_HASH)
        and bool(_PROVIDER_EFFECTIVE_NAMES)
    )
    return {
        "sandbox_tool_schema_hash": local_hash,
        "tool_schema_generation": tool_schema_generation(),
        "expected_tools": list(expected),
        "local_tools": list(local_names),
        "provider_tools": list(_PROVIDER_EFFECTIVE_NAMES),
        "provider_schema_hash": _PROVIDER_EFFECTIVE_HASH,
        "live_cross_platform_tools_ready": local_ready and (not _PROVIDER_EFFECTIVE_NAMES or provider_verified),
        "live_provider_tools_verified": provider_verified,
        "local_catalog_ready": local_ready,
    }
