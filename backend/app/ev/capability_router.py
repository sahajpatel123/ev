"""F3 Capability Router — ONE deterministic routing view over existing truth.

PERMANENT PRINCIPLE (F3): the model expresses the GOAL; the backend selects
HOW to accomplish it. The router answers WHICH EXECUTION SYSTEM; the planner
answers WHAT STEPS; the executor answers DO THE STEP.

No fifth registry: candidates reference EXISTING capability truth
(TOOL_SPECS / integrations / macos_life helper / computer executor families /
Core authority). Availability is checked, never assumed from a name.

Execution preference (F3 §9):
  1. CORE (canonical state)      — handed to evie_turn/TurnGate, never executed here
  2. MEMORY                      — historical context; owned by the F0+F1 memory
                                   router; this router never executes memory
  3. SEMANTIC / NATIVE adapter   — verified service capability
  4. GENERIC COMPUTER (AX)       — F2 executor, structured accessibility
  5. VISUAL / pointer            — executor strategies (future hook)
  6. FS / controlled system      — semantically appropriate only

EXECUTION FENCE (global law): routing may change only BEFORE a side-effect
boundary. Once a mutating dispatch may have happened (fence state in
MUTATION_RISK_STATES), no route may re-execute the action.

Flag ``EV_CAPABILITY_ROUTER_V2``: off | shadow | on.
  off    — direct legacy routing only.
  shadow — router predicts the route and records the comparison; legacy stays
           authoritative; mutating capabilities are NEVER double-executed.
  on     — routed capabilities may execute through the selected route
           (initial ON set is read-only/reversible only).
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.config import settings


class RouteKind(StrEnum):
    CORE = "core"
    MEMORY = "memory"
    SEMANTIC = "semantic"
    GENERIC_COMPUTER = "generic_computer"
    UNAVAILABLE = "unavailable"
    POLICY_CONFIRMATION = "policy_confirmation"


class Rationale(StrEnum):
    CORE_STATE_AUTHORITY = "CORE_STATE_AUTHORITY"
    MEMORY_PLANE_NOT_ACTION = "MEMORY_PLANE_NOT_ACTION"
    SEMANTIC_ADAPTER_AVAILABLE = "SEMANTIC_ADAPTER_AVAILABLE"
    GENERIC_UI_REQUIRED = "GENERIC_UI_REQUIRED"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    DEVICE_NOT_TRUSTED = "DEVICE_NOT_TRUSTED"
    POLICY_CONFIRMATION_REQUIRED = "POLICY_CONFIRMATION_REQUIRED"


# Initial routed set (F3 §30): read-only semantic + reversible low-risk.
ROUTER_TOOLS = frozenset(
    {
        "calendar_read",
        "list_messages",
        "list_mail",
        "get_weather",
        "computer_status",
        "start_timer",
        "open_app",
        "activate_app",
    }
)

# Core-state authority: these names belong to evie_turn/TurnGate/Core. The
# router records them for diagnostics but NEVER reroutes canonical truth.
CORE_TOOLS = frozenset(
    {
        "evie_turn",
        "mission_control",
        "life_project_create", "life_project_update", "life_project_query",
        "life_goal_create", "life_goal_update", "life_goal_add_step", "life_goal_query",
        "life_commitment_create", "life_commitment_update", "life_commitment_query",
        "life_relationship_set",
    }
)

# Goal → semantic capability candidates in deterministic preference order.
SEMANTIC_CANDIDATES: dict[str, list[str]] = {
    "calendar_read": ["calendar_read"],
    "list_messages": ["list_messages"],
    "list_mail": ["list_mail"],
    "get_weather": ["get_weather"],
    "computer_status": ["computer_status"],
    "start_timer": ["start_timer"],
    "open_app": ["open_app"],
    "activate_app": ["activate_app"],
}

# Goal keywords for ActionGoal building from raw owner text (deterministic;
# Luna/TurnGate remain the intent authorities — this is execution-path support).
GOAL_SEMANTIC_HINTS: tuple[tuple[str, str], ...] = (
    ("calendar", "calendar_read"),
    ("meeting", "calendar_read"),
    ("schedule", "calendar_read"),
    ("messages", "list_messages"),
    ("texts", "list_messages"),
    ("mail", "list_mail"),
    ("email", "list_mail"),
    ("weather", "get_weather"),
    ("forecast", "get_weather"),
    ("timer", "start_timer"),
    ("open ", "open_app"),
    ("launch ", "open_app"),
    ("switch to", "activate_app"),
    ("bring ", "activate_app"),
)


def router_mode() -> str:
    return (getattr(settings, "capability_router_v2", "off") or "off").strip().lower()


def goal_fingerprint(goal_text: str) -> str:
    return hashlib.sha256((goal_text or "").encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Router input / output
# ---------------------------------------------------------------------------


@dataclass
class ActionGoal:
    """Structured routing input. Adapts existing tool-call context."""

    goal: str
    semantic_intent: str | None = None
    owner_turn_id: str | None = None
    actor: str = "master"
    device_scope: str = "owner"
    target: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    expected_effect: dict[str, Any] | None = None
    risk_context: dict[str, Any] = field(default_factory=dict)
    execution_id: str | None = None
    idempotency_key: str | None = None

    def fingerprint(self) -> str:
        return goal_fingerprint(self.goal)


@dataclass
class CapabilityRoute:
    """Deterministic route decision with machine-readable rationale."""

    route_kind: RouteKind
    capability: str
    executor_family: str | None = None
    device: str | None = None
    availability: str = "unknown"  # available | unavailable | unknown
    expected_effect: dict[str, Any] | None = None
    verification_contract: str = "unknown"
    fallback_allowed: bool = True
    rationale_code: Rationale = Rationale.CAPABILITY_UNAVAILABLE
    policy_risk: str | None = None
    candidates_considered: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_kind": self.route_kind.value,
            "capability": self.capability,
            "executor_family": self.executor_family,
            "device": self.device,
            "availability": self.availability,
            "expected_effect": self.expected_effect,
            "verification_contract": self.verification_contract,
            "fallback_allowed": self.fallback_allowed,
            "rationale_code": self.rationale_code.value,
            "policy_risk": self.policy_risk,
            "candidates_considered": self.candidates_considered,
        }


# ---------------------------------------------------------------------------
# Availability (never assume from a name — check, per §19)
# ---------------------------------------------------------------------------


async def _semantic_available(capability: str, session: Any, device_scope: str) -> tuple[bool, str]:
    """Check real availability for one semantic capability."""

    if device_scope not in {"owner", "master"}:
        return False, "untrusted_scope"
    if capability == "get_weather":
        # Provider-backed service; gateway availability governs.
        return True, "provider_service"
    if capability == "start_timer":
        return True, "core_timer"  # OwnerTimer rows — canonical, always offered
    if capability == "computer_status":
        from app.voice.live.layer import active_lives

        return (len(active_lives()) > 0), "live_session"
    if capability in {"calendar_read", "list_messages", "list_mail"}:
        if session is None:
            return False, "no_session"
        try:
            from sqlalchemy import select

            from app.models import Integration

            adapter = {
                "calendar_read": "calendar",
                "list_messages": "messaging",
                "list_mail": "mail",
            }[capability]
            rows = (
                await session.execute(
                    select(Integration).where(
                        Integration.adapter == adapter,
                        Integration.status == "active",
                    )
                )
            ).scalars().all()
            return (len(rows) > 0), ("integration_active" if rows else "integration_missing")
        except Exception:  # noqa: BLE001 - availability must never raise
            return False, "availability_check_failed"
    return False, "unknown_capability"


def _computer_live_available() -> tuple[bool, str | None]:
    from app.voice.live.layer import active_lives

    lives = active_lives()
    return (len(lives) > 0), (getattr(lives[0], "session_id", None) if lives else None)


def _policy_risk_for(capability: str) -> str | None:
    try:
        from app.ev.policy import evaluate_policy

        decision = evaluate_policy(
            capability, actor="master", channel="voice", provider_connected=True,
        )
        return getattr(decision, "risk_class", None)
    except Exception:  # noqa: BLE001 - risk labeling is advisory
        return None


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------


async def route_action(goal: ActionGoal, *, session: Any = None) -> CapabilityRoute:
    """Deterministic execution-path selection for one action goal."""

    started = time.perf_counter()
    intent = goal.semantic_intent
    if intent is None:
        lowered = (goal.goal or "").lower()
        for hint, candidate in GOAL_SEMANTIC_HINTS:
            if hint in lowered:
                intent = candidate
                break
    considered: list[str] = []

    # 1) Core state stays Core (§14). Recorded, never executed here.
    if goal.target in CORE_TOOLS or (intent in CORE_TOOLS):
        route = CapabilityRoute(
            route_kind=RouteKind.CORE,
            capability=str(goal.target or intent or "evie_turn"),
            verification_contract="turn_result_canonical",
            fallback_allowed=False,
            rationale_code=Rationale.CORE_STATE_AUTHORITY,
        )
        _record(goal, route, started, considered)
        return route

    # 2) Memory plane never becomes action routing (§15/§32).
    lowered = (goal.goal or "").lower()
    if any(mark in lowered for mark in ("what did i decide", "what did we decide", "do you remember", "recall")):
        route = CapabilityRoute(
            route_kind=RouteKind.MEMORY,
            capability="memory_router",
            verification_contract="shadow_envelope_refs",
            fallback_allowed=False,
            rationale_code=Rationale.MEMORY_PLANE_NOT_ACTION,
        )
        _record(goal, route, started, considered)
        return route

    # 3) Semantic candidates with real availability checks.
    if intent:
        considered.append(intent)
        available, detail = await _semantic_available(intent, session, goal.device_scope)
        risk = _policy_risk_for(intent)
        if goal.device_scope not in {"owner", "master"}:
            route = CapabilityRoute(
                route_kind=RouteKind.UNAVAILABLE,
                capability=intent,
                availability="unavailable",
                rationale_code=Rationale.DEVICE_NOT_TRUSTED,
                policy_risk=risk,
                candidates_considered=considered,
            )
            _record(goal, route, started, considered)
            return route
        if available:
            route = CapabilityRoute(
                route_kind=RouteKind.SEMANTIC,
                capability=intent,
                availability="available",
                expected_effect=goal.expected_effect,
                verification_contract=_verification_contract_for(intent),
                fallback_allowed=True,  # legal only PRE-dispatch (fence enforced)
                rationale_code=Rationale.SEMANTIC_ADAPTER_AVAILABLE,
                policy_risk=risk,
                candidates_considered=considered,
            )
            _record(goal, route, started, considered)
            return route

    # 4) Generic computer fallback for app-control goals.
    if intent in {"open_app", "activate_app"} or (goal.target in {"open_app", "activate_app"}):
        live_ok, live_id = _computer_live_available()
        considered.append("computer_executor:navigate")
        if live_ok:
            route = CapabilityRoute(
                route_kind=RouteKind.GENERIC_COMPUTER,
                capability=str(intent or goal.target),
                executor_family="navigate",
                device=live_id,
                availability="available",
                verification_contract="observe_after_expected_effect",
                rationale_code=Rationale.GENERIC_UI_REQUIRED,
                candidates_considered=considered,
            )
            _record(goal, route, started, considered)
            return route

    route = CapabilityRoute(
        route_kind=RouteKind.UNAVAILABLE,
        capability=str(intent or goal.target or "unknown"),
        availability="unavailable",
        rationale_code=Rationale.CAPABILITY_UNAVAILABLE,
        candidates_considered=considered,
    )
    _record(goal, route, started, considered)
    return route


def _verification_contract_for(capability: str) -> str:
    return {
        "calendar_read": "semantic_api_rows",
        "list_messages": "semantic_api_rows",
        "list_mail": "semantic_api_rows",
        "get_weather": "provider_payload",
        "start_timer": "canonical_timer_row",
        "computer_status": "live_readiness_state",
        "open_app": "observe_after_expected_effect",
        "activate_app": "observe_after_expected_effect",
    }.get(capability, "unknown")


# ---------------------------------------------------------------------------
# Diagnostics (§36/§37): bounded metadata only
# ---------------------------------------------------------------------------

_ROUTE_LOG: deque[dict[str, Any]] = deque(maxlen=256)
_ROUTE_COUNTS: Counter[str] = Counter()


def _record(goal: ActionGoal, route: CapabilityRoute, started: float, considered: list[str]) -> None:
    entry = {
        "turn_id": goal.owner_turn_id,
        "goal_fingerprint": goal.fingerprint(),
        "route_kind": route.route_kind.value,
        "capability": route.capability,
        "availability": route.availability,
        "rationale": route.rationale_code.value,
        "risk": route.policy_risk,
        "verification": route.verification_contract,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "considered": considered[:4],
    }
    _ROUTE_LOG.append(entry)
    _ROUTE_COUNTS[f"kind:{route.route_kind.value}"] += 1
    _ROUTE_COUNTS[f"rationale:{route.rationale_code.value}"] += 1
    try:
        from app.ev.computer_runtime import log_computer

        log_computer("capability.route_selected", extra=entry)
    except Exception:  # noqa: BLE001
        pass


def note_route_outcome(
    *,
    execution_id: str,
    attempted: bool,
    verified: bool,
    error: str | None = None,
    fallback: str | None = None,
) -> None:
    """Post-execution outcome attached to routing health (§37)."""

    _ROUTE_COUNTS["exec:attempted" if attempted else "exec:not_attempted"] += 1
    if attempted:
        _ROUTE_COUNTS["exec:verified" if verified else "exec:verification_failure"] += 1
    if error == "ambiguous_effect":
        _ROUTE_COUNTS["exec:ambiguous_after_attempt"] += 1
    if fallback == "before_dispatch":
        _ROUTE_COUNTS["fallback:before_dispatch"] += 1
    elif fallback == "after_dispatch":
        _ROUTE_COUNTS["fallback:after_dispatch"] += 1  # MUST stay 0


def route_health_snapshot() -> dict[str, Any]:
    total = sum(v for k, v in _ROUTE_COUNTS.items() if k.startswith("kind:")) or 1

    def share(prefix: str) -> float:
        return round(100.0 * _ROUTE_COUNTS.get(prefix, 0) / total, 1)

    return {
        "routed_total": sum(v for k, v in _ROUTE_COUNTS.items() if k.startswith("kind:")),
        "semantic_pct": share("kind:semantic"),
        "core_pct": share("kind:core"),
        "memory_pct": share("kind:memory"),
        "computer_pct": share("kind:generic_computer"),
        "unavailable_pct": share("kind:unavailable"),
        "confirmation_pct": share("kind:policy_confirmation"),
        "verification_failures": _ROUTE_COUNTS.get("exec:verification_failure", 0),
        "ambiguous_after_attempt": _ROUTE_COUNTS.get("exec:ambiguous_after_attempt", 0),
        "fallback_before_dispatch": _ROUTE_COUNTS.get("fallback:before_dispatch", 0),
        "fallback_after_dispatch": _ROUTE_COUNTS.get("fallback:after_dispatch", 0),
        "recent": list(_ROUTE_LOG)[-8:],
    }
