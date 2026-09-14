"""The phone knows what it is, what it can do, and every route it can reach.

Giving a phone the full tool bus was only half the fix. The kernel prompt had
been written for the Mac and never said which device was speaking, so the mind
had no model of its own reach: it knew the tool names it was handed but not
that this device has no shell, no Clock, no Mail, and a paired Home Station that
carries out everything else. That is why "I can't do that on the phone" kept
appearing while the system could plainly do the thing.

These tests lock the self-model and the three surfaces that must agree on it:
the system prompt, ``capability.discover``, and the spoken capabilities answer.
"""

from __future__ import annotations

from app.cognitive.context import compile_context
from app.cognitive.session_store import CognitiveSession
from app.models import Device


def _phone() -> Device:
    return Device(
        name="Test iPhone",
        token_hash="d" * 64,
        role="primary_companion",
        platform="ios",
        device_type="phone",
        memory_scope="owner",
    )


def _state(**overrides) -> dict:
    state = {
        "device": "Test iPhone",
        "role": "primary_companion",
        "origin": "iPhone",
        "native_shell": False,
        "permissions": {},
        "local_available": ["create_timer", "open_app", "message_contact"],
        "local_unavailable": {"create_alarm": "Needs the Evie iPhone app"},
        "home_station": ["list_mail", "start_timer", "send_message", "place_call"],
    }
    state.update(overrides)
    return state


# --------------------------------------------------------------------------
# The self-model itself
# --------------------------------------------------------------------------


def test_self_model_separates_local_from_home_station(monkeypatch) -> None:
    from app.device_gateway import cognitive_phone
    from app.device_gateway.mobile_actions import service

    monkeypatch.setattr(
        service,
        "status_snapshot",
        lambda **kwargs: {
            "native_shell_connected": False,
            "capabilities": [
                {"operation": "create_timer", "available": True, "reason": None},
                {"operation": "open_app", "available": True, "reason": None},
                {"operation": "create_alarm", "available": False,
                 "reason": "Needs the Evie iPhone app"},
            ],
            # A launch URL / credential must never reach the model.
            "last_action": {"launch_url": "https://private.example/secret", "token": "secret"},
        },
    )
    model = cognitive_phone.phone_self_model(_phone())

    assert model["origin"] == "iPhone"
    assert model["native_shell"] is False
    assert sorted(model["local_available"]) == ["create_timer", "open_app"]
    assert model["local_unavailable"] == {"create_alarm": "Needs the Evie iPhone app"}
    assert "list_mail" in model["home_station"]
    assert "secret" not in str(model)


def test_self_model_survives_a_missing_broker(monkeypatch) -> None:
    from app.device_gateway import cognitive_phone
    from app.device_gateway.mobile_actions import service

    def boom(**kwargs):
        raise RuntimeError("no broker")

    monkeypatch.setattr(service, "status_snapshot", boom)
    model = cognitive_phone.phone_self_model(_phone())
    # The prompt must still compile; it just has less to say.
    assert model["origin"] == "iPhone"
    assert model["home_station"]


# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------


def _prompt(phone_state) -> str:
    return compile_context(
        transcript="open the calculator",
        modality="voice",
        device_id="11111111-1111-1111-1111-111111111111",
        cognition=CognitiveSession(session_id="t"),
        capability_names=["phone_action", "phone.read", "home.act", "life.mail", "timer.act"],
        phone_state=phone_state,
    )


def test_prompt_tells_the_phone_it_is_a_phone() -> None:
    prompt = _prompt(_state())
    assert "DEVICE —" in prompt
    assert "on the owner's iPhone" in prompt
    # The limits it must not pretend away.
    assert "no shell" in prompt
    assert "no Clock app" in prompt
    assert "create_timer" in prompt
    assert "create_alarm: Needs the Evie iPhone app" in prompt
    # And the machine that does the rest.
    assert "HOME STATION" in prompt
    assert "list_mail" in prompt


def test_prompt_carries_the_routing_ladder_in_order() -> None:
    prompt = _prompt(_state())
    assert "ROUTING" in prompt
    local = prompt.index("1. this iPhone")
    semantic = prompt.index("2. Core or Home Station")
    universal = prompt.index("3. home.act")
    honest = prompt.index("4. only when every route returned a real failure")
    assert local < semantic < universal < honest
    # The whole point: refusing while a route is open is not allowed.
    assert "never answer that you cannot" in prompt
    # And no effect may be claimed for the wrong machine.
    assert "executed_on" in prompt


