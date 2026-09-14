"""Regression locks for the swarm-found messaging gaps.

Each test names the real-life failure it prevents:
- a failed send must never sound delivered, and never leak class names;
- a parked send needs explicit consent ("ok" alone is not approval);
- cancelling after the send went out must not claim it was cancelled;
- "did I call X" must never dial;
- phone/email recipients, unknown channels, and one-word bodies parse;
- a Contacts TCC failure must not block a WhatsApp send WhatsApp resolved;
- in-app asks must not invent stopword items.
"""

from __future__ import annotations

import pytest

from app.ev.briefing import plan_life_tool_calls
from app.ev.in_app import parse_in_app_intent
from app.ev.messaging.approval import (
    is_negative,
    is_send_approval_affirmative,
)
from app.ev.send_intent import incomplete_send_recipient, parse_send_intent
from app.ev.tools import life_success_reply


def test_failed_sends_never_claim_delivery_or_leak_class_names() -> None:
    permission = life_success_reply(
        {"ok": False, "error": "LifePermissionDeniedError: denied"},
        tool_name="send_message",
    )
    lowered = permission.lower()
    assert "sent" not in lowered or "nothing was sent" in lowered
    assert "lifepermissiondeniederror" not in lowered
    assert "system settings" in lowered

    simulated = life_success_reply(
        {"ok": True, "sent": True, "simulated": True, "to": "a@b.c"},
        tool_name="send_mail",
    )
    assert not simulated.lower().startswith("sent email")


def test_send_approval_needs_an_explicit_consent_token() -> None:
    for phrase in ("ok", "okay", "sure", "please", "correct", "go", "do", "sounds good"):
        assert not is_send_approval_affirmative(phrase), phrase
    for phrase in ("yes", "yeah", "yep", "confirm", "yes please", "ok send it", "do it"):
        assert is_send_approval_affirmative(phrase), phrase
    assert is_send_approval_affirmative("send it")
    assert is_negative("no")
    assert is_negative("cancel")


def test_call_inquiries_never_plan_a_real_call() -> None:
    offered = {"place_call", "resolve_contact", "send_message"}
    for phrase in ("did I call Maya", "have you called Maya", "did Mom call", "did I miss a call from Maya"):
        calls = plan_life_tool_calls(phrase, offered)
        assert all(call.name != "place_call" for call in calls), phrase
    planned = plan_life_tool_calls("call Maya", offered)
    assert any(call.name == "place_call" for call in planned)


def test_parser_destinations_unknown_channels_and_lowercase_bodies() -> None:
    phone = parse_send_intent("text +1 555 0100 I'm late")
    assert phone == {"to": "+1 555 0100", "text": "I'm late"}
    wa_phone = parse_send_intent("send a WhatsApp message to +91 98765 43210 saying hi")
    assert wa_phone is not None
    assert wa_phone["to"] == "+91 98765 43210" and wa_phone["channel"] == "whatsapp"
    email = parse_send_intent("email john@example.com saying the deck is ready")
    assert email is not None and email["to"] == "john@example.com" and email["channel"] == "mail"

    unknown = parse_send_intent("send hi to Sam on pigeon")
    assert unknown is not None and unknown.get("channel") == "pigeon"

    assert parse_send_intent("text mom bye") == {"to": "mom", "text": "bye"}
    # "to <name>" keeps lowercase second words as part of the name.
    assert parse_send_intent("send a message to customer care") is None
    assert incomplete_send_recipient("send a message to customer care") == "customer care"
    assert incomplete_send_recipient("text +1 555 0100") == "+1 555 0100"


def test_in_app_rejects_stopword_items() -> None:
    assert parse_in_app_intent("open the chat in WhatsApp") is None
    assert parse_in_app_intent("play some music") is None
    intent = parse_in_app_intent("open John's chat in WhatsApp")
    assert intent is not None and intent.item == "John"


@pytest.mark.asyncio
async def test_cancel_after_execute_never_claims_cancellation(db_session) -> None:
    from app.ev.messaging.approval import cancel_pending, park_send
    from app.utils.text import utcnow

    action = await park_send(
        db_session, to="John", text="running late", display="John", actor="voice"
    )
    action.status = "executed"
    action.executed_at = utcnow()
    action.result = {"ok": True, "sent": True, "spoken": "Sent WhatsApp to John."}
    await db_session.flush()

    outcome = await cancel_pending(db_session, action, actor="voice")
    assert outcome.get("cancelled") is False
    assert outcome.get("sent") is True
    assert "already been sent" in str(outcome.get("spoken") or "")


