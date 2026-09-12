"""Spark capability coverage: dead-end channels reachable through execute_semantic.

Offline only: real backing is monkeypatched, presence runs on the sqlite test DB.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cognitive.executor import execute_semantic
from app.cognitive.session_store import CognitiveSession, reset_for_tests


@pytest.fixture(autouse=True)
def _clean_cognition() -> Any:
    reset_for_tests()
    yield
    reset_for_tests()


def _cognition() -> CognitiveSession:
    return CognitiveSession(session_id="capcov")


async def _run(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return await execute_semantic(
        None,
        name,
        arguments,
        cognition=_cognition(),
        actor="master",
        live_session_id=None,
        steering_seen=0,
    )


def _install_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def fake_dispatch(session, name, arguments, **kwargs):
        seen["name"] = name
        seen["args"] = dict(arguments or {})
        seen["kwargs"] = kwargs
        if error is not None:
            raise error
        return dict(result or {"ok": True, "spoken": "done."})

    monkeypatch.setattr("app.ev.tools.dispatch", fake_dispatch)
    return seen


# --- (a) every new spec is advertised to Spark ---


def test_new_capability_specs_are_advertised() -> None:
    from app.cognitive.capabilities import tool_specs

    specs = {spec.name: spec for spec in tool_specs()}
    expected = {
        "timer.act": "R1",
        "weather.get": "R0",
        "life.state": "R2",
        "notify.schedule": "R1",
        "phone.call": "R3",
        # home.act is the universal Home Station route: it is what stops a
        # device with no local machinery (an iPhone in Safari) from reaching a
        # spoken dead end. owner.profile is how the owner teaches their name.
        "home.act": "R2",
        "owner.profile": "R1",
    }
    for name, risk in expected.items():
        assert name in specs, name
        assert specs[name].risk_class == risk
    # 23 existing semantic tools plus these two additions.
    assert len(specs) == 25


# --- weather.get ---


async def test_weather_get_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_dispatch(
        monkeypatch, result={"ok": True, "results": [{"title": "Weather in Lisbon"}]}
    )
    out = await _run("weather.get", {"place": "Lisbon", "query": "will it rain"})
    assert seen["name"] == "get_weather"
    assert seen["args"] == {"place": "Lisbon", "query": "will it rain"}
    assert out["ok"] is True
    assert out["results"][0]["title"] == "Weather in Lisbon"


async def test_weather_get_backing_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_dispatch(monkeypatch, error=RuntimeError("no route to open-meteo"))
    out = await _run("weather.get", {"place": "Lisbon"})
    assert out["ok"] is False
    assert out["error"] == "CAPABILITY_UNAVAILABLE"
    assert out["diagnosis"] == "RuntimeError"
    assert out["spoken"]


# --- timer.act ---


async def test_timer_act_start_maps_seconds_to_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_dispatch(monkeypatch, result={"ok": True, "spoken": "Timer set."})
    out = await _run("timer.act", {"op": "start", "label": "pasta", "seconds": 120})
    assert seen["name"] == "start_timer"
    assert seen["args"]["text"] == "pasta"
    assert seen["args"]["minutes"] == pytest.approx(2.0)
    assert out["ok"] is True


async def test_timer_act_unknown_op_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_dispatch(monkeypatch)
    out = await _run("timer.act", {"op": "explode"})
    assert out["ok"] is False
    assert out["error"] == "CAPABILITY_UNAVAILABLE"
    assert out["diagnosis"] == "CAPABILITY_UNAVAILABLE"


# --- life.state ---


async def test_life_state_routes_canonical_op(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _install_dispatch(monkeypatch, result={"ok": True, "projects": [{"id": "p1"}]})
    out = await _run("life.state", {"op": "life_project_query", "args": {"priority": "LOW"}})
    assert seen["name"] == "life_project_query"
    assert seen["args"] == {"priority": "LOW"}
    assert out["ok"] is True
    assert out["projects"][0]["id"] == "p1"


async def test_life_state_unknown_op_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_dispatch(monkeypatch)
    out = await _run("life.state", {"op": "life_teleport_everyone"})
    assert out["ok"] is False
    assert out["error"] == "CAPABILITY_UNAVAILABLE"
    assert out["diagnosis"] == "CAPABILITY_UNAVAILABLE"


# --- notify.schedule ---


async def test_notify_schedule_creates_durable_contract_with_notify_node(
    db_session: AsyncSession,
) -> None:
    out = await execute_semantic(
        db_session,
        "notify.schedule",
        {"op": "schedule", "objective": "check the laundry", "when": "in 30 minutes"},
        cognition=_cognition(),
        actor="master",
        live_session_id=None,
        steering_seen=0,
    )
    assert out["ok"] is True
    assert out["state"] == "WAITING_FOR_CONDITION"
    assert out["scheduled_for"]

    from sqlalchemy import select

    from app.models import PresenceCondition
    from app.presence.service import get_contract

    row = await get_contract(db_session, out["contract_id"])
    assert row is not None and row.state == "WAITING_FOR_CONDITION"
    cond = (
        (
            await db_session.execute(
                select(PresenceCondition).where(PresenceCondition.contract_id == row.id)
            )
        )
        .scalars()
        .all()
    )
    assert any(c.cond_class == "TIME" and c.state == "PENDING" for c in cond)
    nodes = (row.graph or {}).get("nodes") or []
    assert any(n.get("kind") == "NOTIFY" for n in nodes)


async def test_notify_schedule_immediate_contract_is_active(
    db_session: AsyncSession,
) -> None:
    out = await execute_semantic(
        db_session,
        "notify.schedule",
        {"op": "schedule", "objective": "stretch break"},
        cognition=_cognition(),
        actor="master",
        live_session_id=None,
        steering_seen=0,
    )
    assert out["ok"] is True
    assert out["state"] == "ACTIVE"
    assert out["scheduled_for"] is None


async def test_notify_schedule_missing_objective_fails_closed(
    db_session: AsyncSession,
) -> None:
    out = await execute_semantic(
        db_session,
        "notify.schedule",
        {"op": "schedule"},
        cognition=_cognition(),
        actor="master",
        live_session_id=None,
        steering_seen=0,
    )
    assert out["ok"] is False
    assert out["error"] == "MISSING_OBJECTIVE"
    assert out["diagnosis"] == "MISSING_OBJECTIVE"


async def test_notify_schedule_list_and_cancel(db_session: AsyncSession) -> None:
    made = await execute_semantic(
        db_session,
        "notify.schedule",
        {"op": "schedule", "objective": "call the pharmacy"},
        cognition=_cognition(),
        actor="master",
        live_session_id=None,
        steering_seen=0,
    )
    listing = await execute_semantic(
        db_session,
        "notify.schedule",
        {"op": "list"},
        cognition=_cognition(),
        actor="master",
        live_session_id=None,
        steering_seen=0,
    )
    assert listing["ok"] is True
    assert any(c["contract_id"] == made["contract_id"] for c in listing["contracts"])
    cancelled = await execute_semantic(
        db_session,
        "notify.schedule",
        {"op": "cancel", "contract_id": made["contract_id"]},
        cognition=_cognition(),
        actor="master",
        live_session_id=None,
        steering_seen=0,
    )
    assert cancelled["ok"] is True
    assert cancelled["cancelled"] is True
    missing = await execute_semantic(
        db_session,
        "notify.schedule",
        {"op": "cancel", "contract_id": "00000000-0000-0000-0000-000000000000"},
        cognition=_cognition(),
        actor="master",
        live_session_id=None,
        steering_seen=0,
    )
    assert missing["ok"] is False
    assert missing["error"] == "CONTRACT_NOT_FOUND"


async def test_notify_schedule_unknown_op_fails_closed(db_session: AsyncSession) -> None:
    out = await execute_semantic(
        db_session,
        "notify.schedule",
        {"op": "broadcast"},
        cognition=_cognition(),
        actor="master",
        live_session_id=None,
        steering_seen=0,
    )
    assert out["ok"] is False
    assert out["error"] == "CAPABILITY_UNAVAILABLE"
    assert out["diagnosis"] == "CAPABILITY_UNAVAILABLE"


# --- phone.call ---


def _fake_live() -> SimpleNamespace:
    return SimpleNamespace(
        device_id="dev-1",
        device_role="companion",
        instance_id="inst-1",
        session_id="live-1",
        gateway_origin="https://phone.local",
        device_label="Owner iPhone",
    )


async def test_phone_call_maps_to_registry_op_and_passes_confirmation_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_live()
    monkeypatch.setattr("app.voice.live.layer.active_lives", lambda: [fake])
    confirmation = {
        "ok": False,
        "executed": False,
        "confirmation_required": True,
        "action_id": "act-77",
        "spoken": "Send 'running late' to Mom?",
    }
    seen: dict[str, Any] = {}

    async def fake_dispatch_phone_action(**kwargs):
        seen.update(kwargs)
        return dict(confirmation)

    monkeypatch.setattr(
        "app.device_gateway.mobile_actions.tool.dispatch_phone_action",
        fake_dispatch_phone_action,
    )
    out = await _run(
        "phone.call", {"op": "message", "contact": "Mom", "message": "running late"}
    )
    assert seen["device_id"] == "dev-1"
    assert seen["role"] == "companion"
    assert seen["session_id"] == "live-1"
    assert seen["arguments"]["operation"] == "message_contact"
    assert seen["arguments"]["contact_query"] == "Mom"
    assert seen["arguments"]["message"] == "running late"
    # Structured confirmation_required shapes pass through for Spark to retry.
    assert out == confirmation


async def test_phone_call_without_live_phone_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.voice.live.layer.active_lives", lambda: [])
    out = await _run("phone.call", {"op": "call", "contact": "Mom"})
    assert out["ok"] is False
    assert out["error"] == "PHONE_NOT_CONNECTED"
    assert out["diagnosis"] == "PHONE_NOT_CONNECTED"


async def test_phone_call_missing_contact_fails_closed() -> None:
    out = await _run("phone.call", {"op": "call"})
    assert out["ok"] is False
    assert out["error"] == "MISSING_CONTACT"
    assert out["diagnosis"] == "MISSING_CONTACT"


async def test_phone_call_unknown_op_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.voice.live.layer.active_lives", lambda: [_fake_live()])
    out = await _run("phone.call", {"op": "podcast", "contact": "Mom"})
    assert out["ok"] is False
    assert out["error"] == "CAPABILITY_UNAVAILABLE"
    assert out["diagnosis"] == "CAPABILITY_UNAVAILABLE"


# --- unknown names still fail closed exactly as before ---


async def test_unknown_tool_name_returns_capability_unavailable() -> None:
    out = await _run("teleport.owner", {})
    assert out["ok"] is False
    assert out["error"] == "CAPABILITY_UNAVAILABLE"
    assert out["diagnosis"] == "CAPABILITY_UNAVAILABLE"
