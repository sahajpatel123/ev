"""Typed phone turns use the real kernel and broker, without a live session."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.contracts import ChatResult, ToolCall
from app.device_gateway.cognitive_text import run_phone_text
from app.device_gateway.lease import claim_lease, current_lease
from app.device_gateway.mobile_actions.store import get_action, reset_for_tests
from app.models import Device


@pytest.fixture
def text_kernel(monkeypatch, tmp_path):
    from app.cognitive import kernel
    from app.cognitive.session_store import reset_for_tests as reset_cognition

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "cognitive_mode", "muse_kernel")
    monkeypatch.setattr(kernel, "muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr(kernel, "should_prefetch_memory", lambda **kwargs: False)
    mac = AsyncMock(side_effect=AssertionError("Typed phone request reached Mac executor"))
    monkeypatch.setattr(kernel, "execute_semantic", mac)
    monkeypatch.setattr("app.cognitive.executor.execute_semantic", mac)
    reset_cognition()
    reset_for_tests()
    yield mac
    reset_for_tests()
    reset_cognition()


def scripted_phone_model(monkeypatch, operation, arguments, before_action=None):
    calls = []

    class Muse:
        def __init__(self):
            self.step = 0

        async def chat_with_tools(self, messages, specs, **kwargs):
            self.step += 1
            calls.append(list(messages))
            offered = {s.name for s in specs}
            # A phone turn is offered the whole semantic bus plus its own local
            # actuators. Offering only three tools is what made every Core and
            # Home Station capability unreachable from the owner's phone.
            assert {"phone_action", "phone.read", "capability.discover"} <= offered
            assert {
                "home.act",
                "life.mail",
                "life.messages",
                "life.send",
                "timer.act",
                "weather.get",
                "owner.profile",
            } <= offered
            if self.step == 1:
                if before_action is not None:
                    await before_action()
                return ChatResult(text="", tool_calls=[ToolCall(
                    id="text-action", name="phone_action",
                    arguments={"operation": operation, **arguments},
                )])
            return ChatResult(text="Check the action card on this phone.")

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", Muse)
    return calls


@pytest.mark.parametrize("role", ["primary_companion", "secondary_companion"])
async def test_text_timer_without_talk_and_durable_retry(text_kernel, monkeypatch, db_session, role):
    device = Device(name="Typed phone", role=role, platform="ios", token_hash="text-phone")
    db_session.add(device)
    await db_session.flush()
    calls = scripted_phone_model(monkeypatch, "create_timer", {"duration_seconds": 60})
    args = dict(device=device, text="Set a timer for one minute", instance_id="text-tab",
                origin="https://home.example.ts.net", request_id="same-request")
    result = await run_phone_text(db_session, **args)
    assert result["ok"] is True
    action = result["phone_action"]
    assert action["operation"] == "create_timer"
    assert action["executed"] is False
    stored = get_action(action["action_id"])
    assert stored["device_id"] == str(device.id)
    assert stored["session_id"] == ""
    assert stored["phone_text_binding"]
    lease = await current_lease(db_session)
    assert lease.session_id is None
    lease_id = lease.lease_id
    await db_session.commit()
    db_session.expire_all()
    await db_session.refresh(device)
    replay = await run_phone_text(db_session, **args)
    assert replay["replayed"] is True
    assert replay["phone_action"]["action_id"] == action["action_id"]
    assert replay["phone_action"]["recovered"] is True
    assert (await current_lease(db_session)).lease_id == lease_id
    assert len(calls) == 2
    text_kernel.assert_not_awaited()


async def test_text_does_not_rotate_active_talk_lease(text_kernel, monkeypatch, db_session):
    from app.device_gateway.lease import _when
    from app.utils.text import utcnow

    device = Device(name="Talking phone", platform="ios", token_hash="talking-phone")
    db_session.add(device)
    await db_session.flush()
    lease = await claim_lease(db_session, device_id=device.id, instance_id="tab", session_id="real-talk", client_generation=4)
    identity = lease.lease_id
    lease.expires_at = utcnow() + timedelta(seconds=1)
    await db_session.flush()
    scripted_phone_model(monkeypatch, "create_timer", {"duration_seconds": 60})
    result = await run_phone_text(db_session, device=device, text="Set a timer", instance_id="tab",
                                  origin="https://home.example.ts.net", request_id="text-while-talking")
    assert result["phone_action"]["operation"] == "create_timer"
    assert lease.lease_id == identity
    assert lease.session_id == "real-talk"
    assert lease.client_generation == 4
    assert _when(lease.expires_at) > utcnow() + timedelta(seconds=10)


async def test_lease_takeover_during_text_inference_blocks_action(text_kernel, monkeypatch, db_session):
    device = Device(name="First phone", platform="ios", token_hash="first-text")
    other = Device(name="Other phone", platform="ios", token_hash="other-text")
    db_session.add_all([device, other])
    await db_session.flush()

    async def takeover():
        await claim_lease(db_session, device_id=other.id, instance_id="other-tab")
        await db_session.flush()

    calls = scripted_phone_model(monkeypatch, "create_timer", {"duration_seconds": 60}, takeover)
    result = await run_phone_text(db_session, device=device, text="Set a timer", instance_id="tab",
                                  origin="https://home.example.ts.net", request_id="takeover-turn")
    assert "phone_action" not in result
    assert result["ok"] is False
    assert result["error_code"] == "PHONE_CONTEXT_CHANGED"
    assert result["reply"] == "The conversation moved while I was thinking. Please ask again."
    assert "PHONE_CONTEXT_CHANGED" in str([m.content for m in calls[-1] if m.role == "tool"])
    text_kernel.assert_not_awaited()


async def test_text_draft_confirmation_without_a_live_session(text_kernel, monkeypatch, db_session):
    from app.device_gateway.durable_actions import load_action

    device = Device(name="Draft phone", platform="ios", token_hash="draft-phone")
    db_session.add(device)
    await db_session.flush()
    scripted_phone_model(monkeypatch, "message_contact", {"phone_number": "+15555550123", "message": "Hello"})
    context = dict(device=device, instance_id="draft-tab", origin="https://home.example.ts.net")
    draft = await run_phone_text(db_session, **context, text="Draft Hello, don't send it yet", request_id="draft-1")
    action_id = draft["phone_action"]["action_id"]
    assert get_action(action_id)["state"] == "draft"
    confirmed = await run_phone_text(db_session, **context, text="yes", request_id="confirm-1")
    assert confirmed["phone_action"]["action_id"] == action_id
    assert confirmed["phone_action"]["executed"] is False
    assert get_action(action_id)["state"] == "authorized"
    assert (await load_action(db_session, action_id))["state"] == "authorized"
    assert (await current_lease(db_session)).session_id is None


async def test_text_endpoint_uses_kernel_and_request_id_precedence(text_kernel, monkeypatch, db_session):
    from starlette.requests import Request

    from app.device_gateway.api import TextRequest, user_text

    device = Device(name="Endpoint phone", platform="ios", token_hash="endpoint-phone")
    db_session.add(device)
    await db_session.flush()
    calls = scripted_phone_model(monkeypatch, "create_timer", {"duration_seconds": 60})
    request = Request({"type": "http", "scheme": "https", "method": "POST", "path": "/text",
                       "headers": [(b"host", b"home.example.ts.net"), (b"origin", b"https://home.example.ts.net")]})
    first = await user_text(TextRequest(text="Set a timer", request_id="preferred", idempotency_key="ignored-1", instance_id="tab"),
                            request, device, db_session)
    replay = await user_text(TextRequest(text="Set a timer", request_id="preferred", idempotency_key="ignored-2", instance_id="tab"),
                             request, device, db_session)
    assert first["phone_action"]["action_id"] == replay["phone_action"]["action_id"]
    assert replay["replayed"] is True
    assert len(calls) == 2
    assert first["reply"]


@pytest.mark.parametrize("utterance", ["cancel that", "never mind", "no", "stop"])
async def test_text_cancellation_updates_pending_card_without_model_or_global_goal(text_kernel, monkeypatch, db_session, utterance):
    from app.cognitive.session_store import current
    from app.device_gateway.durable_actions import load_action

    device = Device(name="Cancel phone", platform="ios", token_hash="cancel-phone")
    db_session.add(device)
    await db_session.flush()
    calls = scripted_phone_model(monkeypatch, "message_contact", {"phone_number": "+15555550123", "message": "Hello"})
    context = dict(device=device, instance_id="cancel-tab", origin="https://home.example.ts.net")
    draft = await run_phone_text(db_session, **context, text="Draft Hello, don't send it yet", request_id="draft-cancel")
    action_id = draft["phone_action"]["action_id"]
    cognition = current()
    cognition.focused_goal_id = "unrelated-global-goal"
    result = await run_phone_text(db_session, **context, text=utterance, request_id="cancel-request")
    assert result["reply"] == "Cancelled."
    assert result["phone_action"]["action_id"] == action_id
    assert result["phone_action"]["card"]["status"] == "cancelled"
    assert get_action(action_id)["state"] == "cancelled"
    assert (await load_action(db_session, action_id))["state"] == "cancelled"
    assert cognition.focused_goal_id == "unrelated-global-goal"
    assert len(calls) == 2
    text_kernel.assert_not_awaited()


@pytest.mark.parametrize("change", ["tab", "lease", "origin", "revision"])
async def test_text_replay_withholds_action_after_context_changes(text_kernel, monkeypatch, db_session, change):
    device = Device(name="Replay phone", platform="ios", token_hash="replay-phone")
    db_session.add(device)
    await db_session.flush()
    calls = scripted_phone_model(monkeypatch, "create_timer", {"duration_seconds": 60})
    args = dict(device=device, text="Set a timer", instance_id="tab", origin="https://home.example.ts.net", request_id="replay-key")
    first = await run_phone_text(db_session, **args)
    assert first["phone_action"]
    if change == "tab":
        args["instance_id"] = "replacement-tab"
    elif change == "lease":
        await claim_lease(db_session, device_id=device.id, instance_id="tab")
        await db_session.flush()
    elif change == "origin":
        args["origin"] = "https://replacement.example.ts.net"
    else:
        device.auth_revision += 1
        await db_session.flush()
    before = (await current_lease(db_session)).lease_id
    result = await run_phone_text(db_session, **args)
    assert result["replayed"] is True
    assert result["actions_withheld"] == "PHONE_CONTEXT_CHANGED"
    assert "phone_action" not in result and "phone_actions" not in result
    assert (await current_lease(db_session)).lease_id == before
    assert len(calls) == 2


@pytest.mark.parametrize("outcome", ["cancelled", "executed", "expired"])
async def test_recovered_card_uses_terminal_durable_state_after_cache_loss(text_kernel, monkeypatch, db_session, outcome):
    import time

    from starlette.requests import Request

    from app.device_gateway.durable_actions import load_action, upsert_action
    from app.device_gateway.mobile_actions.routes import (
        CompleteBody,
        mobile_actions_cancel,
        mobile_actions_client_complete,
    )
    from app.device_gateway.mobile_actions.store import update_action

    device = Device(name="Recovery phone", platform="ios", token_hash="terminal-phone")
    db_session.add(device)
    await db_session.flush()
    calls = scripted_phone_model(monkeypatch, "create_timer", {"duration_seconds": 60})
    args = dict(device=device, text="Set a timer", instance_id="tab", origin="https://home.example.ts.net", request_id="terminal-key")
    first = await run_phone_text(db_session, **args)
    action_id = first["phone_action"]["action_id"]
    await db_session.commit()
    request = Request({"type": "http", "headers": [(b"host", b"home.example.ts.net")]})
    if outcome == "cancelled":
        result = await mobile_actions_cancel(action_id, request, device)
        assert result["ok"] is True
    elif outcome == "executed":
        result = await mobile_actions_client_complete(action_id, CompleteBody(status="executed", result="CREATED"), request, device)
        assert result["ok"] is True
    else:
        updated = update_action(action_id, exp=time.time() - 1)
        await upsert_action(db_session, updated)
        await db_session.commit()
    durable = await load_action(db_session, action_id)
    if outcome != "expired":
        assert durable["state"] == outcome
    reset_for_tests()
    recovered = await run_phone_text(db_session, **args)
    card = recovered["phone_action"]
    assert card["card"]["status"] == outcome
    assert card["native_execute"] is False
    assert card["recovered"] is True
    for key in ("launch_url", "open_url", "copy_text", "share_text", "duration_seconds"):
        assert key not in card and key not in card["card"]
    assert len(calls) == 2
