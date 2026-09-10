"""Generalized messaging graph: channels, recipients, routing, summaries.

The P0 bug was not one phrase: it was that channel, recipient, and body were
each silently rewritten on the way out. These tests pin the shared logic that
every send path now uses, plus the notification/conversation summary shape.
"""

from __future__ import annotations

import pytest

from app.ev.messaging import (
    channels,
    match_recipient,
    route_channel,
    score_person_name,
    unknown_channel,
)
from app.ev.messaging.recipients import ambiguous_recipient_spoken, verify_peer


def test_channel_registry_normalizes_aliases_including_unwired() -> None:
    assert channels.normalize_channel("WhatsApp") == "whatsapp"
    assert channels.normalize_channel("wa") == "whatsapp"
    assert channels.normalize_channel("iMessage") == "imessage"
    assert channels.normalize_channel("SMS") == "sms"
    assert channels.normalize_channel("e-mail") == "mail"
    assert channels.normalize_channel("Telegram") == "telegram"
    assert channels.normalize_channel("signal") == "signal"
    assert channels.normalize_channel("discord") == "discord"
    assert channels.normalize_channel("pigeon") is None
    assert channels.normalize_channel("") is None


def test_channel_detection_prefers_preposition_and_ignores_body_words() -> None:
    assert channels.detect_channel("send it on whatsapp") == "whatsapp"
    assert channels.detect_channel("send a WhatsApp message to Sam") == "whatsapp"
    assert channels.detect_channel("text Sarah I'll call later") is None
    assert channels.detect_channel("send a message to mom about the email") is None
    assert channels.detect_channel("send a telegram message to Sam") == "telegram"
    assert channels.detect_channel("text mom on tuesday we meet") is None
    assert unknown_channel("send it on pigeon") == "pigeon"
    assert unknown_channel("send it on tuesday") is None
    assert unknown_channel("send it on the way") is None


def test_route_channel_never_silently_downgrades() -> None:
    # Unwired channel: unavailable, no helper command to fall back to.
    telegram = route_channel("telegram", helper_available=True)
    assert telegram.mode == "unavailable"
    assert telegram.helper_command is None
    assert "Telegram" in telegram.spoken
    # Explicit SMS keeps its service; generic messages is auto.
    sms = route_channel("sms", helper_available=True)
    assert sms.channel == "sms" and sms.service == "sms" and sms.mode == "send"
    generic = route_channel(None, helper_available=True)
    assert generic.channel == "messages" and generic.service == "auto"
    # WhatsApp can prepare but cannot claim a tap-free send.
    whatsapp = route_channel("whatsapp", helper_available=True)
    assert whatsapp.mode == "compose" and whatsapp.service is None
    # Unknown channel with a name is refused, never mapped onto Messages.
    refused = route_channel("pigeon", helper_available=True)
    assert refused.mode == "unavailable"
    # No bridge at all stays honest.
    offline = route_channel("messages", helper_available=False)
    assert offline.mode == "unavailable"


def test_person_scoring_is_whole_token_not_substring() -> None:
    # The P0 mis-resolution: John must not match Johnson.
    assert score_person_name("John", "Johnson") == 0.0
    assert score_person_name("john", "John Smith") > 0.8
    assert score_person_name("John Smith", "John Smith") == 1.0
    assert score_person_name("Smith", "John Smith") > 0.7
    assert score_person_name("Sam", "Samantha") == 0.0


def test_match_recipient_unique_ambiguous_direct_and_none() -> None:
    rows = [
        {"id": "1", "full_name": "John Smith", "phone_numbers": ["+15550100"]},
        {"id": "2", "full_name": "John Doe", "phone_numbers": ["+15550101"]},
        {"id": "3", "full_name": "Johnson Lee", "phone_numbers": ["+15550102"]},
    ]
    single = match_recipient("John Smith", rows)
    assert single.status == "unique"
    assert single.primary_phone == "+15550100"

    ambiguous = match_recipient("John", rows)
    assert ambiguous.status == "ambiguous"
    assert len(ambiguous.candidates) == 2
    assert any("John Smith" in name for name in ambiguous.candidates)
    spoken = ambiguous_recipient_spoken(ambiguous).lower()
    assert "john smith" in spoken and "john doe" in spoken

    # Johnson alone is still not John.
    assert match_recipient("John", [rows[2]]).status == "none"

    direct = match_recipient("+1 555 0100", rows)
    assert direct.status == "direct"
    assert match_recipient("nobody at all", rows).status == "none"


