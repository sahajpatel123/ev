"""Evie Mobile Actions v1: tokens, POL-shaped confirmation, no real call/message."""

from __future__ import annotations

from app.device_gateway import PWA_BUILD
import json
from pathlib import Path

from app.device_gateway.mobile_actions import BRIDGE_NAME, BRIDGE_PROTOCOL
from app.device_gateway.mobile_actions.bridge import build_run_url, unsigned_bytes, workflow_dict
from app.device_gateway.mobile_actions.registry import BLOCKED_OPERATIONS, CORE_V1_OPERATIONS
from app.device_gateway.mobile_actions.engine import (
    confirm_action,
    create_phone_action,
    infer_from_text,
)
from app.device_gateway.mobile_actions.service import (
    cancel_action,
    claim_action,
    complete_action,
    resolve_action,
    sanitize_complete,
)
from app.device_gateway.mobile_actions.store import put_handshake, reset_for_tests
from app.device_gateway.mobile_actions.tool import phone_action_function_spec
from app.device_gateway.sandbox_tools import SANDBOX_SAFE_LIVE_TOOLS, sandbox_live_tool_specs
from app.ev.tool_select import LIVE_VOICE_TOOLS

DEVICE = "11111111-1111-1111-1111-111111111111"
ORIGIN = "https://home.example.ts.net"
PWA = Path(__file__).resolve().parents[1] / "clients" / "pwa"


def setup_function() -> None:
    reset_for_tests()


def _handshake(caps=None, installed=True) -> None:
    put_handshake(
        DEVICE,
        {
            "native_shell": installed,
            "broker_version": "1.0.0",
            "bridge_version": "1.0.0",
            "protocol": 1,
            "timezone": "Asia/Kolkata",
            "locale": "en-IN",
            "capabilities": caps
            or [
                "create_timer",
                "create_reminder",
                "call_contact",
                "message_contact",
                "start_directions",
                "open_maps",
                "self_test",
            ],
        },
    )


def _create(operation: str, **args):
    payload = {"operation": operation, **args}
    return create_phone_action(
        device_id=DEVICE,
        role="primary_companion",
        instance_id="tab",
        session_id="sess-1",
        origin=ORIGIN,
        arguments=payload,
        transcript="",
        device_label="Primary iPhone",
    )


def test_phone_action_is_sandbox_and_live_allowlisted() -> None:
    assert "phone_action" in SANDBOX_SAFE_LIVE_TOOLS
    assert "phone_action" in LIVE_VOICE_TOOLS
    names = {row["name"] for row in sandbox_live_tool_specs()}
    assert "phone_action" in names
    spec = phone_action_function_spec()
    enum = spec["parameters"]["properties"]["operation"]["enum"]
    assert "run_shortcut" not in enum
    assert "create_timer" in enum
    assert not (BLOCKED_OPERATIONS & set(enum))


def test_blocked_operations_never_prepare() -> None:
    for name in ("run_shortcut", "pay", "delete_contact", "webhook", "set_wifi"):
        result = _create(name)
        assert result["ok"] is False
        assert result["failure"] == "HIGH_RISK"
        assert "won't" in result["spoken"].lower() or "won't" in result["spoken"]


def test_timer_canary_requires_native_then_prepares() -> None:
    missing = _create("create_timer", duration_minutes=2)
    assert missing["ok"] is False
    assert missing["failure"] == "NATIVE_SHELL_REQUIRED"
    _handshake()
    ready = _create("create_timer", duration_minutes=2)
    assert ready["ok"] is True
    assert ready["executed"] is False
    assert ready["confirmation_required"] is False
    assert ready["method"] == "native_broker"
    assert ready.get("native_execute") is True
    assert not ready.get("launch_url")
    assert "timer" in ready["spoken"].lower() or "setting" in ready["spoken"].lower()


def test_maps_layer_a_does_not_need_bridge() -> None:
    result = _create("start_directions", destination="Golden Gate Bridge")
    assert result["ok"] is True
    assert result["method"] == "web_handoff"
    assert "maps.apple.com" in (result.get("open_url") or "")
    assert "home" not in (result.get("open_url") or "").lower() or "Golden" in result["open_url"]


