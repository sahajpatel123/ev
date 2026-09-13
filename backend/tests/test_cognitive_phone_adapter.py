"""Phone adapter boundaries, without a provider or physical device."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.device_gateway import cognitive_phone
from app.utils.text import utcnow


async def test_discovery_excludes_action_credentials(monkeypatch):
    device = SimpleNamespace(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        role="primary_companion", name="Owner phone", revoked_at=None,
        memory_scope="owner",
    )
    lease = SimpleNamespace(
        device_id=device.id, session_id="live", instance_id="tab",
        expires_at=utcnow() + timedelta(minutes=1),
    )
    live = SimpleNamespace(instance_id="tab", gateway_origin="https://home.example.ts.net", _closed=False)
    db = SimpleNamespace(get=AsyncMock(return_value=device), refresh=AsyncMock())
    monkeypatch.setattr(cognitive_phone, "assert_live_authority", AsyncMock(return_value=(live, lease)))
    from app.device_gateway.mobile_actions import service

    monkeypatch.setattr(service, "status_snapshot", lambda **kwargs: {
        "capabilities": [{"operation": "create_timer"}],
        "last_action": {"launch_url": "https://private.example/secret", "token": "secret"},
    })
    result = await cognitive_phone.execute_phone_tool(
        db, "capability.discover", {}, device_id=str(device.id),
        live_session_id="live", transcript="What can you do?",
    )
    assert result == {"ok": True, "capabilities": [{"operation": "create_timer"}]}


async def test_unknown_device_cannot_dispatch(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(cognitive_phone, "dispatch_phone_action", dispatch)
    db = SimpleNamespace(get=AsyncMock(return_value=None))
    result = await cognitive_phone.execute_phone_tool(
        db, "phone_action", {"operation": "create_timer"},
        device_id="11111111-1111-1111-1111-111111111111",
        live_session_id="live", transcript="Set a timer",
    )
    assert result["error"] == "PHONE_NOT_TRUSTED"
    assert result["executed"] is False
    dispatch.assert_not_awaited()


async def test_model_cannot_confirm_an_action(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(cognitive_phone, "dispatch_phone_action", dispatch)
    result = await cognitive_phone.execute_phone_tool(
        SimpleNamespace(), "phone_action",
        {"operation": "message_contact", "confirm_action_id": "pending-action"},
        device_id="11111111-1111-1111-1111-111111111111",
        live_session_id="live", transcript="What is pending?",
    )
    assert result["error"] == "OWNER_CONFIRMATION_REQUIRED"
    dispatch.assert_not_awaited()
    assert "confirm_action_id" not in cognitive_phone.phone_tool_specs()[0].parameters["properties"]


@pytest.mark.parametrize("role", ["primary_companion", "secondary_companion"])
@pytest.mark.parametrize("fail_after_action", [False, True])
async def test_kernel_phone_send_uses_phone_tools_not_mac(monkeypatch, tmp_path, db_session, role, fail_after_action):
    from app.cognitive import kernel
    from app.cognitive.session_store import reset_for_tests
    from app.config import settings
    from app.contracts import ChatResult, ToolCall
    from app.models import Device

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    reset_for_tests()
    device = Device(name="Test phone", token_hash="a" * 64, role=role, platform="ios", memory_scope="owner")
    db_session.add(device)
    await db_session.flush()
    phone_tool = AsyncMock(return_value={
        "ok": True, "executed": False, "spoken": "Ready for confirmation.",
        "launch_url": "https://private.example/secret", "card": {"token": "secret"},
    })
    monkeypatch.setattr(cognitive_phone, "execute_phone_tool", phone_tool)
    mac_tool = AsyncMock(side_effect=AssertionError("Phone must not execute a Mac tool"))
    monkeypatch.setattr(kernel, "execute_semantic", mac_tool)
    monkeypatch.setattr("app.cognitive.executor.execute_semantic", mac_tool)
    monkeypatch.setattr(kernel, "muse_spark_key_loaded", lambda: True)
    seen = []

    class Muse:
        async def chat_with_tools(self, messages, specs, **kwargs):
            seen.append((list(messages), [spec.name for spec in specs]))
            if len(seen) == 1:
                return ChatResult(text="", tool_calls=[ToolCall(
                    id="phone-1", name="phone_action",
                    arguments={"operation": "message_contact", "text": "Hello"},
                )])
            if fail_after_action:
                raise RuntimeError("Provider disconnected after action preparation")
            return ChatResult(text="Ready for confirmation.")

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: Muse())
    try:
        result = await kernel.handle_turn(
            transcript="Send a message to Alex saying Hello", device_id=str(device.id),
            live_session_id="phone-session", session=db_session,
        )
        assert result.kind == ("unavailable" if fail_after_action else "muse")
        assert result.evidence == [phone_tool.return_value]
        assert "secret" not in str(result.as_dict())
        phone_tool.assert_awaited_once()
        assert phone_tool.await_args.kwargs["device_id"] == str(device.id)
        mac_tool.assert_not_awaited()
        offered = set(seen[0][1])
        # The phone sees the full bus now, but the model asked for a phone-local
        # actuator — so it must still have gone to the phone adapter and never to
        # the Mac executor.
        assert {"phone_action", "phone.read", "capability.discover"} <= offered
        assert "home.act" in offered
        tool_messages = [m.content for m in seen[-1][0] if m.role == "tool"]
        assert tool_messages and "secret" not in str(tool_messages)
    finally:
        reset_for_tests()


async def test_kernel_receipt_uses_phone_kernel_and_caller_transaction(monkeypatch, db_session):
    from app.cognitive.kernel import KernelResult
    from app.device_gateway.turn_receipts import record_turn_receipt
    from app.models import Device

    device = Device(name="Phone", token_hash="receipt-phone", platform="ios")
    db_session.add(device)
    await db_session.flush()
    monkeypatch.setattr("app.cognitive.mode.muse_kernel_active", lambda: True)
    action = {"action_id": "action-1", "card": {"title": "Confirm timer"}, "executed": False}
    kernel = AsyncMock(return_value=KernelResult(spoken="Ready for confirmation.", evidence=[action]))
    pipeline = AsyncMock(side_effect=AssertionError("No Mac preroute"))
    monkeypatch.setattr("app.cognitive.kernel.handle_turn", kernel)
    monkeypatch.setattr("app.device_gateway.pipeline.run_trusted_device_turn", pipeline)
    receipt = await record_turn_receipt(
        db_session, device=device, idempotency_key="phone-kernel-receipt",
        transcript="Set a timer", session_id="live",
    )
    pipeline.assert_not_awaited()
    kernel.assert_awaited_once()
    assert kernel.await_args.kwargs["session"] is db_session
    assert kernel.await_args.kwargs["device_id"] == str(device.id)
    assert receipt["core_reply"] == "Ready for confirmation."
    assert receipt["phone_action"] == action
    assert receipt["phone_actions"] == [action]
    await db_session.commit()
    db_session.expire_all()
    await db_session.refresh(device)
    replay = await record_turn_receipt(
        db_session, device=device, idempotency_key="phone-kernel-receipt",
        transcript="Set a timer", session_id="live",
    )
    assert replay["phone_action"] == {**action, "recovered": True}
    assert replay["replayed"] is True
    kernel.assert_awaited_once()


async def test_receipt_key_cannot_replay_another_phones_reply(db_session):
    from app.device_gateway.turn_receipts import record_turn_receipt
    from app.models import Device

    first = Device(name="Primary", token_hash="primary-receipt", platform="ios")
    second = Device(name="Secondary", token_hash="secondary-receipt", platform="ios")
    db_session.add_all([first, second])
    await db_session.flush()
    args = {"idempotency_key": "same-receipt-key", "transcript": "", "kind": "observation"}
    original = await record_turn_receipt(db_session, device=first, **args)
    replay = await record_turn_receipt(db_session, device=first, **args)
    collision = await record_turn_receipt(db_session, device=second, **args)
    assert replay["receipt_id"] == original["receipt_id"]
    assert replay["replayed"] is True
    assert collision == {"ok": False, "error_code": "IDEMPOTENCY_KEY_CONFLICT", "authority": False}
    first.revoked_at = utcnow()
    revoked = await record_turn_receipt(db_session, device=first, **args)
    assert revoked == {"ok": False, "error_code": "DEVICE_TRUST_CHANGED", "authority": False}
    first.revoked_at = None
    first.memory_scope = "sandbox"
    demoted = await record_turn_receipt(db_session, device=first, **args)
    assert demoted == {"ok": False, "error_code": "DEVICE_TRUST_CHANGED", "authority": False}


async def test_client_evidence_cannot_claim_a_server_action(db_session):
    from app.device_gateway.turn_receipts import record_turn_receipt
    from app.models import Device

    device = Device(name="Sandbox", token_hash="sandbox-evidence", memory_scope="sandbox")
    db_session.add(device)
    await db_session.flush()
    result = await record_turn_receipt(
        db_session, device=device, idempotency_key="forged-evidence-receipt", transcript="",
        evidence={"core_takeover": True, "core_reply": "Sent!", "action_tool": "send",
                  "action_executed": True, "action_verified": True,
                  "phone_action": {"launch_url": "https://untrusted.example"}},
    )
    assert result["trusted_owner"] is False
    for key in ("core_takeover", "core_reply", "phone_action", "action_tool", "action_executed", "action_verified"):
        assert key not in result


@pytest.mark.parametrize("change", ["none", "lease", "generation", "revision", "instance", "origin", "live"])
async def test_action_cannot_adopt_replacement_authority(monkeypatch, change):
    device = SimpleNamespace(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        role="primary_companion", name="Phone", revoked_at=None,
        memory_scope="owner", auth_revision=1,
    )
    lease = SimpleNamespace(
        device_id=device.id, session_id="live", instance_id="tab", lease_id="lease-1",
        client_generation=1, expires_at=utcnow() + timedelta(minutes=1),
    )
    live = SimpleNamespace(
        instance_id="tab", gateway_origin="https://home.example.ts.net",
        _closed=False, auth_revision=1, client_generation=1,
        device_id=str(device.id), session_id="live", lease_id="lease-1",
    )
    db = SimpleNamespace(get=AsyncMock(return_value=device), refresh=AsyncMock())
    authority = AsyncMock(return_value=(live, lease))
    monkeypatch.setattr(cognitive_phone, "assert_live_authority", authority)
    monkeypatch.setattr(cognitive_phone, "live_for_session", lambda _: authority.return_value[0])
    dispatch = AsyncMock(return_value={"ok": True, "executed": False})
    monkeypatch.setattr(cognitive_phone, "dispatch_phone_action", dispatch)
    binding = await cognitive_phone.capture_phone_binding(db, device_id=str(device.id), live_session_id="live")
    assert binding is not None
    if change == "lease":
        lease.lease_id = live.lease_id = "lease-2"
    elif change == "generation":
        lease.client_generation = live.client_generation = 2
    elif change == "revision":
        device.auth_revision = live.auth_revision = 2
    elif change == "instance":
        lease.instance_id = live.instance_id = "replacement-tab"
    elif change == "origin":
        live.gateway_origin = "https://replacement.example.ts.net"
    elif change == "live":
        authority.return_value = (SimpleNamespace(**vars(live)), lease)
    result = await cognitive_phone.execute_phone_tool(
        db, "phone_action", {"operation": "create_timer", "duration_seconds": 60},
        device_id=str(device.id), live_session_id="live", transcript="Set a timer",
        expected_binding=binding,
    )
    if change == "none":
        dispatch.assert_awaited_once()
        assert result["ok"] is True
        # Home Station fallback is ON: an iPhone with no native broker must be
        # able to have the same request carried out by Home Station, labelled as
        # such, instead of hitting a dead end. The "never SILENTLY become a Mac
        # effect" law is enforced by the result evidence, not by blocking the
        # hop.
        assert dispatch.await_args.kwargs["allow_home_station_fallback"] is True
    else:
        dispatch.assert_not_awaited()
        assert result["error"] == "PHONE_CONTEXT_CHANGED"
        assert result["executed"] is False


@pytest.mark.parametrize("scenario", ["valid", "heartbeat", "generation_mismatch", "missing_revision", "replacement_during_refresh"])
async def test_phone_binding_with_real_registry_and_lease(monkeypatch, db_session, scenario):
    from app.device_gateway.lease import claim_lease, heartbeat_lease
    from app.device_gateway.webrtc_live import attach_phone_control_live
    from app.models import Device
    from app.voice.live.layer import register_live, unregister_live
    from app.voice.live.session import LiveSession

    device = Device(name="Bound phone", token_hash="bound-phone", platform="ios", device_type="phone")
    db_session.add(device)
    await db_session.flush()
    lease = await claim_lease(
        db_session, device_id=device.id, instance_id="bound-tab",
        session_id="bound-session", client_generation=1,
    )
    await db_session.flush()
    live = attach_phone_control_live(
        device=device, session_id="bound-session", actor="device:Bound phone",
        instance_id="bound-tab", gateway_origin="https://home.example.ts.net",
    )
    live.lease_id = lease.lease_id
    live.client_generation = 1
    replacement = None
    dispatch = AsyncMock(return_value={"ok": True, "executed": False})
    monkeypatch.setattr(cognitive_phone, "dispatch_phone_action", dispatch)
    try:
        if scenario == "generation_mismatch":
            live.client_generation = 2
        elif scenario == "missing_revision":
            del live.auth_revision
        binding = await cognitive_phone.capture_phone_binding(
            db_session, device_id=str(device.id), live_session_id="bound-session",
        )
        if scenario in {"generation_mismatch", "missing_revision"}:
            assert binding is None
            return
        assert binding is not None
        if scenario == "heartbeat":
            await heartbeat_lease(db_session, device_id=device.id, instance_id="bound-tab")
            await db_session.flush()
        elif scenario == "replacement_during_refresh":
            replacement = LiveSession(session_id="bound-session", device_id=str(device.id))
            refresh = db_session.refresh

            async def replace_after_refresh(*args, **kwargs):
                await refresh(*args, **kwargs)
                register_live(replacement)

            monkeypatch.setattr(db_session, "refresh", replace_after_refresh)
        result = await cognitive_phone.execute_phone_tool(
            db_session, "phone_action", {"operation": "create_timer", "duration_seconds": 60},
            device_id=str(device.id), live_session_id="bound-session", transcript="Set a timer",
            expected_binding=binding,
        )
        if scenario == "replacement_during_refresh":
            # Constructing the replacement can also close its predecessor;
            # either the closed-session guard or registry fence must reject it.
            assert result["error"] in {"PHONE_CONTEXT_CHANGED", "PHONE_LEASE_CHANGED"}
            dispatch.assert_not_awaited()
        else:
            assert result["ok"] is True
            dispatch.assert_awaited_once()
    finally:
        unregister_live(live)
        if replacement is not None:
            unregister_live(replacement)


@pytest.mark.parametrize("changed", ["none", "session", "instance", "origin"])
async def test_real_broker_confirmation_is_bound_to_original_phone_context(db_session, changed):
    from app.device_gateway.mobile_actions.engine import create_phone_action
    from app.device_gateway.mobile_actions.store import get_action, reset_for_tests
    from app.device_gateway.mobile_actions.tool import dispatch_phone_action
    from app.models import Device

    reset_for_tests()
    device = Device(name="Confirm phone", token_hash="confirm-phone", platform="ios")
    db_session.add(device)
    await db_session.flush()
    context = {
        "device_id": str(device.id), "role": "primary_companion",
        "instance_id": "original-tab", "session_id": "original-session",
        "origin": "https://home.example.ts.net",
    }
    try:
        prepared = create_phone_action(
            **context, arguments={"operation": "message_contact", "phone_number": "+15555550123", "message": "Hello"},
            transcript="Draft a message saying Hello, don't send it yet", device_label="Confirm phone",
        )
        action_id = prepared["action_id"]
        assert get_action(action_id)["state"] == "draft"
        if changed == "session":
            context["session_id"] = "replacement-session"
        elif changed == "instance":
            context["instance_id"] = "replacement-tab"
        elif changed == "origin":
            context["origin"] = "https://replacement.example.ts.net"
        result = await dispatch_phone_action(
            **context, arguments={"operation": "message_contact"}, transcript="yes",
            allow_home_station_fallback=False, require_confirmation_context=True,
            db_session=db_session,
        )
        row = get_action(action_id)
        assert row is not None
        if changed == "none":
            assert result["ok"] is True
            assert row["state"] == "authorized"
            assert result["executed"] is False
        else:
            assert result["error"] == "CONFIRMATION_CONTEXT_CHANGED"
            assert row["state"] == "draft"
    finally:
        reset_for_tests()
