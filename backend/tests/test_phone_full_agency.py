"""A phone turn can reach the whole system, not just three tools.

Why this file exists
--------------------
A phone used to be offered ``phone_action``, ``phone.read`` and
``capability.discover`` and nothing else, and its device-local dispatch refused
the Home Station fallback. Every Core and Home Station capability was therefore
unreachable from the device the owner actually carries: "open the calculator",
"what is the last email", "what was my last iMessage", "send a WhatsApp to X"
all ended in a spoken dead end even though the Mac could do every one of them.

These tests lock the corrected shape:

* a phone turn is offered the semantic bus **plus** its own local actuators;
* a device-local tool still goes to the phone adapter (never silently to Core);
* a semantic tool goes to the same executor every other surface uses, and it
  carries the authority binding of the turn that opened it;
* ``home.act`` is the universal route, and it labels its result honestly;
* the owner's own name is stored only from the owner's own words.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.contracts import ChatResult, ToolCall
from app.models import Device


def _phone(role: str = "primary_companion") -> Device:
    return Device(
        name="Test iPhone",
        token_hash="b" * 64,
        role=role,
        platform="ios",
        device_type="phone",
        memory_scope="owner",
    )


# --------------------------------------------------------------------------
# The bus itself
# --------------------------------------------------------------------------


def test_phone_turn_bus_is_the_semantic_bus_plus_local_actuators() -> None:
    from app.device_gateway.cognitive_phone import phone_turn_specs

    offered = {spec.name for spec in phone_turn_specs(compact=False)}

    # Local actuators must always be present: there is no other route to a
    # device-local effect.
    assert {"phone_action", "phone.read", "capability.discover"} <= offered
    # Every capability the owner asked for on the phone. Each of these was
    # unreachable before, which is the bug this file guards.
    assert {
        "home.act",
        "life.mail",
        "life.messages",
        "life.send",
        "timer.act",
        "weather.get",
        "people.lookup",
        "owner.profile",
        "memory.search",
    } <= offered
    # And the Mac-side actuators, so the phone can ask for them explicitly.
    assert {"files.act", "code.act", "computer.perform_effect", "digital.act"} <= offered


def test_phone_local_tools_are_not_offered_to_a_non_phone_surface() -> None:
    """A Mac/web turn must never be handed a phone-local actuator."""

    from app.cognitive.speed import tool_specs_for_turn

    names = {spec.name for spec in tool_specs_for_turn(compact=False)}
    assert "phone_action" not in names
    assert "phone.read" not in names
    # The universal route is fine everywhere: on the Mac it resolves to the Mac.
    assert "home.act" in names


def test_capability_discover_still_withholds_action_credentials() -> None:
    from app.device_gateway import cognitive_phone

    spec = next(s for s in cognitive_phone.phone_tool_specs() if s.name == "phone_action")
    assert "confirm_action_id" not in spec.parameters["properties"]


# --------------------------------------------------------------------------
# Routing: local tool -> phone adapter, semantic tool -> the shared executor
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["primary_companion", "secondary_companion"])
async def test_phone_turn_reaches_a_home_station_tool(monkeypatch, tmp_path, db_session, role):
    """The model asks for ``life.mail`` on a phone turn and Core answers it."""

    from app.cognitive import kernel
    from app.cognitive.session_store import reset_for_tests
    from app.device_gateway import cognitive_phone

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    reset_for_tests()
    device = _phone(role)
    db_session.add(device)
    await db_session.flush()

    phone_tool = AsyncMock(side_effect=AssertionError("semantic tool used the phone adapter"))
    monkeypatch.setattr(cognitive_phone, "execute_phone_tool", phone_tool)
    semantic = AsyncMock(
        return_value={"ok": True, "spoken": "You have 3 unread messages.", "executed": False}
    )
    monkeypatch.setattr(kernel, "execute_semantic", semantic)
    monkeypatch.setattr(kernel, "muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr(kernel, "should_prefetch_memory", lambda **kwargs: False)
    seen: list[list[str]] = []

    class Muse:
        def __init__(self) -> None:
            self.step = 0

        async def chat_with_tools(self, messages, specs, **kwargs):
            self.step += 1
            seen.append([spec.name for spec in specs])
            if self.step == 1:
                return ChatResult(
                    text="",
                    tool_calls=[ToolCall(id="c1", name="life.mail", arguments={"query": "unread"})],
                )
            return ChatResult(text="You have 3 unread messages.")

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", Muse)
    try:
        result = await kernel.handle_turn(
            transcript="What is the last email I got?",
            device_id=str(device.id),
            live_session_id="phone-session",
            session=db_session,
        )
        assert result.kind == "muse"
        semantic.assert_awaited_once()
        # Authority travels with the call, and the device is bound to the turn.
        assert semantic.await_args.args[1] == "life.mail"
        assert semantic.await_args.kwargs["device_id"] == str(device.id)
        phone_tool.assert_not_awaited()
        assert "life.mail" in seen[0]
    finally:
        reset_for_tests()


async def test_device_local_tool_still_goes_to_the_phone_adapter(monkeypatch, tmp_path, db_session):
    """The fallback does not turn a device-local action into a Core action."""

    from app.cognitive import kernel
    from app.cognitive.session_store import reset_for_tests
    from app.device_gateway import cognitive_phone

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    reset_for_tests()
    device = _phone()
    db_session.add(device)
    await db_session.flush()

    phone_tool = AsyncMock(
        return_value={"ok": True, "executed": False, "spoken": "Timer card is on this phone."}
    )
    monkeypatch.setattr(cognitive_phone, "execute_phone_tool", phone_tool)
    semantic = AsyncMock(side_effect=AssertionError("device-local tool reached the Core executor"))
    monkeypatch.setattr(kernel, "execute_semantic", semantic)
    monkeypatch.setattr(kernel, "muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr(kernel, "should_prefetch_memory", lambda **kwargs: False)

    class Muse:
        def __init__(self) -> None:
            self.step = 0

        async def chat_with_tools(self, messages, specs, **kwargs):
            self.step += 1
            if self.step == 1:
                return ChatResult(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="phone_action",
                            arguments={"operation": "create_timer", "duration_seconds": 60},
                        )
                    ],
                )
            return ChatResult(text="Timer card is on this phone.")

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", Muse)
    try:
        await kernel.handle_turn(
            transcript="Set a timer for one minute",
            device_id=str(device.id),
            live_session_id="phone-session",
            session=db_session,
        )
        phone_tool.assert_awaited_once()
        semantic.assert_not_awaited()
    finally:
        reset_for_tests()


# --------------------------------------------------------------------------
# home.act — the universal route
# --------------------------------------------------------------------------


async def _run_home_act(monkeypatch, db_session, *, broker_result, request="open the calculator"):
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import CognitiveSession
    from app.device_gateway import phone_mac

    device = _phone()
    db_session.add(device)
    await db_session.flush()
    broker = AsyncMock(return_value=broker_result)
    monkeypatch.setattr(phone_mac, "maybe_phone_mac_act", broker)
    result = await execute_semantic(
        db_session,
        "home.act",
        {"request": request},
        cognition=CognitiveSession(session_id="test-session"),
        actor=f"device:{device.name}",
        live_session_id=None,
        steering_seen=0,
        device_id=str(device.id),
    )
    return result, broker


async def test_home_act_runs_on_home_station_and_says_so(monkeypatch, db_session):
    result, broker = await _run_home_act(
        monkeypatch,
        db_session,
        broker_result={
            "reply": "Calculator is open on Home Station.",
            "ok": True,
            "accepted": True,
            "executed": True,
            "verified": True,
            "route": "HOME_STATION",
            "tool": "open_app",
        },
    )
    broker.assert_awaited_once()
    assert broker.await_args.kwargs["text"] == "open the calculator"
    assert result["ok"] is True
    assert result["executed"] is True
    # The effect is labelled: it never claims the phone ran it.
    assert result["executed_on"] == "home_station"
    assert result["operation"] == "open_app"


async def test_home_act_is_honest_when_home_station_has_no_path(monkeypatch, db_session):
    result, _broker = await _run_home_act(monkeypatch, db_session, broker_result=None)
    assert result["ok"] is False
    assert result["diagnosis"] == "HOME_STATION_NO_PATH"
    assert result["executed"] is False
    assert result["executed_on"] == "home_station"


async def test_home_act_needs_a_bound_device(monkeypatch, db_session):
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import CognitiveSession

    result = await execute_semantic(
        db_session,
        "home.act",
        {"request": "open the calculator"},
        cognition=CognitiveSession(session_id="test-session"),
        actor="device:unknown",
        live_session_id=None,
        steering_seen=0,
        device_id=None,
    )
    assert result["ok"] is False


async def test_home_act_refuses_an_empty_request(monkeypatch, db_session):
    result, broker = await _run_home_act(monkeypatch, db_session, broker_result={}, request="   ")
    assert result["ok"] is False
    broker.assert_not_awaited()


# --------------------------------------------------------------------------
# owner.profile — the owner's own name
# --------------------------------------------------------------------------


async def test_owner_profile_reads_then_stores_the_name(db_session):
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import CognitiveSession

    async def call(args):
        return await execute_semantic(
            db_session,
            "owner.profile",
            args,
            cognition=CognitiveSession(session_id="test-session"),
            actor="device:Test iPhone",
            live_session_id=None,
            steering_seen=0,
        )

    first = await call({"op": "get"})
    assert first["ok"] is True
    assert first["name"] is None
    assert "name" in first["spoken"].lower()

    stored = await call({"op": "set", "name": "Sahaj"})
    assert stored["ok"] is True
    assert stored["name"] == "Sahaj"
    assert stored["executed"] is True

    again = await call({"op": "get"})
    assert again["name"] == "Sahaj"
    assert "Sahaj" in again["spoken"]


async def test_owner_profile_refuses_an_empty_name(db_session):
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import CognitiveSession

    result = await execute_semantic(
        db_session,
        "owner.profile",
        {"op": "set", "name": "   "},
        cognition=CognitiveSession(session_id="test-session"),
        actor="device:Test iPhone",
        live_session_id=None,
        steering_seen=0,
    )
    assert result["ok"] is False


# --------------------------------------------------------------------------
# Authority: a semantic tool is bound to the turn that opened it
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# The safety net: a mind outage must not become a dead end
# --------------------------------------------------------------------------


async def test_phone_turn_acts_on_home_station_when_the_mind_is_down(monkeypatch, db_session):
    """Provider down → the deterministic lane still carries out a real action."""

    from uuid import uuid4

    from app.device_gateway import phone_core, phone_mac, turn_receipts

    monkeypatch.setattr(settings, "cognitive_mode", "muse_kernel")

    async def provider_down(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.cognitive.kernel.handle_turn", provider_down)
    monkeypatch.setattr(phone_core, "maybe_phone_core_read", AsyncMock(return_value=None))
    monkeypatch.setattr(
        phone_mac,
        "maybe_phone_mac_act",
        AsyncMock(
            return_value={
                "reply": "Your 5-minute timer is set on Home Station.",
                "ok": True,
                "accepted": True,
                "executed": True,
                "verified": True,
                "route": "HOME_STATION",
                "tool": "start_timer",
            }
        ),
    )

    device = _phone()
    db_session.add(device)
    await db_session.flush()
    receipt = await turn_receipts.record_turn_receipt(
        db_session,
        device=device,
        idempotency_key="fallback-" + uuid4().hex[:12],
        transcript="Set a timer for 5 minutes",
        session_id="sess-fallback",
    )
    await db_session.commit()

    assert receipt["core_route"] == "HOME_STATION"
    assert "Home Station" in receipt["core_reply"]
    assert receipt["action_tool"] == "start_timer"
    assert receipt["action_executed"] is True
    # And the failure is visible rather than disguised as an ordinary refusal.
    assert receipt["kernel_failed"] is True
    assert receipt["kernel_error"] == "RuntimeError"


async def test_phone_turn_is_honest_when_neither_lane_can_help(monkeypatch, db_session):
    """Nothing can serve the request: say so plainly, do not invent an answer."""

    from uuid import uuid4

    from app.device_gateway import phone_core, phone_mac, turn_receipts

    monkeypatch.setattr(settings, "cognitive_mode", "muse_kernel")

    async def provider_down(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("app.cognitive.kernel.handle_turn", provider_down)
    monkeypatch.setattr(phone_core, "maybe_phone_core_read", AsyncMock(return_value=None))
    monkeypatch.setattr(phone_mac, "maybe_phone_mac_act", AsyncMock(return_value=None))

    device = _phone()
    db_session.add(device)
    await db_session.flush()
    receipt = await turn_receipts.record_turn_receipt(
        db_session,
        device=device,
        idempotency_key="dead-" + uuid4().hex[:12],
        transcript="Tell me a story about Saturn",
        session_id="sess-dead",
    )
    await db_session.commit()

    assert receipt["core_route"] == "unavailable"
    assert "can't think" in receipt["core_reply"].lower()
    assert receipt["kernel_failed"] is True


async def test_phone_turn_authority_guard_flags_a_moved_conversation(monkeypatch, db_session):
    from app.device_gateway import cognitive_phone

    monkeypatch.setattr(
        cognitive_phone,
        "capture_phone_binding",
        AsyncMock(return_value=SimpleNamespace(identity=("moved",))),
    )
    changed = await cognitive_phone.phone_turn_authority_changed(
        db_session,
        device_id="11111111-1111-1111-1111-111111111111",
        live_session_id="phone-session",
        expected_binding=SimpleNamespace(identity=("original",)),
        text_context=None,
    )
    assert changed == "PHONE_CONTEXT_CHANGED"


async def test_phone_turn_authority_guard_passes_an_unchanged_conversation(monkeypatch, db_session):
    from app.device_gateway import cognitive_phone

    binding = SimpleNamespace(identity=("same",))
    monkeypatch.setattr(
        cognitive_phone, "capture_phone_binding", AsyncMock(return_value=binding)
    )
    changed = await cognitive_phone.phone_turn_authority_changed(
        db_session,
        device_id="11111111-1111-1111-1111-111111111111",
        live_session_id="phone-session",
        expected_binding=binding,
        text_context=None,
    )
    assert changed is None


async def test_typed_phone_authority_guard_flags_a_lost_lease(db_session):
    from app.device_gateway.cognitive_text import PhoneTextContext, text_context_still_current

    device = _phone()
    db_session.add(device)
    await db_session.flush()
    context = PhoneTextContext(
        device_id=str(device.id),
        instance_id="instance",
        lease_id="lease",
        generation=1,
        auth_revision=int(device.auth_revision or 1),
        origin="https://home.ts.net",
    )
    # No lease is held for this instance, so the typed turn no longer owns it.
    assert await text_context_still_current(db_session, context=context) == "PHONE_CONTEXT_CHANGED"


async def test_typed_phone_authority_guard_flags_a_revoked_device(db_session):
    from app.device_gateway.cognitive_text import PhoneTextContext, text_context_still_current
    from app.utils.text import utcnow

    device = _phone()
    db_session.add(device)
    await db_session.flush()
    context = PhoneTextContext(
        device_id=str(device.id),
        instance_id="instance",
        lease_id="lease",
        generation=1,
        auth_revision=int(device.auth_revision or 1),
        origin="https://home.ts.net",
    )
    device.revoked_at = utcnow()
    await db_session.flush()
    assert await text_context_still_current(db_session, context=context) == "DEVICE_TRUST_CHANGED"
