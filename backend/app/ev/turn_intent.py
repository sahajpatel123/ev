"""Turn intent schema (G1.3) — typed contract between Luna and Evie Core.

Luna never returns conversational prose for routing; it returns a validated
TurnIntent via structured output.  Evie Core then deterministically resolves
canonical IDs and executes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Route = Literal[
    "CONVERSATION",
    "STATE_QUERY",
    "STATE_MUTATION",
    "MISSION_CONTROL",
    "ACTION",
    "DELEGATED_JOB",
    "RESEARCH_MISSION",
    "CLARIFICATION",
    "UNSUPPORTED",
]

Operation = Literal[
    "PROJECT_LIST",
    "PROJECT_GET",
    "PROJECT_CREATE",
    "PROJECT_UPDATE",
    "GOAL_LIST",
    "GOAL_GET",
    "GOAL_CREATE",
    "GOAL_UPDATE",
    "COMMITMENT_LIST",
    "COMMITMENT_GET",
    "COMMITMENT_CREATE",
    "COMMITMENT_UPDATE",
    "COMMITMENT_CANCEL",
    "STATUS",
    "WHAT_CHANGED",
    "RELATIONSHIP_QUERY",
    "RELATIONSHIP_UPDATE",
    "CAPABILITY_QUERY",
    "UNKNOWN",
]

class TurnIntent(BaseModel):
    """Structured output from Luna.  Validated, not regex-parsed."""

    route: Route = Field(description="High-level route for the turn")
    operation: Operation = Field(default="UNKNOWN", description="Typed operation within the route")
    confidence: float = Field(default=0.9, ge=0.0, le=1.0, description="Model confidence 0-1")
    needs_clarification: bool = Field(default=False)
    clarification_question: str | None = Field(default=None, description="Question to ask when ambiguous")
    # Entities/arguments are human references, never UUIDs
    project_title: str | None = Field(default=None, description="Human project title, e.g. Personal Fitness")
    goal_title: str | None = Field(default=None, description="Human goal title")
    commitment_query: str | None = Field(default=None, description="Substring to find commitment, e.g. workout")
    description: str | None = Field(default=None, description="Generic description / title for creation")
    priority: str | None = Field(default=None, description="CRITICAL/HIGH/NORMAL/LOW")
    due_at: str | None = Field(default=None, description="Natural due time, e.g. tomorrow at 7 PM")
    success_criteria: str | None = Field(default=None)
    status: str | None = Field(default=None)
    person: str | None = Field(default=None)
    relation: str | None = Field(default=None)
    capability_subject: str | None = Field(
        default=None,
        description="Entity family for CAPABILITY_QUERY, e.g. commitments",
    )
    raw_arguments: dict[str, Any] = Field(default_factory=dict, description="Passthrough for future ops")


class TurnResult(BaseModel):
    """Authoritative result from TurnController (Evie Core owns truth)."""

    ok: bool
    route: Route
    operation: Operation
    canonical_data: dict[str, Any] | list[Any] | None = None
    entity_refs: list[dict[str, Any]] = Field(default_factory=list)
    owner_message: str | None = None
    error: str | None = None
    approval_required: bool = False
    needs_clarification: bool = False
    clarification_question: str | None = None
    # For latency/cost tracking
    latency_ms: float | None = None
    luna_usage: dict[str, Any] | None = None
    # F1 shadow memory: turn-scoped recalled history for the response layer.
    # Historical context ONLY — never canonical truth, never persisted. The
    # block is pre-labeled ([EVIE_RECALLED_HISTORY]) and expires with the turn.
    shadow_context: dict[str, Any] | None = None
