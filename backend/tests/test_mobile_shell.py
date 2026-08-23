"""Evie Mobile Shell: trust engine, native broker path, one-confirmation law."""

from __future__ import annotations

from app.device_gateway.mobile_actions.apps import resolve_app
from app.device_gateway.mobile_actions.engine import (
    apply_confirmation_utterance,
    create_phone_action,
    infer_from_text,
    native_execute_action,
)
from app.device_gateway.mobile_actions.service import complete_action
from app.device_gateway.mobile_actions.store import get_action, put_handshake, reset_for_tests
from app.device_gateway.mobile_actions.trust import classify_utterance, freeze_hash, is_negated, wants_draft

DEVICE = "11111111-1111-1111-1111-111111111111"
ORIGIN = "https://home.example.ts.net"


def setup_function() -> None:
    reset_for_tests()
    put_handshake(
        DEVICE,
        {
            "native_shell": True,
            "broker_version": "1.0.0",
            "protocol": 1,
            "capabilities": [
                "create_timer",
                "create_reminder",
                "message_contact",
                "call_contact",
                "open_app",
                "direct_message",
            ],
        },
    )


def test_yes_no_classifier() -> None:
    for phrase in ("yes", "yeah", "yep", "do it", "send it", "go ahead", "sure", "correct"):
        assert classify_utterance(phrase) == "yes"
    for phrase in ("no", "don't", "cancel", "never mind", "stop"):
        assert classify_utterance(phrase) == "no"
    assert classify_utterance("what about dinner") == "unrelated"
    assert classify_utterance("who are you sending it to") == "query"


def test_negation_does_not_call() -> None:
    assert infer_from_text("Don't call Sahil") is None
    assert is_negated("Don't call Sahil")
    blocked = create_phone_action(
        device_id=DEVICE,
        role="primary_companion",
        instance_id="tab",
        session_id="s",
        origin=ORIGIN,
        arguments={"operation": "call_contact", "contact_query": "Sahil"},
        transcript="Don't call Sahil",
        device_label="Primary iPhone",
    )
    assert blocked["ok"] is False
    assert blocked["failure"] == "CANCELLED"


def test_dont_send_yet_is_draft() -> None:
    assert wants_draft("Message Sahil that I'm late, but don't send it yet")
    result = create_phone_action(
        device_id=DEVICE,
        role="primary_companion",
        instance_id="tab",
        session_id="s",
        origin=ORIGIN,
        arguments={"operation": "message_contact", "contact_query": "Sahil", "message": "I'm late"},
        transcript="Message Sahil that I'm late, but don't send it yet",
        device_label="Primary iPhone",
    )
    assert result["ok"] is True
    row = get_action(result["action_id"])
    assert row["state"] == "draft"
    native = native_execute_action(action_id=result["action_id"], device_id=DEVICE)
    assert native["ok"] is False
    assert native["error"] == "DRAFT"


def test_direct_message_voice_yes_executes_once() -> None:
    pending = create_phone_action(
        device_id=DEVICE,
        role="primary_companion",
        instance_id="tab",
        session_id="s",
        origin=ORIGIN,
        arguments={
            "operation": "direct_message",
            "contact_query": "Sahil",
            "message": "I'll be ten minutes late.",
        },
        device_label="Primary iPhone",
    )
    assert pending["confirmation_required"] is True
    assert "Sahil" in pending["spoken"]
    digest = freeze_hash(get_action(pending["action_id"])["normalized"])
    yes = apply_confirmation_utterance(device_id=DEVICE, origin=ORIGIN, text="Yes")
    assert yes and yes["ok"] is True
    assert yes["confirmation_required"] is False
    assert yes["native_execute"] is True
    run = native_execute_action(action_id=pending["action_id"], device_id=DEVICE)
    assert run["ok"] is True
    row = get_action(pending["action_id"])
    replay = native_execute_action(action_id=pending["action_id"], device_id=DEVICE)
    assert replay["ok"] is False
    done = complete_action(
        action_id=pending["action_id"],
        completion_token=row["completion_token"],
        payload={"status": "executed", "result": "SENT", "verified": True},
    )
    assert done["ok"] is True
    duplicate = complete_action(
        action_id=pending["action_id"],
        completion_token=row["completion_token"],
        payload={"status": "executed", "result": "SENT", "verified": True},
    )
    assert duplicate.get("idempotent") is True
    assert digest == freeze_hash(row["normalized"])


