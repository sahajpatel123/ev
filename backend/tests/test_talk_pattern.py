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