@pytest.mark.asyncio
async def test_whatsapp_exact_name_beats_punctuation_and_duplicate_rows() -> None:
    """Real case: seven 'Mansi' chats must not block sending to 'Mansi'."""

    from app.ev.messaging.whatsapp_web import resolve

    rows = [
        {"name": "Mansi", "chat_ref": "Mansi", "gist": "+91 832 061 9357"},
        {"name": "Mansi\u2026!!", "chat_ref": "Mansi\u2026!!", "gist": ""},
        {"name": "Mansi Makani", "chat_ref": "Mansi Makani", "gist": ""},
        {"name": "Mansi(D)", "chat_ref": "Mansi(D)", "gist": ""},
    ]
    match = await resolve("Mansi", rows=rows)
    assert match["status"] == "unique"
    assert match["display"] == "Mansi"

    # The same chat rendered twice (chat + contact search) is one chat.
    dupes = [
        {"name": "AbhishekKaDoosra", "chat_ref": "AbhishekKaDoosra", "gist": "Oye"},
        {"name": "AbhishekKaDoosra", "chat_ref": "AbhishekKaDoosra", "gist": ""},
    ]
    assert (await resolve("AbhishekKaDoosra", rows=dupes))["status"] == "unique"

    # Two genuinely different chats with the same name stay a question.
    two = [
        {"name": "John", "chat_ref": "John A"},
        {"name": "John", "chat_ref": "John B"},
    ]
    assert (await resolve("John", rows=two))["status"] == "ambiguous"


def test_chrome_apple_events_failure_names_the_chrome_setting() -> None:
    from app.ev.messaging.failures import spoken_failure

    spoken = spoken_failure(
        {"error": "whatsapp_web_unavailable", "diagnosis": "javascript_apple_events_disabled"},
        channel="whatsapp",
    )
    assert "Allow JavaScript from Apple Events" in spoken
    assert "View" in spoken and "Developer" in spoken


@pytest.mark.asyncio
async def test_adapter_whatsapp_send_survives_contacts_permission_denied(
    monkeypatch,
) -> None:
    """The reported failure: Contacts TCC denied must not abort a WhatsApp send."""

    import app.integrations.adapters as adapters
    from app.integrations.adapters import BUILTIN_ADAPTERS, MessagingAdapter
    from app.integrations.life_helper import LifePermissionDeniedError

    adapter = next(
        item for item in BUILTIN_ADAPTERS if isinstance(item, MessagingAdapter)
    )

    async def native_unique(channel, to):
        assert channel == "whatsapp"
        return {
            "status": "unique",
            "id": "chat-john",
            "display": "John",
            "phone": "",
            "source": "whatsapp_web",
        }

    async def contacts_denied(*_args, **_kwargs):
        raise LifePermissionDeniedError("Apple life permission denied")

    async def fake_web_send(to, text):
        return {
            "ok": True,
            "sent": True,
            "verified_in_thread": True,
            "to": to,
            "focus_theft": 0,
            "spoken": f"Sent WhatsApp to {to}.",
        }

    import app.ev.messaging.whatsapp_web as whatsapp_web

    async def web_up(**_kwargs):
        return True

    monkeypatch.setattr(
        "app.ev.messaging.native.resolve_native_contact", native_unique
    )
    monkeypatch.setattr(adapters, "_resolve_life_contact", contacts_denied)
    monkeypatch.setattr(whatsapp_web, "web_available", web_up)
    monkeypatch.setattr(whatsapp_web, "send", fake_web_send)

    result = await adapter._macos_life_act(
        "messaging.send",
        {"to": "John", "text": "running late", "channel": "whatsapp"},
        ["messaging:act"],
        {
            "provider": "macos_life",
            "contact_allowlist": "all",
            "life_confirm_unknown": True,
        },
    )
    assert result.get("ok") is True
    assert result.get("sent") is True
    assert result.get("mode") == "whatsapp_web"
