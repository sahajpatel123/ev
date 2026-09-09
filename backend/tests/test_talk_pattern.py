"""Talk-pattern board and send intent — meaning, not phrase recipes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.ev.send_intent import parse_send_intent
from app.ev.tool_select import resolve_live_action, select_tool
from app.memory.life_archive.talk import (
    TalkBoard,
    TalkPerson,
    axes_for,
    is_talk_pattern_query,
    speak_board,
)


def test_talk_pattern_is_the_graph_not_one_thread() -> None:
    assert is_talk_pattern_query("who do I talk with most") is True
    assert is_talk_pattern_query("who do I talk to") is True
    assert is_talk_pattern_query("who have I been texting") is True
    assert is_talk_pattern_query("people I usually message") is True
    assert is_talk_pattern_query("last chat with Ada") is False
    assert is_talk_pattern_query("summarize my chat with Ada") is False
    assert is_talk_pattern_query("text Mom I'm late") is False
    assert is_talk_pattern_query("who am I leaving hanging?") is False
    assert is_talk_pattern_query("what did we talk about") is False


def test_talk_axes_are_meaning_not_a_template() -> None:
    assert axes_for("who do I talk with most") == frozenset({"active"})
    assert axes_for("who did I message most recently") == frozenset({"recency"})
    assert axes_for("who have I messaged the most ever") == frozenset({"volume"})
    assert axes_for("who do I talk to") == frozenset({"active"})
    assert "volume" not in axes_for("who do I talk to")


def test_talk_board_speaks_disagreeing_axes_from_facts() -> None:
    clock = datetime(2026, 9, 7, tzinfo=UTC)
    board = TalkBoard(
        people=[
            TalkPerson(
                name="Ada",
                volume=400,
                last_at=clock - timedelta(days=20),
                channels={"whatsapp"},
            ),
            TalkPerson(
                name="Maya",
                volume=40,
                last_at=clock - timedelta(hours=3),
                channels={"whatsapp"},
            ),
        ]
    )
    spoken = speak_board(board, frozenset({"volume"}), clock=clock)
    lowered = spoken.lower()
    assert "ada" in lowered
    assert "maya" in lowered
    assert "cannot find" not in lowered
    recent = speak_board(board, frozenset({"recency"}), clock=clock)
    assert "maya" in recent.lower()
    empty = speak_board(TalkBoard(), frozenset({"volume"}))
    assert "reliable" in empty.lower()


def test_talk_most_is_recent_activity_not_old_volume() -> None:
    clock = datetime(2026, 9, 7, tzinfo=UTC)
    board = TalkBoard(
        people=[
            TalkPerson(
                name="Gopal",
                volume=11000,
                recent=0,
                last_at=clock - timedelta(days=200),
                channels={"whatsapp"},
            ),
            TalkPerson(
                name="Puran",
                volume=40,
                recent=1,
                last_at=clock - timedelta(days=2),
                channels={"whatsapp"},
            ),
            TalkPerson(
                name="Mansi",
                volume=800,
                recent=18,
                last_at=clock - timedelta(hours=2),
                channels={"whatsapp"},
            ),
        ]
    )
    spoken = speak_board(board, frozenset({"active"}), clock=clock)
    lowered = spoken.lower()
    assert "mansi" in lowered
    assert "gopal" not in lowered
    assert "puran" not in lowered
    assert "these days" in lowered
    ever = speak_board(board, frozenset({"volume"}), clock=clock)
    assert "gopal" in ever.lower()


def test_send_intent_covers_acts_not_one_verb() -> None:
    late = parse_send_intent("text Mom I'm late")
    assert late is not None
    assert late["to"].lower() == "mom"
    assert "late" in late["text"].lower()
    tell = parse_send_intent("tell Mom that I'm late")
    assert tell is not None and tell["to"].lower() == "mom"
    know = parse_send_intent("let Dad know I'm on my way")
    assert know is not None and know["to"].lower() == "dad"
    wa = parse_send_intent("whatsapp Maya I'm outside")
    assert wa is not None
    assert wa.get("channel") == "whatsapp"
    mail = parse_send_intent("email Ada the deck is ready")
    assert mail is not None
    assert mail.get("channel") == "mail"
    assert parse_send_intent("tell me about my conversations") is None
    assert parse_send_intent("how are you") is None
    assert parse_send_intent("Did I get any email from Alex?") is None
    assert parse_send_intent("Do I get any email from Alex?") is None
    assert parse_send_intent("any email from Alex") is None


def test_send_intent_reply_shape_and_no_saying_recipient() -> None:
    reply = parse_send_intent("reply to Mansi on whatsapp saying ok")
    assert reply is not None
    assert reply["to"] == "Mansi"
    assert reply["text"].lower() == "ok"
    assert reply.get("channel") == "whatsapp"
    simple = parse_send_intent("reply to Alex saying thanks")
    assert simple is not None and simple["to"] == "Alex"
    # Body lead-ins are never a recipient.
    assert parse_send_intent("reply saying ok") is None


def test_resolve_live_action_send_from_varied_speech() -> None:
    for phrase in (
        "text Mom I'm late",
        "tell Mom I'm late",
        "let Mom know I'm late",
        "send a whatsapp to Mom I'm late",
    ):
        resolved = resolve_live_action(phrase)
        assert resolved is not None, phrase
        assert resolved[0] == "send_message", phrase
        assert resolved[1]["to"].lower() == "mom"
        assert "late" in str(resolved[1].get("text") or "").lower()
    assert select_tool("tell me about my conversations with different people").selected != "send_message"


def test_reads_never_route_to_send_message() -> None:
    from app.memory.life_archive.locate import classify_shelf

    for phrase in (
        "latest message from mansi",
        "last message from mom",
        "did mom call",
        "check whatsapp",
    ):
        assert select_tool(phrase).selected != "send_message", phrase
        resolved = resolve_live_action(phrase)
        assert resolved is not None and resolved[0] != "send_message", phrase
    assert select_tool("latest message from mansi").selected == "recall_history"
    assert resolve_live_action("latest message from mansi")[0] == "recall"
    assert classify_shelf("latest message from mansi") == "inbox"
    assert resolve_live_action("check whatsapp")[0] == "list_messages"


def test_call_inquiries_never_place_calls_but_requests_do() -> None:
    for phrase in (
        "did you call mom",
        "have you called mom",
        "did evie call mom",
        "do you call mom",
        "did mom call",
    ):
        assert select_tool(phrase).selected != "place_call", phrase
        resolved = resolve_live_action(phrase)
        assert resolved is None or resolved[0] != "place_call", phrase
    for phrase in (
        "call mom",
        "ring mom",
        "please call mom",
        "can you call mom",
        "could you call mom",
        "will you call mom",
        "would you call mom",
    ):
        assert select_tool(phrase).selected == "place_call", phrase
        resolved = resolve_live_action(phrase)
        assert resolved is not None and resolved[0] == "place_call", phrase


def test_incomplete_send_recipient_needs_verb_and_name() -> None:
    from app.ev.send_intent import incomplete_send_recipient

    for phrase, who in (
        ("email mom", "mom"),
        ("mail mom", "mom"),
        ("text mom", "mom"),
        ("message mom", "mom"),
        ("tell mom", "mom"),
        ("reply to mom", "mom"),
        ("reply to mom on whatsapp", "mom"),
        ("let mom know", "mom"),
        ("send mom a text", "mom"),
        ("send a text to mom", "mom"),
        ("text mom on whatsapp", "mom"),
        ("send mom a text on whatsapp", "mom"),
        ("please text mom", "mom"),
        ("can you email mom", "mom"),
        ("will you message mom", "mom"),
    ):
        assert incomplete_send_recipient(phrase) == who, phrase
    # Complete sends, inquiries, and reads are never "incomplete".
    for phrase in (
        "email mom the update",
        "text mom hi",
        "reply to mom saying ok",
        "any email from mom",
        "did mom text me",
        "tell me about my chats",
        "message from mansi",
        "text me",
        "reply on whatsapp",
        "whatsapp mom",
        "what did mom say",
        "did you text mom",
        "catch me up on whatsapp",
    ):
        assert incomplete_send_recipient(phrase) == "", phrase


def test_incomplete_sends_prompt_for_body_never_read_or_search() -> None:
    from app.ev.spark_act import ActDecision, fallback_act, live_tool_for_act
    from app.ev.tool_select import _mail_live_read
    from app.memory.life_archive.locate import classify_shelf

    for phrase in (
        "email mom",
        "mail mom",
        "text mom",
        "message mom",
        "reply to mom",
        "send mom a text",
        "text mom on whatsapp",
    ):
        assert classify_shelf(phrase) is None, phrase
        assert _mail_live_read(phrase) is False, phrase
        resolved = resolve_live_action(phrase)
        assert resolved is None or resolved[0] != "list_mail", phrase
        assert fallback_act(phrase) is not None, phrase
        assert fallback_act(phrase).act == "life", phrase
        assert live_tool_for_act(phrase, ActDecision(act="life")) is None, phrase
        assert live_tool_for_act(phrase, ActDecision(act="recall")) is None, phrase
    # Reads keep their drawers and tools.
    assert classify_shelf("any email from mom") == "mail"
    assert _mail_live_read("any email from mom") is True
    assert resolve_live_action("check whatsapp")[0] == "list_messages"


def test_send_grammar_rejects_preposition_recipients_and_channel_bodies() -> None:
    # "reply to mom" must not send to "To" with body "mom"; a body that is
    # only "on whatsapp" is a missing body, not message text.
    assert parse_send_intent("reply to mom") is None
    assert parse_send_intent("send a text to mom") is None
    parsed = parse_send_intent("reply to mom on whatsapp")
    assert parsed is None
    assert parse_send_intent("text mom on whatsapp") is None
    # File words are never recipients either.
    assert parse_send_intent("the whatsapp backup file") is None
    assert parse_send_intent("message file mom") is None
    # Real bodies still parse, including ones starting with "on".
    assert parse_send_intent("reply to mom saying ok")["text"] == "ok"
    assert parse_send_intent("text mom on tuesday we meet")["text"] == "on tuesday we meet"
    assert parse_send_intent("send mom a note saying hi")["text"] == "hi"
    assert select_tool("mail mom").selected == "send_message"


def test_sends_route_to_send_message_never_archive_shelves() -> None:
    from app.memory.life_archive.locate import classify_shelf

    for phrase, who in (
        ("send an email to Rahul saying the deck is ready", "rahul"),
        ("reply to Mansi on whatsapp saying ok", "mansi"),
        ("whatsapp Mansi hi", "mansi"),
        ("text Alex saying on my way", "alex"),
    ):
        assert classify_shelf(phrase) is None, phrase
        assert select_tool(phrase).selected == "send_message", phrase
        resolved = resolve_live_action(phrase)
        assert resolved is not None and resolved[0] == "send_message", phrase
        assert str(resolved[1].get("to") or "").lower() == who, phrase


@pytest.mark.asyncio
async def test_send_without_adapter_uses_helper_not_not_connected(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev.policy import PolicyDecision
    from app.ev.tools import dispatch
    from app.integrations.life_helper import LifeHelperResult

    async def fake_helper(command, args, helper_path=None):
        del helper_path
        if command == "contacts.resolve":
            return LifeHelperResult(
                command,
                {
                    "matches": [
                        {
                            "full_name": "Mom",
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
                {"to": args.get("to"), "sent": True},
                {"evidence": {"sent": True, "recipient": args.get("to")}},
            )
        raise AssertionError(command)

    monkeypatch.setattr(
        "app.services.life_stream_daemon.life_stream_should_run", lambda: True
    )
    monkeypatch.setattr("app.ev.apps.discover_life_helper_path", lambda: "/tmp/ev-helper")
    monkeypatch.setattr("app.ev.tools._channel_from_talk", lambda _to: None)
    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", fake_helper)
    monkeypatch.setattr(
        "app.ev.policy.provider_connected",
        lambda *_args, **_kwargs: True,
    )

    from app.ev.policy import PolicyDecision

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
        {"to": "Mom", "text": "I'm late"},
        actor="master",
        allow_sensitive=True,
    )
    body = response.result or {}
    assert body.get("error") != "not_connected"
    spoken = str(body.get("spoken") or "").lower()
    assert "not connected" not in spoken
    assert body.get("ok") is True
    assert body.get("sent") is True
    assert "mom" in spoken


@pytest.mark.asyncio
async def test_explicit_whatsapp_without_phone_stays_unsent(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev.tools import dispatch
    from app.integrations.life_helper import LifeHelperResult

    seen: list[str] = []

    async def fake_helper(command, args, helper_path=None):
        del helper_path
        seen.append(command)
        if command == "contacts.resolve":
            return LifeHelperResult(command, {"matches": []}, {})
        raise AssertionError(command)

    monkeypatch.setattr(
        "app.services.life_stream_daemon.life_stream_should_run", lambda: True
    )
    monkeypatch.setattr("app.ev.apps.discover_life_helper_path", lambda: "/tmp/ev-helper")
    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", fake_helper)
    monkeypatch.setattr(
        "app.ev.policy.provider_connected",
        lambda *_args, **_kwargs: True,
    )

    from app.ev.policy import PolicyDecision

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
        {"to": "Stranger", "text": "hi", "channel": "whatsapp"},
        actor="master",
        allow_sensitive=True,
    )
    body = response.result or {}
    # No contact match: must NOT fall through to iMessage and mis-send.
    assert body.get("ok") is False
    assert body.get("sent") is not True
    assert "messages.send" not in seen
    assert "whatsapp.send" not in seen
    assert "phone number for stranger" in str(body.get("spoken") or "").lower()


@pytest.mark.asyncio
async def test_place_call_without_phone_fails_friendly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unit-level: dispatch gates calls behind training-wheels onboarding,
    # which is unrelated to recipient resolution. Exercise the hub write
    # directly with no contact match — call.place must never run.
    from app.ev.tools import _mac_hub_life_write
    from app.integrations.life_helper import LifeHelperResult

    seen: list[str] = []

    async def fake_helper(command, args, helper_path=None):
        del helper_path
        seen.append(command)
        if command == "contacts.resolve":
            return LifeHelperResult(command, {"matches": []}, {})
        raise AssertionError(command)

    monkeypatch.setattr(
        "app.services.life_stream_daemon.life_stream_should_run", lambda: True
    )
    monkeypatch.setattr("app.ev.apps.discover_life_helper_path", lambda: "/tmp/ev-helper")
    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", fake_helper)
    body = await _mac_hub_life_write("place_call", {"name": "Stranger"})
    assert body is not None
    assert body.get("ok") is False
    assert "call.place" not in seen
    assert "phone number for stranger" in str(body.get("spoken") or "").lower()


@pytest.mark.asyncio
async def test_place_call_with_phone_dials_digits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ev.tools import _mac_hub_life_write
    from app.integrations.life_helper import LifeHelperResult

    seen_args: dict = {}

    async def fake_helper(command, args, helper_path=None):
        del helper_path
        if command == "contacts.resolve":
            return LifeHelperResult(
                command,
                {"matches": [{"full_name": "Mom", "phone_numbers": ["+1 (555) 0100"]}]},
                {},
            )
        if command == "call.place":
            seen_args.update(args)
            return LifeHelperResult(
                command,
                {"destination": args.get("destination"), "opened": True},
                {"evidence": {"opened": True}},
            )
        raise AssertionError(command)

    monkeypatch.setattr(
        "app.services.life_stream_daemon.life_stream_should_run", lambda: True
    )
    monkeypatch.setattr("app.ev.apps.discover_life_helper_path", lambda: "/tmp/ev-helper")
    monkeypatch.setattr("app.integrations.life_helper.run_life_helper", fake_helper)
    body = await _mac_hub_life_write("place_call", {"name": "Mom"})
    assert body is not None
    assert body.get("ok") is True
    assert seen_args.get("destination") == "+15550100"
    assert "mom" in str(body.get("spoken") or "").lower()