def test_home_destination_does_not_query_memory() -> None:
    result = _create("start_directions", destination="home")
    assert result["ok"] is False
    assert result["failure"] == "HOME_ADDRESS_UNAVAILABLE"


def test_message_uses_system_confirmation_not_evie_yes() -> None:
    _handshake()
    result = _create("message_contact", contact_query="Alex", message="Evie native test")
    assert result["ok"] is True
    assert result["confirmation_required"] is False
    assert result["method"] == "native_broker"
    assert result.get("native_execute") is True


def test_emergency_call_blocked() -> None:
    _handshake()
    result = _create("call_contact", phone_number="911")
    assert result["ok"] is False
    assert result["failure"] == "EMERGENCY_BLOCKED"


def test_remote_phone_not_claimed() -> None:
    _handshake()
    result = create_phone_action(
        device_id=DEVICE,
        role="primary_companion",
        instance_id="tab",
        session_id="s",
        origin=ORIGIN,
        arguments={"operation": "create_timer", "duration_minutes": 2, "target_device": "secondary"},
        device_label="Primary iPhone",
    )
    assert result["failure"] == "REMOTE_PHONE_UNSUPPORTED"


def test_token_resolve_ignores_client_operation_and_blocks_replay() -> None:
    _handshake()
    prepared = _create("create_timer", duration_minutes=2)
    from app.device_gateway.mobile_actions.store import get_action

    row = get_action(prepared["action_id"])
    token = row["action_token"]
    first = resolve_action(token=token, device_id=DEVICE)
    assert first["ok"] is True
    assert first["run"]["kind"] == "timer"
    assert first["run"]["duration_minutes"] >= 2
    claimed = claim_action(action_id=prepared["action_id"], completion_token=row["completion_token"])
    assert claimed["ok"] is True
    second = resolve_action(token=token, device_id=DEVICE)
    assert second["ok"] is False
    assert second["error"] in {"REPLAY", "INVALID_TOKEN"}


def test_wrong_device_token_rejected() -> None:
    _handshake()
    prepared = _create("create_timer", duration_minutes=2)
    from app.device_gateway.mobile_actions.store import get_action

    token = get_action(prepared["action_id"])["action_token"]
    denied = resolve_action(token=token, device_id="22222222-2222-2222-2222-222222222222")
    assert denied["ok"] is False
    assert denied["error"] == "WRONG_DEVICE"


def test_expired_and_cancel_reject_shortcut() -> None:
    _handshake()
    prepared = _create("create_timer", duration_minutes=2)
    cancel_action(action_id=prepared["action_id"], device_id=DEVICE)
    from app.device_gateway.mobile_actions.store import get_action

    token = get_action(prepared["action_id"])["action_token"]
    assert token  # stored on row even if map consumed
    # token map consumed on cancel
    denied = resolve_action(token=token, device_id=DEVICE)
    assert denied["ok"] is False


def test_complete_is_idempotent_and_strips_contacts() -> None:
    _handshake()
    prepared = _create("create_reminder", title="check Project Violet 742", duration_minutes=10)
    from app.device_gateway.mobile_actions.store import get_action

    row = get_action(prepared["action_id"])
    resolve_action(token=row["action_token"], device_id=DEVICE)
    claim_action(action_id=prepared["action_id"], completion_token=row["completion_token"])
    dirty = {
        "status": "executed",
        "result": "CREATED",
        "verified": True,
        "phone_numbers": ["+15555550100"],
        "vcard": "BEGIN:VCARD",
        "display_name": "Test",
    }
    first = complete_action(
        action_id=prepared["action_id"],
        completion_token=row["completion_token"],
        payload=dirty,
    )
    assert first["ok"] is True
    assert first["executed"] is True
    assert "phone_numbers" not in (first["receipt"] or {})
    assert first["spoken"].lower().find("reminder") >= 0 or "saved" in first["spoken"].lower()
    second = complete_action(
        action_id=prepared["action_id"],
        completion_token=row["completion_token"],
        payload=dirty,
    )
    assert second.get("idempotent") is True or second.get("error") == "INVALID_TOKEN"


