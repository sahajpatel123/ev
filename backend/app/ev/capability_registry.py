"""Runtime capability projectors for the Live Capability Manifest.

Tool specs remain the declaration source. Projectors bind those tools to live
device and permission state so the manifest is derived, not hand-written.
Camera and computer register themselves; future features should too.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

OverlayFn = Callable[[dict[str, Any], Any], dict[str, Any]]


@dataclass(frozen=True)
class RegisteredCapability:
    name: str
    description: str
    tools: frozenset[str]
    overlay: OverlayFn
    readiness_key: str
    risk_class: str = "R1"


_REGISTRY: dict[str, RegisteredCapability] = {}


def register_capability(capability: RegisteredCapability) -> RegisteredCapability:
    _REGISTRY[capability.name] = capability
    return capability


def registered_capabilities() -> list[RegisteredCapability]:
    return list(_REGISTRY.values())


def apply_capability_overlays(
    entries: list[dict[str, Any]],
    readiness: dict[str, Any],
) -> list[dict[str, Any]]:
    """Overlay registered tool families onto the current runtime projection."""

    out = list(entries)
    for capability in registered_capabilities():
        state = readiness.get(capability.name)
        if state is None:
            continue
        for index, entry in enumerate(out):
            if str(entry.get("name") or "") in capability.tools:
                out[index] = capability.overlay(dict(entry), state)
    return out


# ---------------------------------------------------------------------------
# CANONICAL SEMANTIC CAPABILITY LAYER (derived, never a second authority)
#
# PERMANENT CAPABILITY LAW: Evie's capabilities are defined by Evie Core +
# TurnController operations + policy + current environment — NOT by what the
# current voice/chat model can see as direct tools. This layer derives
# truthful semantic ability FROM the implementation source (TurnController /
# Luna intent contract) so self-knowledge and diagnostics can never drift
# from what the gate actually executes.
# ---------------------------------------------------------------------------

SEMANTIC_OPERATION_FAMILIES: dict[str, dict[str, Any]] = {
    "PROJECT_READ": {"ops": ["PROJECT_LIST", "PROJECT_GET"], "subject": "projects"},
    "PROJECT_CREATE": {"ops": ["PROJECT_CREATE"], "subject": "projects"},
    "PROJECT_UPDATE": {"ops": ["PROJECT_UPDATE"], "subject": "projects"},
    "GOAL_READ": {"ops": ["GOAL_LIST", "GOAL_GET"], "subject": "goals"},
    "GOAL_CREATE": {"ops": ["GOAL_CREATE"], "subject": "goals"},
    "GOAL_UPDATE": {"ops": ["GOAL_UPDATE"], "subject": "goals"},
    "COMMITMENT_READ": {"ops": ["COMMITMENT_LIST", "COMMITMENT_GET"], "subject": "commitments"},
    "COMMITMENT_CREATE": {"ops": ["COMMITMENT_CREATE"], "subject": "commitments"},
    "COMMITMENT_UPDATE": {"ops": ["COMMITMENT_UPDATE"], "subject": "commitments"},
    "COMMITMENT_CANCEL": {"ops": ["COMMITMENT_CANCEL"], "subject": "commitments"},
    "MISSION_CONTROL_READ": {"ops": ["STATUS"], "subject": "status"},
    "WHAT_CHANGED_READ": {"ops": ["WHAT_CHANGED"], "subject": "status"},
}

_SUBJECT_WORDS: dict[str, str] = {
    "commitment": "commitments",
    "commitments": "commitments",
    "reminder": "commitments",
    "reminders": "commitments",
    "goal": "goals",
    "goals": "goals",
    "project": "projects",
    "projects": "projects",
    "status": "status",
}

_semantic_cache: dict[str, Any] | None = None


def _controller_bound_ops() -> tuple[set[str], bool]:
    """Derive controller bindings from the implementation source itself."""
    import inspect

    from app.ev.luna_adapter import EMIT_INTENT_TOOL

    raw_params = EMIT_INTENT_TOOL.get("parameters")
    raw_properties = raw_params.get("properties") if isinstance(raw_params, dict) else None
    properties: dict[str, Any] = raw_properties if isinstance(raw_properties, dict) else {}
    raw_operation = properties.get("operation")
    operation: dict[str, Any] = raw_operation if isinstance(raw_operation, dict) else {}
    raw_enum = operation.get("enum")
    enum = set(str(value) for value in list(raw_enum)) if isinstance(raw_enum, list) else set()
    try:
        from app.ev import turn_controller as _tc

        source = inspect.getsource(_tc)
    except Exception:  # pragma: no cover - defensive; binding truth must not crash health
        return set(), False
    bound: set[str] = set()
    for op in enum:
        if op == "UNKNOWN":
            continue
        if f'"{op}"' in source or f"'{op}'" in source:
            bound.add(op)
    return bound, True


def semantic_capabilities() -> dict[str, dict[str, Any]]:
    """Truthful semantic capability snapshot derived from the live code."""
    global _semantic_cache
    if _semantic_cache is not None:
        return _semantic_cache
    bound, source_ok = _controller_bound_ops()
    out: dict[str, dict[str, Any]] = {}
    for name, spec in SEMANTIC_OPERATION_FAMILIES.items():
        ops = spec["ops"]
        registered = all(op in bound for op in ops)
        out[name] = {
            "registered": registered,
            "controller_bound": registered,
            "operations": ops,
            # Policy: no approval gate exists on these canonical families
            # today. When policy gating lands, derive it here — do NOT
            # hard-code optimism elsewhere.
            "policy_allowed": True,
            "runtime_available": True,
            "realtime_direct_tool": False,
            "execution_owner": "TurnGate/Core",
            "source_derived": source_ok,
        }
    _semantic_cache = out
    return out


def semantic_capability_for(subject_word: str | None) -> dict[str, dict[str, Any]]:
    subject = _SUBJECT_WORDS.get((subject_word or "").strip().lower(), "")
    caps = semantic_capabilities()
    if not subject:
        return caps
    return {
        k: v
        for k, v in caps.items()
        if SEMANTIC_OPERATION_FAMILIES[k]["subject"] == subject
    }


def semantic_capability_answer(subject_word: str | None) -> dict[str, Any]:
    """Canonical answer for owner capability questions (Part 11/16)."""
    caps = semantic_capability_for(subject_word)
    supported = sorted(k for k, v in caps.items() if v["registered"])
    unsupported = sorted(k for k, v in caps.items() if not v["registered"])
    spoken: str
    if not caps:
        spoken = (
            "I can work with your projects, goals, commitments, and mission "
            "control — ask me what changed or what's due."
        )
    else:
        pretty = {
            "PROJECT_READ": "read projects",
            "PROJECT_CREATE": "create projects",
            "PROJECT_UPDATE": "update projects",
            "GOAL_READ": "read goals",
            "GOAL_CREATE": "create goals",
            "GOAL_UPDATE": "update goals",
            "COMMITMENT_READ": "read commitments",
            "COMMITMENT_CREATE": "create commitments",
            "COMMITMENT_UPDATE": "update commitments",
            "COMMITMENT_CANCEL": "cancel commitments",
            "MISSION_CONTROL_READ": "give status",
            "WHAT_CHANGED_READ": "report what changed",
        }
        spoken = "Yes — I can " + ", ".join(pretty.get(s, s.lower()) for s in supported) + "."
    return {
        "spoken": spoken,
        "supported": supported,
        "unsupported": unsupported,
        "capabilities": caps,
        "authority": "capability_registry+TurnController",
    }


def capability_diagnostics() -> dict[str, Any]:
    """Truthful diagnostics for /v1/health (Part 12).

    Makes explicit that 'Realtime direct tool = NO' does NOT mean
    'Evie capability = NO'.
    """
    caps = semantic_capabilities()
    cancel = caps.get("COMMITMENT_CANCEL", {})
    return {
        "canonical_source": "capability_registry+TurnController",
        "semantic_operations": {
            k: {
                "registered": v["registered"],
                "controller_bound": v["controller_bound"],
                "policy_allowed": v["policy_allowed"],
                "runtime_available": v["runtime_available"],
                "realtime_direct_tool": v["realtime_direct_tool"],
                "execution_owner": v["execution_owner"],
            }
            for k, v in caps.items()
        },
        "commitment_cancel": {
            "registered": cancel.get("registered", False),
            "controller_bound": cancel.get("controller_bound", False),
            "policy_allowed": cancel.get("policy_allowed", False),
            "current_runtime_available": cancel.get("runtime_available", False),
            "realtime_direct_tool": False,
            "execution_owner": "TurnGate/Core",
        },
    }
