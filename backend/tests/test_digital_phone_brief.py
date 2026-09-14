"""iPhone PWA: summaries first, details on follow-up, no surprise send/reroute."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.digital.adapters.whatsapp import FakeWhatsAppBacking
from app.digital.fabric import OpContext
from app.digital.phone_brief import (
    classify_phone_manner,
    is_phone_comm_ask,
    phone_inbox_turn,
    reset_phone_focus,
)
from app.digital.phone_turn import maybe_digital_turn
from app.digital.types import AutonomyLevel
from tests.test_digital_operations import FakeGmailTransport, _lease

LONG = "Can you send the notes from standup when you get a minute? " + ("lorem ipsum dolor sit amet. " * 40)


def _imessage(who: str, body: str) -> dict:
    return {
        "text": f"{who}: {body}",
        "handle": who,
        "preview": body[:220],
        "kind": "live_mac",
        "memory_type": "message.imessage.received",
        "channel": "imessage",
        "source": "imessage",
        "when": "2026-09-09T10:00:00+00:00",
    }


@pytest.fixture(autouse=True)
def _clear_focus() -> None:
    reset_phone_focus()
    yield
    reset_phone_focus()


def test_last_message_ask_is_a_phone_comm_ask() -> None:
    from app.digital.identity import extract_person_query

    assert extract_person_query("what message do I have last") == ""
    assert extract_person_query("what are my latest WhatsApp messages") == ""
    assert extract_person_query("latest WhatsApp with Mansi") == "Mansi"
    assert is_phone_comm_ask("what message do I have last")
    assert is_phone_comm_ask("what are my latest messages")
    assert classify_phone_manner("what message do I have last", has_focus=False) == "digest"
    assert classify_phone_manner("more about that particular chat", has_focus=True) == "details"
    assert classify_phone_manner("reroute those chats to Rahul", has_focus=True) == "reroute"
    assert not is_phone_comm_ask("send a whatsapp to Mom")


@pytest.mark.asyncio
async def test_last_messages_are_summaries_not_bodies(db_session) -> None:
    transport = FakeGmailTransport()
    transport.add_message(sender="Akash <a@ex.com>", subject="Quote", text=LONG)
    wa = FakeWhatsAppBacking(
        chats={
            "c1": {
                "name": "Mansi",
                "unread": 1,
                "messages": [{"id": "1", "from_me": False, "text": LONG, "timestamp": "1"}],
            }
        }
    )
    ctx = OpContext(
        actor="phone",
        session=db_session,
        lease=_lease(),
        transport=transport,
        whatsapp_backing=wa,
        autonomy=AutonomyLevel.READ,
    )
    device = SimpleNamespace(id="primary-iphone")

    async def peek(who, limit):
        del who, limit
        return [_imessage("Puran", LONG)]

    out = await phone_inbox_turn(
        db_session,
        "what message do I have last",
        device=device,
        ctx=ctx,
        imessage_peek=peek,
    )
    spoken = out["spoken"].lower()
    assert out["sent"] is False
    assert out["unauthorized_sends"] == 0
    assert "lorem ipsum" not in spoken
    assert LONG not in out["spoken"]
    assert "ask for more about" in spoken
    assert "mansi" in spoken or "puran" in spoken or "akash" in spoken or "recent mail" in spoken


@pytest.mark.asyncio
async def test_more_details_uses_focused_chat(db_session) -> None:
    wa = FakeWhatsAppBacking(
        chats={
            "c1": {
                "name": "Mansi",
                "messages": [{"id": "1", "from_me": False, "text": "Let's meet after work about the project.", "timestamp": "1"}],
            }
        }
    )
    ctx = OpContext(actor="phone", session=db_session, whatsapp_backing=wa, autonomy=AutonomyLevel.READ)
    device = SimpleNamespace(id="primary-iphone")

    first = await phone_inbox_turn(
        db_session,
        "what are my latest WhatsApp messages",
        device=device,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    assert first["sent"] is False
    assert "lorem ipsum" not in first["spoken"].lower()

    more = await phone_inbox_turn(
        db_session,
        "more about that particular chat",
        device=device,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    assert more["manner"] == "details"
    assert more["sent"] is False
    assert "mansi" in more["spoken"].lower()
    assert "let's meet after work about the project" not in more["spoken"].lower()


@pytest.mark.asyncio
async def test_reroute_is_explicit_and_does_not_send(db_session) -> None:
    wa = FakeWhatsAppBacking(
        chats={"c1": {"name": "Mansi", "messages": [{"id": "1", "from_me": False, "text": "hi", "timestamp": "1"}]}}
    )
    ctx = OpContext(actor="phone", session=db_session, whatsapp_backing=wa, autonomy=AutonomyLevel.READ)
    device = SimpleNamespace(id="secondary-iphone")
    await phone_inbox_turn(
        db_session,
        "what are my latest WhatsApp messages",
        device=device,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    silent = await phone_inbox_turn(
        db_session,
        "those chats look useful",
        device=device,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    assert silent["sent"] is False
    routed = await phone_inbox_turn(
        db_session,
        "reroute those chats to Rahul",
        device=device,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    assert routed["manner"] == "reroute"
    assert routed["sent"] is False
    assert routed["unauthorized_sends"] == 0
    assert wa.sent == []
    assert "not sent" in routed["spoken"].lower()
    hold = await phone_inbox_turn(
        db_session,
        "do not send",
        device=device,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    assert hold["manner"] == "hold"
    assert hold["sent"] is False
    assert wa.sent == []
    assert "not sent" in hold["spoken"].lower()


@pytest.mark.asyncio
async def test_phone_turn_intercepts_last_message(db_session) -> None:
    device = SimpleNamespace(id="primary-iphone")
    wa = FakeWhatsAppBacking(
        chats={"c1": {"name": "Mansi", "messages": [{"id": "1", "from_me": False, "text": LONG, "timestamp": "1"}]}}
    )
    ctx = OpContext(actor="phone", session=db_session, whatsapp_backing=wa, autonomy=AutonomyLevel.READ)
    hit = await maybe_digital_turn(
        db_session,
        "what message do I have last",
        device=device,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    assert hit is not None
    assert hit["route"] == "DIGITAL_OPS"
    assert "lorem ipsum" not in (hit.get("reply") or "").lower()
    digital = hit.get("digital") or {}
    assert digital.get("sent") is False
    assert digital.get("messages") in (None, [])


@pytest.mark.asyncio
async def test_two_iphones_keep_separate_focus(db_session) -> None:
    wa = FakeWhatsAppBacking(
        chats={
            "c1": {"name": "Mansi", "messages": [{"id": "1", "from_me": False, "text": "alpha thread", "timestamp": "1"}]},
            "c2": {"name": "Puran", "messages": [{"id": "1", "from_me": False, "text": "beta thread", "timestamp": "1"}]},
        }
    )
    ctx = OpContext(actor="phone", session=db_session, whatsapp_backing=wa, autonomy=AutonomyLevel.READ)
    primary = SimpleNamespace(id="primary-iphone")
    secondary = SimpleNamespace(id="secondary-iphone")
    await phone_inbox_turn(
        db_session,
        "latest WhatsApp with Mansi",
        device=primary,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    await phone_inbox_turn(
        db_session,
        "latest WhatsApp with Puran",
        device=secondary,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    a = await phone_inbox_turn(
        db_session,
        "more about that particular chat",
        device=primary,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    b = await phone_inbox_turn(
        db_session,
        "more about that particular chat",
        device=secondary,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    assert "mansi" in a["spoken"].lower()
    assert "puran" in b["spoken"].lower()
    assert "mansi" not in b["spoken"].lower()
    assert "puran" not in a["spoken"].lower()


@pytest.mark.asyncio
async def test_gmail_and_imessage_follow_the_same_summary_format(db_session) -> None:
    transport = FakeGmailTransport()
    transport.add_message(sender="Akash <a@ex.com>", subject="Quote", text=LONG)
    ctx = OpContext(
        actor="phone",
        session=db_session,
        lease=_lease(),
        transport=transport,
        whatsapp_backing=FakeWhatsAppBacking(chats={}),
        autonomy=AutonomyLevel.READ,
    )
    device = SimpleNamespace(id="primary-iphone")

    mail = await phone_inbox_turn(
        db_session,
        "what mail do I have last",
        device=device,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    assert mail["manner"] == "digest"
    assert "lorem ipsum" not in mail["spoken"].lower()
    assert "akash" in mail["spoken"].lower() or "quote" in mail["spoken"].lower()
    more_mail = await phone_inbox_turn(
        db_session,
        "more about that particular chat",
        device=device,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    assert more_mail["manner"] == "details"
    assert "lorem ipsum" not in more_mail["spoken"].lower()
    assert LONG not in more_mail["spoken"]

    reset_phone_focus()
    texts = await phone_inbox_turn(
        db_session,
        "what are my latest messages",
        device=device,
        ctx=ctx,
        imessage_peek=lambda *_: [_imessage("Puran", LONG)],
    )
    assert "lorem ipsum" not in texts["spoken"].lower()
    assert "puran" in texts["spoken"].lower()
    more_im = await phone_inbox_turn(
        db_session,
        "more about that particular chat",
        device=device,
        ctx=ctx,
        imessage_peek=lambda *_: [_imessage("Puran", LONG)],
    )
    assert more_im["manner"] == "details"
    assert "puran" in more_im["spoken"].lower()
    assert "lorem ipsum" not in more_im["spoken"].lower()
    assert LONG not in more_im["spoken"]


@pytest.mark.asyncio
async def test_mixed_digest_focus_keeps_mail_and_chat_halves(db_session) -> None:
    from app.digital.phone_brief import _split_items, get_phone_focus

    transport = FakeGmailTransport()
    transport.add_message(sender="Akash <a@ex.com>", subject="Quote", text=LONG)
    wa = FakeWhatsAppBacking(
        chats={
            "c1": {
                "name": "Mansi",
                "messages": [{"id": "1", "from_me": False, "text": "alpha thread", "timestamp": "1"}],
            }
        }
    )
    ctx = OpContext(
        actor="phone",
        session=db_session,
        lease=_lease(),
        transport=transport,
        whatsapp_backing=wa,
        autonomy=AutonomyLevel.READ,
    )
    device = SimpleNamespace(id="primary-iphone")
    digest = await phone_inbox_turn(
        db_session,
        "catch me up",
        device=device,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    assert digest["manner"] == "digest"
    focus = get_phone_focus("primary-iphone")
    assert focus is not None
    mail_items, chat_items = _split_items(focus.items)
    assert mail_items, "mixed digest must keep the mail half in focus"
    assert chat_items, "mixed digest must keep the chat half in focus"
    # Follow-up on the mail half resolves from focus instead of failing.
    more = await phone_inbox_turn(
        db_session,
        "more about that mail from Akash",
        device=device,
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    assert more["manner"] == "details"
    assert "akash" in more["spoken"].lower() or "quote" in more["spoken"].lower()
    assert LONG not in more["spoken"]


@pytest.mark.asyncio
async def test_phone_brief_hub_on_skips_gmail_and_whatsapp_web(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def empty(*args, **kwargs):
        del args, kwargs
        return []

    async def boom(*args, **kwargs):
        raise AssertionError(f"fabric must not run: {args} {kwargs}")

    monkeypatch.setattr("app.digital.phone_brief._mac_hub_on", lambda: True)
    monkeypatch.setattr("app.digital.phone_brief._peek_mac_mail", empty)
    monkeypatch.setattr("app.digital.phone_brief._peek_mac_whatsapp", empty)
    monkeypatch.setattr("app.digital.phone_brief.execute", boom)
    ctx = OpContext(
        actor="phone",
        session=db_session,
        lease=_lease(),
        transport=FakeGmailTransport(),
        whatsapp_backing=FakeWhatsAppBacking(),
        autonomy=AutonomyLevel.READ,
    )
    out = await phone_inbox_turn(
        db_session,
        "catch me up",
        device=SimpleNamespace(id="primary-iphone"),
        ctx=ctx,
        imessage_peek=lambda *_: [],
    )
    assert out["sent"] is False
    assert out["unauthorized_sends"] == 0