def test_prompt_withholds_the_doctrine_from_a_non_phone() -> None:
    prompt = _prompt(None)
    assert "DEVICE —" not in prompt
    assert "on the owner's iPhone" not in prompt


def test_prompt_names_the_universal_route_for_a_phone() -> None:
    prompt = _prompt(_state())
    assert "home.act" in prompt
    assert "owner.profile" in prompt
    assert "phone.read" in prompt


# --------------------------------------------------------------------------
# capability.discover
# --------------------------------------------------------------------------


def test_discover_on_a_phone_merges_local_and_core() -> None:
    from app.cognitive.executor import _discover

    out = _discover({}, phone_state=_state())
    assert out["ok"] is True
    semantic = {row.get("name") for row in out["capabilities"]}
    assert {"life.mail", "life.send", "timer.act"} <= semantic
    device = out["device"]
    assert device["origin"] == "iPhone"
    assert device["native_shell"] is False
    available = [row for row in device["local_actions"] if row["available"]]
    assert {row["operation"] for row in available} == {
        "create_timer", "open_app", "message_contact",
    }
    blocked = [row for row in device["local_actions"] if not row["available"]]
    assert blocked and blocked[0]["reason"] == "Needs the Evie iPhone app"
    assert "list_mail" in device["home_station_actions"]


def test_discover_without_a_phone_is_unchanged() -> None:
    from app.cognitive.executor import _discover

    out = _discover({}, phone_state=None)
    assert set(out.keys()) == {"ok", "capabilities"}
    assert out["capabilities"]


def test_discover_carries_no_credentials() -> None:
    from app.cognitive.executor import _discover

    out = _discover({}, phone_state=_state())

    def keys(node) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            for name, value in node.items():
                found.add(str(name).lower())
                found |= keys(value)
        elif isinstance(node, list):
            for item in node:
                found |= keys(item)
        return found

    # Keys, not prose: several tool descriptions legitimately tell the model to
    # never pass a token, which is not the same as carrying one.
    banned = {"launch_url", "open_url", "token", "receipt", "access_token",
              "refresh_token", "cookie", "api_key", "authorization", "secret"}
    assert banned.isdisjoint(keys(out))
    assert "https://" not in str(out.get("device"))


async def test_discover_through_the_executor_on_a_phone(db_session) -> None:
    from app.cognitive.executor import execute_semantic

    out = await execute_semantic(
        db_session,
        "capability.discover",
        {},
        cognition=CognitiveSession(session_id="t"),
        actor="device:Test iPhone",
        live_session_id=None,
        steering_seen=0,
        phone_state=_state(),
    )
    assert out["ok"] is True
    assert out["device"]["local_actions"]


# --------------------------------------------------------------------------
# The spoken answer must agree with the model
# --------------------------------------------------------------------------


async def test_spoken_capabilities_reflect_the_device(monkeypatch, db_session) -> None:
    from app.device_gateway import phone_core

    monkeypatch.setattr(
        phone_core, "phone_self_model", lambda device: _state(), raising=False
    )
    device = _phone()
    db_session.add(device)
    await db_session.flush()
    result = await phone_core.maybe_phone_core_read(
        db_session, device=device, text="what can you do?"
    )
    assert result is not None
    assert result["route"] == "CAPABILITIES"
    spoken = str(result["reply"])
    # It must mention both halves, and not overclaim a native app.
    assert "Home Station" in spoken
    assert "create timer" in spoken or "create_timer" in spoken
    assert result["executed"] is True
    assert result["native_shell"] is False


async def test_spoken_capabilities_never_crash_without_a_broker(monkeypatch, db_session) -> None:
    from app.device_gateway import phone_core

    def boom(device):
        raise RuntimeError("no broker")

    monkeypatch.setattr(phone_core, "phone_self_model", boom, raising=False)
    device = _phone()
    db_session.add(device)
    await db_session.flush()
    result = await phone_core.maybe_phone_core_read(
        db_session, device=device, text="what can you do?"
    )
    assert result is not None
    assert result["ok"] is True
    assert result["reply"]
