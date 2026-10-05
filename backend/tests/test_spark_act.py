"""MiMo judges work turns Gemini would otherwise improvise."""

from __future__ import annotations

import pytest

from app.ev.spark_act import (
    ActDecision,
    decide_owner_act,
    fallback_act,
    live_tool_for_act,
    maybe_spark_act_utterance,
)
from app.ev.spark_task import LifeJob, clear_life_job, remember_life_job


@pytest.fixture(autouse=True)
def _clear_life_job() -> None:
    clear_life_job()
    yield
    clear_life_job()


def test_greetings_stay_on_mini() -> None:
    assert maybe_spark_act_utterance("hey") is False
    assert maybe_spark_act_utterance("how are you") is False
    assert fallback_act("thanks") is None
    assert maybe_spark_act_utterance(
        "I was thinking about what you said yesterday about the movie"
    ) is False
    assert maybe_spark_act_utterance(
        "can you check that thing we talked about online"
    ) is True


def test_fallback_code_and_search_do_not_wait_on_mimo() -> None:
    assert fallback_act("create a calculator app UI").act == "code"
    assert fallback_act("write a python script that prints hello").act == "code"
    assert fallback_act("what's the weather in mumbai").act == "search"
    tool = live_tool_for_act(
        "create a calculator app UI", fallback_act("create a calculator app UI")
    )
    assert tool is not None
    assert tool[0] == "code"


