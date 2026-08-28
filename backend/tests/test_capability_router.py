"""F3 capability router: routing hierarchy, parity, fences, no-shearing tests.

Acceptance matrix (F3 §31-35):
  - memory questions stay with the Memory Router (§32)
  - current-state questions stay with Core/evie_turn (§33)
  - semantic timers/calendar/weather route semantic-first (§34)
  - unsupported app actions fall back to the generic computer executor (§35)
  - availability is checked, never assumed (§19)
  - untrusted scope denies (DEVICE_NOT_TRUSTED)
  - dispatch-level shadow: legacy stays authoritative; ON adds fenced
    pre-dispatch generic fallback only
  - route health: fallback-after-dispatch must remain 0
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.ev.capability_router import (
    ActionGoal,
    Rationale,
    RouteKind,
    route_action,
    route_health_snapshot,
)


@pytest.fixture(autouse=True)
def _router_flag():
    previous = settings.capability_router_v2
    yield
    settings.capability_router_v2 = previous


def _goal(goal: str, **kw) -> ActionGoal:
    return ActionGoal(goal=goal, **kw)


# ---------------------------------------------------------------------------
# §32/§33: the router must not steal memory or current-state questions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_questions_stay_with_memory_router(db_session) -> None:
    route = await route_action(_goal("What did I decide about the memory architecture?"), session=db_session)
    assert route.route_kind == RouteKind.MEMORY
    assert route.rationale_code == Rationale.MEMORY_PLANE_NOT_ACTION
    assert route.fallback_allowed is False


@pytest.mark.asyncio
async def test_core_state_routes_to_core(db_session) -> None:
    route = await route_action(
        _goal("Project priority", target="life_project_query"), session=db_session
    )
    assert route.route_kind == RouteKind.CORE
    assert route.rationale_code == Rationale.CORE_STATE_AUTHORITY
    assert route.verification_contract == "turn_result_canonical"


# ---------------------------------------------------------------------------
# §34/§30: semantic-first for capable domains
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_weather_routes_semantic(db_session) -> None:
    route = await route_action(_goal("What's the weather?", semantic_intent="get_weather"), session=db_session)
    assert route.route_kind == RouteKind.SEMANTIC
    assert route.rationale_code == Rationale.SEMANTIC_ADAPTER_AVAILABLE
    assert route.verification_contract == "provider_payload"


@pytest.mark.asyncio
async def test_timer_routes_semantic_not_ui(db_session) -> None:
    """§34: a five-minute timer must not become open-Clock-and-click."""

    route = await route_action(_goal("Start a five-minute timer", semantic_intent="start_timer"), session=db_session)
    assert route.route_kind == RouteKind.SEMANTIC
    assert route.capability == "start_timer"
    assert route.executor_family is None  # not the computer executor
    assert route.verification_contract == "canonical_timer_row"


@pytest.mark.asyncio
async def test_semantic_capability_requires_real_availability(db_session) -> None:
    """§19: calendar_read without an active calendar integration is unavailable."""

    route = await route_action(_goal("What's on my calendar?", semantic_intent="calendar_read"), session=db_session)
    # The test DB has no active calendar integration.
    assert route.availability == "unavailable"
    assert route.route_kind == RouteKind.UNAVAILABLE


# ---------------------------------------------------------------------------
# §35: generic computer fallback when no semantic path exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_navigation_prefers_helper_then_generic(db_session) -> None:
    """§9: semantic helper first; generic executor only when it is absent."""

    from unittest.mock import patch

    from app.voice.live.layer import register_live, reset_live_registry
    from app.voice.live.session import LiveSession

    reset_live_registry()
    session = LiveSession(session_id="f3-generic", device_id="mac", backchannel_enabled=False)
    register_live(session)
    try:
        # No macos_life helper in the test DB -> generic computer route.
        route = await route_action(
            _goal("Turn on the obscure preference inside FooApp", semantic_intent="open_app"),
            session=db_session,
        )
        assert route.route_kind == RouteKind.GENERIC_COMPUTER
        assert route.executor_family == "navigate"
        assert route.rationale_code == Rationale.GENERIC_UI_REQUIRED
        assert route.verification_contract == "observe_after_expected_effect"

        # With a helper integration present -> semantic route wins.
        async def fake_helper(_session):
            return object()

        with patch("app.ev.apps.find_macos_life_integration", fake_helper):
            route = await route_action(
                _goal("Open Calculator", semantic_intent="open_app"), session=db_session
            )
        assert route.route_kind == RouteKind.SEMANTIC
        assert route.rationale_code == Rationale.SEMANTIC_ADAPTER_AVAILABLE
    finally:
        session.close()
        reset_live_registry()


@pytest.mark.asyncio
async def test_generic_fallback_requires_live_device(db_session) -> None:
    from app.voice.live.layer import reset_live_registry

    reset_live_registry()
    route = await route_action(_goal("Open FooApp", semantic_intent="open_app"), session=db_session)
    assert route.route_kind == RouteKind.UNAVAILABLE


# ---------------------------------------------------------------------------
# §19: scope law
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_untrusted_scope_denied(db_session) -> None:
    route = await route_action(
        _goal("What's the weather?", semantic_intent="get_weather", device_scope="sandbox"),
        session=db_session,
    )
    assert route.route_kind == RouteKind.UNAVAILABLE
    assert route.rationale_code == Rationale.DEVICE_NOT_TRUSTED


# ---------------------------------------------------------------------------
# Dispatch-level integration: shadow predicts, ON falls back fenced (§28/§29)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_shadow_records_but_legacy_authoritative(db_session) -> None:
    from app.ev.tools import dispatch

    settings.capability_router_v2 = "shadow"
    result = await dispatch(db_session, "get_weather", {}, actor="master")
    assert result.ok is True or "weather" in str(result.error).lower()
    health = route_health_snapshot()
    assert health["routed_total"] >= 1
    assert health["fallback_after_dispatch"] == 0  # permanent law


@pytest.mark.asyncio
async def test_dispatch_on_mode_fenced_fallback_no_live(db_session) -> None:
    """ON + no live device + helper unavailable → honest unavailability (no crash,
    no fallback-after-dispatch)."""

    from app.ev.tools import dispatch
    from app.voice.live.layer import reset_live_registry

    settings.capability_router_v2 = "on"
    reset_live_registry()
    result = await dispatch(db_session, "open_app", {"name": "Music"}, actor="master")
    body = result.result if isinstance(result.result, dict) else {}
    # No live session and no helper: the failure must be honest, not invented.
    assert result.ok is False or body.get("ok") is False or body.get("degraded") is True
    assert route_health_snapshot()["fallback_after_dispatch"] == 0


@pytest.mark.asyncio
async def test_router_health_records_kinds(db_session) -> None:
    await route_action(_goal("What did we decide about IIT?"), session=db_session)
    await route_action(_goal("weather?", semantic_intent="get_weather"), session=db_session)
    health = route_health_snapshot()
    assert health["memory_pct"] > 0 or health["semantic_pct"] > 0
    assert health["fallback_after_dispatch"] == 0