def test_voice_no_cancels() -> None:
    pending = create_phone_action(
        device_id=DEVICE,
        role="primary_companion",
        instance_id="tab",
        session_id="s",
        origin=ORIGIN,
        arguments={"operation": "direct_message", "contact_query": "Sahil", "message": "hi"},
        device_label="Primary iPhone",
    )
    no = apply_confirmation_utterance(device_id=DEVICE, origin=ORIGIN, text="No")
    assert no and no.get("failure") == "CANCELLED"
    row = get_action(pending["action_id"])
    assert row["state"] == "cancelled"


def test_mutate_requires_new_yes() -> None:
    create_phone_action(
        device_id=DEVICE,
        role="primary_companion",
        instance_id="tab",
        session_id="s",
        origin=ORIGIN,
        arguments={
            "operation": "direct_message",
            "contact_query": "Sahil",
            "message": "I'll be ten minutes late.",
        },
        device_label="Primary iPhone",
    )
    mutated = apply_confirmation_utterance(
        device_id=DEVICE, origin=ORIGIN, text="Actually say fifteen minutes."
    )
    assert mutated and mutated["confirmation_required"] is True
    assert "fifteen" in mutated["spoken"]


def test_expired_confirmation_rejected() -> None:
    pending = create_phone_action(
        device_id=DEVICE,
        role="primary_companion",
        instance_id="tab",
        session_id="s",
        origin=ORIGIN,
        arguments={"operation": "direct_message", "contact_query": "Sahil", "message": "hi"},
        device_label="Primary iPhone",
    )
    from app.device_gateway.mobile_actions import store
    from app.device_gateway.mobile_actions.engine import confirm_action

    store.update_action(pending["action_id"], exp=0)
    expired = confirm_action(action_id=pending["action_id"], device_id=DEVICE, origin=ORIGIN)
    assert expired.get("failure") == "EXPIRED"


def test_open_app_registry() -> None:
    assert resolve_app("Insta").display_name == "Instagram"
    assert resolve_app("Spotify").app_id == "spotify"
    assert resolve_app("not-a-real-app-xyz") is None
    result = create_phone_action(
        device_id=DEVICE,
        role="primary_companion",
        instance_id="tab",
        session_id="s",
        origin=ORIGIN,
        arguments={"operation": "open_app", "app_id": "Safari"},
        device_label="Primary iPhone",
    )
    assert result["ok"] is True
    assert result["confirmation_required"] is False
    assert "apple.com" in (result.get("open_url") or "")
    missing = create_phone_action(
        device_id=DEVICE,
        role="primary_companion",
        instance_id="tab",
        session_id="s",
        origin=ORIGIN,
        arguments={"operation": "open_app", "app_id": "BankOfNowhere"},
        device_label="Primary iPhone",
    )
    assert missing["ok"] is False
    assert missing["failure"] == "APP_UNSUPPORTED"


def test_query_pending_message() -> None:
    create_phone_action(
        device_id=DEVICE,
        role="primary_companion",
        instance_id="tab",
        session_id="s",
        origin=ORIGIN,
        arguments={
            "operation": "direct_message",
            "contact_query": "Sahil Patel",
            "message": "I'll be ten minutes late.",
        },
        device_label="Primary iPhone",
    )
    who = apply_confirmation_utterance(device_id=DEVICE, origin=ORIGIN, text="Who are you sending it to?")
    assert who and "Sahil Patel" in who["spoken"]
    what = apply_confirmation_utterance(device_id=DEVICE, origin=ORIGIN, text="What exactly are you sending?")
    assert what and "ten minutes late" in what["spoken"]


def test_unrelated_yes_does_not_fire_without_pending() -> None:
    assert apply_confirmation_utterance(device_id=DEVICE, origin=ORIGIN, text="Yes") is None
