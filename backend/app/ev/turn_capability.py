"""G1.3 capability manifest — turn controller, state, manager."""

from __future__ import annotations

from app.ev.capability_registry import RegisteredCapability, register_capability
from app.ev.model_router import manager_model_info, turn_control_model_info

TURN_TOOLS = frozenset({"evie_turn"})
STATE_TOOLS = frozenset({
    "life_project_create", "life_project_update", "life_project_query",
    "life_goal_create", "life_goal_update", "life_goal_add_step", "life_goal_query",
    "life_commitment_create", "life_commitment_update", "life_commitment_query",
    "life_relationship_set", "mission_control",
})
MANAGER_TOOLS: frozenset[str] = frozenset()  # No direct tools yet; scaffolded

def _overlay_turn(entry: dict, readiness: dict) -> dict:
    entry.setdefault("fallback_reason", None)
    entry["capability"] = "evie.turn_controller"
    entry["readiness"] = readiness.get("readiness", "ready")
    return entry

def _overlay_state(entry: dict, readiness: dict) -> dict:
    entry.setdefault("fallback_reason", None)
    entry["capability"] = "evie.state"
    entry["readiness"] = readiness.get("readiness", "ready")
    return entry

def _overlay_manager(entry: dict, readiness: dict) -> dict:
    entry.setdefault("fallback_reason", None)
    entry["capability"] = "evie.manager"
    entry["readiness"] = readiness.get("readiness", "ready")
    return entry

def _register():
    # Turn controller — Luna
    turn_control_model_info()
    register_capability(
        RegisteredCapability(
            name="evie.turn_controller",
            description="Luna turn intent → Evie Core routing",
            tools=TURN_TOOLS,
            overlay=_overlay_turn,
            readiness_key="evie.turn_controller",
            risk_class="R1",
        )
    )
    # State — canonical project/goal/commitment
    register_capability(
        RegisteredCapability(
            name="evie.state",
            description="Canonical projects/goals/commitments",
            tools=STATE_TOOLS,
            overlay=_overlay_state,
            readiness_key="evie.state",
            risk_class="R1",
        )
    )
    # Manager — DeepSeek scaffolded
    manager_model_info()
    register_capability(
        RegisteredCapability(
            name="evie.manager",
            description="DeepSeek complex-work manager",
            tools=MANAGER_TOOLS,
            overlay=_overlay_manager,
            readiness_key="evie.manager",
            risk_class="R1",
        )
    )

_register()
