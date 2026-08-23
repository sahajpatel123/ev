"""Capability registration for Evie OS G1 core state.

Hard architecture law: Realtime/DeepSeek learn G1 capabilities through the
derived Live Capability Manifest, never through hand-written prompts.
"""

from __future__ import annotations

from app.ev.capability_registry import RegisteredCapability, register_capability

LIFE_TOOLS = frozenset(
    {
        "life_project_create",
        "life_project_update",
        "life_project_query",
        "life_goal_create",
        "life_goal_update",
        "life_goal_add_step",
        "life_goal_query",
        "life_commitment_create",
        "life_commitment_update",
        "life_commitment_query",
        "life_relationship_set",
        "mission_control",
    }
)


def overlay_life_entry(entry: dict, readiness: dict) -> dict:
    entry.setdefault("fallback_reason", None)
    entry["capability"] = "life_state"
    entry["readiness"] = "ready"
    return entry


def _register() -> None:
    register_capability(
        RegisteredCapability(
            name="life_state",
            description="Projects, goals, steps, commitments, mission control",
            tools=frozenset(LIFE_TOOLS),
            overlay=overlay_life_entry,
            readiness_key="life_state",
            risk_class="R1",
        )
    )


_register()