def test_verify_peer_rejects_loose_whatsapp_matches() -> None:
    assert verify_peer("John", {"handle": "Johnson", "phone": "+1"}) is False
    assert verify_peer("John", {"handle": "John Smith", "phone": "+1"}) is True


@pytest.mark.asyncio
async def test_dispatch_sms_keeps_service_and_recipient(db_session, monkeypatch) -> None:
    from app.ev.policy import PolicyDecision
    from app.ev.tools import dispatch
    from app.integrations.life_helper import LifeHelperResult

    seen: list[tuple[str, dict]] = []

    async def fake_helper(command, args, helper_path=None):
        del helper_path
        seen.append((command, dict(args)))
        if command == "contacts.resolve":
            return LifeHelperResult(
                command,
                {
                    "matches": [
                        {
                            "id": "c1",
                            "full_name": "John Smith",
                            "phone_numbers": ["+15550100"],
                            "email_addresses": [],
                        }
                    ]
                },
                {},
            )
        if command == "messages.send":
            return LifeHelperResult(
                command,
                {"to": args.get("to"), "sent": True, "service": args.get("service")},
                {"evidence": {"sent": True, "to": args.get("to")}},
            )
        raise AssertionError(command)

    monkeypatch.setattr(
        "app.services.life_stream_daemon.life_stream_should_run", lambda: True
    )
    monkeypatch.setattr(
        "app.services.life_stream_daemon.LifeStreamDaemon.resolve_whatsapp_peer",
        lambda self, query: None,
    )
    monkeypatch.setattr("app.ev.apps.discover_life_helper_path", lambda: "/tmp/ev-helper")
    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", fake_helper)
    monkeypatch.setattr("app.ev.policy.provider_connected", lambda *_a, **_k: True)

    async def fake_authorize(*_args, **_kwargs):
        return PolicyDecision(
            allowed=True,
            effect="allow",
            reason="ok",
            risk_class="R2",
            confirmation_required=False,
            confirmation_policy="none",
            provider="messaging",
            spoken="",
        )

    monkeypatch.setattr("app.ev.policy.authorize", fake_authorize)
    response = await dispatch(
        db_session,
        "send_message",
        {"to": "John Smith", "text": "running late", "channel": "sms"},
        actor="master",
        allow_sensitive=True,
    )
    body = response.result or {}
    sends = [args for command, args in seen if command == "messages.send"]
    assert sends and sends[0]["service"] == "sms"
    assert sends[0]["to"] == "+15550100"
    assert sends[0]["text"] == "running late"
    assert "whatsapp.send" not in [command for command, _ in seen]
    assert body.get("sent") is True
    assert body.get("channel") == "sms"


def test_briefing_shapes_summary_requests() -> None:
    from app.ev.briefing import _clip, infer_args

    assert infer_args("recall_history", "any new notifications") == {
        "query": "any new notifications"
    }
    assert infer_args("list_messages", "last message from Mansi") == {
        "limit": 8,
        "query": "last message from Mansi",
    }
    assert infer_args("get_upcoming_alerts", "any alerts") == {"limit": 8}

    clipped = _clip({"count": 3, "spoken": "Three alerts: build, backup, bill."})
    assert clipped.startswith('{"spoken"')


def test_alert_digest_is_a_summary_not_a_dump() -> None:
    from app.ev.tools import _alerts_spoken

    assert _alerts_spoken([]) == "Nothing pending — no alerts."
    one = _alerts_spoken([{"title": "Backup finished", "priority": "low"}])
    assert "1 alert" in one and "Backup finished" in one
    many = _alerts_spoken(
        [
            {"title": "Build failed", "priority": "urgent"},
            {"title": "Bill due", "priority": "useful"},
            {"title": "Backup", "priority": "background"},
            {"title": "Standup", "priority": "background"},
        ]
    )
    assert "4 alerts" in many
    assert "urgent" in many
    assert "plus 1 more" in many
    assert all(title in many for title in ("Build failed", "Bill due", "Backup"))