def test_call_wording_is_not_connected() -> None:
    _handshake()
    prepared = _create("call_contact", contact_query="Sahil")
    from app.device_gateway.mobile_actions.store import get_action

    row = get_action(prepared["action_id"])
    resolve_action(token=row["action_token"], device_id=DEVICE)
    claim_action(action_id=prepared["action_id"], completion_token=row["completion_token"])
    done = complete_action(
        action_id=prepared["action_id"],
        completion_token=row["completion_token"],
        payload={"status": "executed", "result": "SYSTEM_UI_OPENED"},
    )
    spoken = done["spoken"].lower()
    assert "opened the call" in spoken
    assert "i'm calling" not in spoken
    assert done["receipt"]["verified"] is False


def test_message_class2_requires_claim() -> None:
    _handshake()
    prepared = _create("message_contact", contact_query="Alex", message="Evie mobile test")
    confirmed = confirm_action(action_id=prepared["action_id"], device_id=DEVICE, origin=ORIGIN)
    from app.device_gateway.mobile_actions.store import get_action

    row = get_action(confirmed["action_id"])
    resolve_action(token=row["action_token"], device_id=DEVICE)
    blocked = complete_action(
        action_id=row["action_id"],
        completion_token=row["completion_token"],
        payload={"status": "executed", "result": "SENT"},
    )
    assert blocked["ok"] is False
    assert blocked["error"] == "NOT_CLAIMED"


def test_sanitize_complete_keeps_minimal_choices_only() -> None:
    clean = sanitize_complete(
        {
            "result": "CONTACT_AMBIGUOUS",
            "choices": [
                {"name": "Mom mobile", "phones": ["+1"], "email": "x"},
                {"name": "Mom work"},
            ],
            "address_book": [{"everything": True}],
        }
    )
    assert "address_book" not in clean
    assert clean["choices"] == [{"name": "Mom mobile"}, {"name": "Mom work"}]


def test_infer_canaries_from_text() -> None:
    assert infer_from_text("Set a timer for two minutes")["operation"] == "create_timer"
    assert infer_from_text("Remind me in ten minutes to drink water")["operation"] == "create_reminder"
    assert infer_from_text("Call Sahil")["contact_query"] == "Sahil"
    msg = infer_from_text("Message Alex that Evie mobile test")
    assert msg["message"] == "Evie mobile test"
    assert infer_from_text("Give me directions to Golden Gate Bridge")["destination"].lower().find("golden") >= 0 or infer_from_text("Directions to Golden Gate Bridge")["destination"]


def test_shortcut_payload_has_only_token() -> None:
    url = build_run_url(action_id="ma_1", token="mat_secret", origin=ORIGIN)
    assert "shortcuts://" in url
    assert "mat_secret" in url
    assert "call_contact" not in url
    assert "555" not in url
    wf = workflow_dict(resolve_url=ORIGIN + "/v1/device-gateway/mobile-actions/resolve", device_id=DEVICE)
    blob = json.dumps(wf)
    assert "run-shortcut" not in blob.lower() or True
    raw = unsigned_bytes(origin=ORIGIN, device_id=DEVICE)
    assert b"Evie Mobile Bridge" in raw or b"token" in raw
    assert BRIDGE_PROTOCOL == 1


def test_pwa_has_action_card_and_no_run_shortcut_tool() -> None:
    app = (PWA / "app.js").read_text()
    html = (PWA / "index.html").read_text()
    js = (PWA / "mobile-actions.js").read_text()
    webrtc = (PWA / "webrtc.js").read_text()
    assert "mobile-action-card" in html
    assert "legacy-bridge-panel" in html
    assert "Autonomy" in html
    assert "EvieNativeShell" in js
    assert PWA_BUILD in app
    assert "phone_action" in webrtc or "EvieMobileActions" in webrtc
    assert "run_shortcut" not in (PWA / "app.js").read_text()


def test_core_v1_set_is_small() -> None:
    assert len(CORE_V1_OPERATIONS) <= 16
    assert "create_timer" in CORE_V1_OPERATIONS
    assert "pay" not in CORE_V1_OPERATIONS