@pytest.mark.asyncio
async def test_obvious_code_does_not_call_mimo(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    async def boom(*_args, **_kwargs):
        called["n"] += 1
        raise AssertionError("mimo should not run on obvious code")

    monkeypatch.setattr("app.ev.spark_act._mimo_decide", boom)
    decision = await decide_owner_act("create a calculator app UI")
    assert decision is not None
    assert decision.act == "code"
    assert decision.source == "fallback"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_mimo_pokes_ambiguous_work(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_mimo(_utterance: str) -> str:
        return "search"

    monkeypatch.setattr("app.ev.spark_act._mimo_decide", fake_mimo)
    monkeypatch.setattr("app.ev.spark_act.fallback_act", lambda _text: None)
    monkeypatch.setattr("app.ev.spark_act.maybe_spark_act_utterance", lambda _text: True)
    decision = await decide_owner_act("can you check that thing we talked about online")
    assert decision is not None
    assert decision.act == "search"
    assert decision.source == "mimo"


def test_live_tool_never_selects_execute_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ev.spark_act import ActDecision

    monkeypatch.setattr(
        "app.ev.tool_select.resolve_live_action",
        lambda _text: ("execute_command", {"command": "ls"}),
    )
    tool = live_tool_for_act("run ls on the laptop", ActDecision(act="life", source="mimo"))
    assert tool is None or tool[0] != "execute_command"
    chat = live_tool_for_act("hey", ActDecision(act="chat", source="mimo"))
    assert chat is None
    weather = live_tool_for_act(
        "what's the weather in mumbai", ActDecision(act="search", source="fallback")
    )
    assert weather is not None
    assert weather[0] == "get_weather"
    assert "mumbai" in str(weather[1].get("query") or "").lower()


def test_correspondence_desk_is_recall_not_mini_chat() -> None:
    hanging = "Who am I leaving hanging?"
    pickup = "Where did we leave it with Ada?"
    assert maybe_spark_act_utterance(hanging) is True
    assert maybe_spark_act_utterance(pickup) is True
    assert fallback_act(hanging).act == "recall"
    assert fallback_act(pickup).act == "recall"
    hang_tool = live_tool_for_act(hanging, fallback_act(hanging))
    assert hang_tool is not None
    assert hang_tool[0] == "recall"
    pick_tool = live_tool_for_act(pickup, fallback_act(pickup))
    assert pick_tool is not None
    assert pick_tool[0] == "recall"
    send = fallback_act("Text Mom I'm late")
    assert send is not None
    assert send.act == "life"


@pytest.mark.asyncio
async def test_mimo_pokes_recall_work_and_cannot_demote_to_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_mimo(_utterance: str) -> str:
        return "recall"

    monkeypatch.setattr("app.ev.spark_act._mimo_decide", fake_mimo)
    decision = await decide_owner_act("Who am I leaving hanging?")
    assert decision is not None
    assert decision.act == "recall"
    assert decision.source == "mimo"

    async def chatty(_utterance: str) -> str:
        return "chat"

    monkeypatch.setattr("app.ev.spark_act._mimo_decide", chatty)
    kept = await decide_owner_act("Who am I leaving hanging?")
    assert kept is not None
    assert kept.act == "recall"
    assert kept.source == "fallback"


@pytest.mark.asyncio
async def test_mimo_act_asks_brain_with_finite_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app.gateway.roles import DecisionAnswer

    seen: dict[str, object] = {}

    async def fake_decide(state, questions, *, actor=None):
        seen["actor"] = actor
        seen["questions"] = sorted(questions)
        return SimpleNamespace(
            status="ok",
            error=None,
            decision_answers={"act": DecisionAnswer(type="choice", choice="recall")},
        )

    # decide_via_role is imported inside _mimo_decide; patch the roles source.
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    monkeypatch.setattr("app.gateway.roles.decide_via_role", fake_decide)
    from app.ev.spark_act import _mimo_decide

    act = await _mimo_decide("Who am I leaving hanging?")
    assert act == "recall"
    assert seen.get("actor") == "spark_act"
    assert seen.get("questions") == ["act"]


@pytest.mark.asyncio
async def test_mimo_pokes_hold_look(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holding = (
        "Look at the thing I'm holding in my hand. I want you to look at it "
        "and tell me more info about this item I'm holding"
    )

    async def fake_mimo(_utterance: str) -> str:
        return "look"

    monkeypatch.setattr("app.ev.spark_act._mimo_decide", fake_mimo)
    decision = await decide_owner_act(holding)
    assert decision is not None
    assert decision.act == "look"
    assert decision.source == "mimo"

    async def dark(_utterance: str) -> str | None:
        return None

    monkeypatch.setattr("app.ev.spark_act._mimo_decide", dark)
    fallback = await decide_owner_act(holding)
    assert fallback is not None
    assert fallback.act == "look"
    assert fallback.source == "fallback"


def test_propose_contents_list_is_a_file_job_not_chat() -> None:
    phrase = (
        "I have a flight tomorrow, I want you to create a list of items you think is necessary "
        "and make a list and save that text document inside my desktop"
    )
    decision = fallback_act(phrase)
    assert decision is not None
    assert decision.act in {"files", "desk", "computer"}
    tool = live_tool_for_act(phrase, decision)
    assert tool is not None
    assert tool[0] == "computer"


def test_followup_without_mail_word_stays_on_last_life_job() -> None:
    remember_life_job(
        LifeJob(
            family="mail",
            tool="list_mail",
            query="what was the last mail I got",
            who="Airline",
            subject="Flight change",
            when="2026-09-05T10:00:00+00:00",
        )
    )
    assert maybe_spark_act_utterance("when did I get it") is True
    assert maybe_spark_act_utterance("what time was that") is True
    tool = live_tool_for_act(
        "when did I get it", ActDecision(act="life", source="mimo")
    )
    assert tool is not None
    assert tool[0] == "list_mail"
    assert "when did I get it" in str(tool[1].get("query") or "")
    assert maybe_spark_act_utterance("how are you") is False


@pytest.mark.asyncio
async def test_mimo_gets_the_hand_on_last_mail_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remember_life_job(
        LifeJob(
            family="mail",
            tool="list_mail",
            query="what was the last mail I got",
            who="Airline",
            when="2026-09-05T10:00:00+00:00",
        )
    )

    async def fake_mimo(_utterance: str) -> str:
        return "life"

    monkeypatch.setattr("app.ev.spark_act._mimo_decide", fake_mimo)
    decision = await decide_owner_act("when did I get it")
    assert decision is not None
    assert decision.act == "life"
    assert decision.source == "mimo"
    tool = live_tool_for_act("when did I get it", decision)
    assert tool is not None
    assert tool[0] == "list_mail"


@pytest.mark.asyncio
async def test_recent_messages_poke_mimo_not_obvious_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"n": 0}

    async def fake_mimo(utterance: str) -> str:
        called["n"] += 1
        assert "recent messages" in utterance.lower()
        return "life"

    monkeypatch.setattr("app.ev.spark_act._mimo_decide", fake_mimo)
    decision = await decide_owner_act("what are my recent messages")
    assert called["n"] == 1
    assert decision is not None
    assert decision.act == "life"
    assert decision.source == "mimo"
    tool = live_tool_for_act("what are my recent messages", decision)
    assert tool is not None
    assert tool[0] == "list_messages"


def test_file_find_and_in_app_are_not_web_search() -> None:
    find = "find my w2 file from laptop"
    assert fallback_act(find) is not None
    assert fallback_act(find).act == "files"
    find_tool = live_tool_for_act(find, fallback_act(find))
    assert find_tool is not None and find_tool[0] == "computer"

    play = "play some song randomly from Chill in Music"
    assert fallback_act(play) is not None
    assert fallback_act(play).act == "computer"
    play_tool = live_tool_for_act(play, fallback_act(play))
    assert play_tool is not None and play_tool[0] == "open_in_app"
    assert play_tool[1].get("random") is True

    opened = "open YouTube from Safari"
    assert fallback_act(opened).act == "computer"
    open_tool = live_tool_for_act(opened, fallback_act(opened))
    assert open_tool is not None and open_tool[0] == "open_in_app"
    assert str(open_tool[1].get("app") or "") == "Safari"


@pytest.mark.asyncio
async def test_mimo_cannot_demote_file_find_to_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_mimo(_utterance: str) -> str:
        return "search"

    monkeypatch.setattr("app.ev.spark_act._mimo_decide", fake_mimo)
    decision = await decide_owner_act("find my resume file on my laptop")
    assert decision is not None
    assert decision.act == "files"
    tool = live_tool_for_act("find my resume file on my laptop", decision)
    assert tool is not None and tool[0] == "computer"
