"""Executor Plan §2.1–2.2: shared lane errors + delivery-evidence gate."""

import pytest

from app.integrations.lane_errors import (
    LANE_ERROR_SPOKEN,
    assert_delivery_evidence,
    lane_error_message,
    normalize_lane_code,
)
from app.integrations.life_helper import LifeHelperError


def test_every_typed_code_renders_spoken_plus_next_step() -> None:
    for code, (spoken, next_step) in LANE_ERROR_SPOKEN.items():
        message = lane_error_message("slack", {"diagnosis": code}, chat="general")
        assert "{lane}" not in message and "{detail}" not in message
        assert len(message) > len(next_step)
        assert next_step in message
        assert spoken.split(".")[0].format(lane="Slack", detail="general") in message


def test_backing_aliases_normalize_onto_typed_codes() -> None:
    assert normalize_lane_code({"diagnosis": "cdp_qr"}) == "session_logged_out"
    assert normalize_lane_code({"error": "cdp_offline"}) == "not_connected"
    assert normalize_lane_code({"reason": "ui_changed"}) == "ax_unwalkable"
    assert normalize_lane_code({"qr": True}) == "session_logged_out"
    assert normalize_lane_code({"diagnosis": "something_new"}) is None
    assert normalize_lane_code(None) is None


def test_helper_markers_map_to_account_and_relay_codes() -> None:
    imessage = lane_error_message(
        "imessage",
        {"reason": "messages.send failed: EVIE_NO_IMESSAGE_SERVICE"},
        verb="send",
    )
    assert "isn't signed in" in imessage
    assert "Nothing went out." in imessage
    relay = lane_error_message(
        "imessage",
        {"reason": "messages.send failed: EVIE_NO_SMS_SERVICE"},
        verb="send",
    )
    assert "Text Message Forwarding" in relay
    both = lane_error_message("imessage", {"reason": "EVIE_NO_MESSAGE_SERVICE"}, verb="send")
    assert "isn't signed in" in both


def test_unknown_signal_stays_honest_without_fake_fix() -> None:
    message = lane_error_message("telegram", {"error": "weird_717"})
    assert "Telegram" in message
    assert "weird_717" in message
    assert "nothing was assumed" in message.lower()


def test_send_verb_names_the_unsent_act() -> None:
    message = lane_error_message(
        "imessage", {"diagnosis": "session_logged_out"}, verb="send", chat="Ada"
    )
    assert "Nothing went out." in message
    read = lane_error_message("imessage", {"diagnosis": "session_logged_out"}, chat="Ada")
    assert "Nothing went out." not in read


def test_whatsapp_wordings_stay_byte_identical() -> None:
    from app.integrations import adapters

    assert adapters._whatsapp_read_error_message({"qr": True}) == (
        "WhatsApp Web is not linked (showing the login screen). On the iPhone: "
        "WhatsApp → Settings → Linked Devices → Link a Device, then scan. "
        "Or open WhatsApp Desktop on the Mac so Evie can read its local cache."
    )
    assert "sync" in adapters._whatsapp_read_error_message(
        {"diagnosis": "desktop_cache_unavailable"}
    ).lower()
    assert adapters._whatsapp_read_error_message({"error": "cdp_offline"}) == (
        "The Mac's background browser for WhatsApp isn't running, so Evie "
        "can't reach WhatsApp Web right now."
    )
    assert adapters._whatsapp_read_error_message(
        {"error": "mystery"}, chat="Ada"
    ) == "WhatsApp background connection: mystery"


def test_delivery_gate_accepts_helper_cdp_and_api_shapes() -> None:
    helper = assert_delivery_evidence(
        "imessage", "send",
        {"ok": True, "data": {"sent": True, "to": "Ada"},
         "delivery": {"confirmed": True, "confirmed_by": "sent"}},
        provider="EVLifeHelper",
    )
    assert helper == {
        "confirmed": True,
        "evidence": {"provider": "EVLifeHelper", "confirmed_by": "sent", "to": "Ada"},
    }
    cdp = assert_delivery_evidence(
        "whatsapp", "send",
        {"ok": True, "sent": True, "message_id": "ABC123", "chat": "Ada"},
        provider="whatsapp_cdp",
    )
    assert cdp["confirmed"] is True
    assert cdp["evidence"]["message_id"] == "ABC123"
    api = assert_delivery_evidence(
        "slack", "post", {"ok": True, "ts": "1728000000.001"}, provider="slack_api"
    )
    assert api["evidence"]["confirmed_by"] == "ts"


def test_delivery_gate_refuses_dry_run_compose_and_ok_false() -> None:
    with pytest.raises(LifeHelperError) as dry:
        assert_delivery_evidence("imessage", "send", {"ok": True, "dry_run": True, "data": {}})
    assert dry.value.error_code == "missing_delivery_evidence"
    with pytest.raises(LifeHelperError) as compose:
        assert_delivery_evidence("mail", "send", {"ok": True, "opened_compose": True})
    assert compose.value.error_code == "missing_delivery_evidence"
    with pytest.raises(LifeHelperError) as failed:
        assert_delivery_evidence("gmail", "send", {"ok": False, "message_id": "x"})
    assert failed.value.error_code == "missing_delivery_evidence"
    with pytest.raises(LifeHelperError):
        assert_delivery_evidence("signal", "send", {})
