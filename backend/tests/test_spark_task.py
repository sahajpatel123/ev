"""MiMo decides readout vs gist for any phrasing of a life task."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ev.spark_task import (
    LifeJob,
    TaskDecision,
    clear_life_job,
    decide_task,
    fallback_task_decision,
    remember_life_job,
)
from app.gateway.roles import DecisionAnswer
from app.memory.mail_speak import speak_mail, speak_received
from app.memory.recall import _spoken_from_evidence
from app.services.life_stream_daemon import LifeStreamDaemon
from tests.test_mail_speak import LONG_BODY, _mail


@pytest.fixture(autouse=True)
def _clear_life_job() -> None:
    clear_life_job()
    yield
    clear_life_job()


def _decide_with(monkeypatch, *, family, manner, focus="gist", latest=False, seen=None):
    from app.gateway.roles import DecisionAnswer

    async def fake_decide(state, questions, *, actor=None):
        if seen is not None:
            seen["state"] = state
            seen["questions"] = sorted(questions)
        return SimpleNamespace(
            status="ok",
            error=None,
            decision_answers={
                "family": DecisionAnswer(type="choice", choice=family),
                "manner": DecisionAnswer(type="choice", choice=manner),
                "focus": DecisionAnswer(type="choice", choice=focus),
                "latest": DecisionAnswer(type="choice", choice="true" if latest else "false"),
            },
        )

    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    monkeypatch.setattr("app.gateway.roles.decide_via_role", fake_decide)


def test_fallback_readout_is_structural_not_a_phrase_catalog() -> None:
    readout = [
        "Evie, do readout mails for me",
        "read that email out",
        "read the mail aloud",
        "read it to me line by line",
        "read me the last email",
        "recite the whole mail",
    ]
    summary = [
        "What are the new mails?",
        "what was the last mail",
        "any new mail",
        "what was the email from Alex about",
        "check my inbox",
        "summarise that mail",
        "what are my latest messages",
        "what did I talk with Mansi last",
    ]
    for phrase in readout:
        decision = fallback_task_decision(phrase, family_hint="mail")
        assert decision.manner == "readout", phrase
        assert decision.family == "mail"
    for phrase in summary:
        hint = "messages" if "message" in phrase.lower() or "talk with" in phrase.lower() else "mail"
        decision = fallback_task_decision(phrase, family_hint=hint)
        assert decision.manner != "readout", phrase


def test_readout_speaks_the_mail_line_by_line_but_still_caps() -> None:
    items = [
        _mail(
            "Lunch tomorrow",
            "Alex",
            preview="Can we move lunch to noon at the cafe? I'll book a table. " + LONG_BODY,
        )
    ]
    spoken = speak_mail(
        "read that email out",
        items,
        decision=TaskDecision(family="mail", manner="readout", latest=True, source="mimo"),
    )
    lowered = spoken.lower()
    assert spoken.lower().startswith("reading it out")
    assert "alex" in lowered
    assert "noon" in lowered or "cafe" in lowered
    assert LONG_BODY not in spoken
    assert "that's as far as i'll read" in lowered
    digest = speak_mail(
        "What are the new mails?",
        items,
        decision=TaskDecision(family="mail", manner="digest", source="mimo"),
    )
    assert digest.lower().startswith("recent mail:")
    assert "reading it out" not in digest.lower()
    assert LONG_BODY not in digest
    last = speak_mail(
        "what was the last mail",
        items,
        decision=TaskDecision(family="mail", manner="particular", latest=True, source="mimo"),
    )
    assert "reading it out" not in last.lower()
    assert "lunch" in last.lower() or "noon" in last.lower() or "alex" in last.lower()
    assert LONG_BODY not in last
    stamp = speak_received(items[0])
    assert stamp
    assert stamp.lower() in last.lower()


@pytest.mark.asyncio
async def test_mimo_layer_wakes_for_unseen_phrasing(monkeypatch: pytest.MonkeyPatch) -> None:
    """MiMo, not a regex, maps a novel wording onto readout."""

    _decide_with(monkeypatch, family="mail", manner="readout", focus="readout", latest=True)
    decision = await decide_task("walk me through that email", family_hint="mail")
    assert decision.source == "mimo"
    assert decision.manner == "readout"
    assert decision.family == "mail"


@pytest.mark.asyncio
async def test_mimo_layer_chooses_digest_for_whats_new(monkeypatch: pytest.MonkeyPatch) -> None:
    _decide_with(monkeypatch, family="mail", manner="digest", latest=False)
    decision = await decide_task("what showed up in the mailbox overnight", family_hint="mail")
    assert decision.family == "mail"
    assert decision.manner != "readout"
    assert decision.source == "mimo"
    assert decision.manner == "digest"


@pytest.mark.asyncio
async def test_mimo_down_falls_back_without_waking_a_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: False)
    decision = await decide_task("walk me through that email", family_hint="mail")
    assert decision.source == "fallback"
    assert decision.manner == "readout"


@pytest.mark.asyncio
async def test_mimo_cannot_promote_latest_to_readout(monkeypatch: pytest.MonkeyPatch) -> None:
    _decide_with(monkeypatch, family="messages", manner="readout", latest=True)
    decision = await decide_task("what are my latest messages", family_hint="messages")
    assert decision.manner != "readout"
    assert decision.source == "mimo"


def test_spoken_evidence_honors_readout_decision() -> None:
    from app.ev.spark_task import bind_decision, reset_decision

    daemon = LifeStreamDaemon(chat_db_path="/nonexistent/chat.db")
    hits = daemon.peek_mail(
        [
            _mail(
                "Flight change",
                "Airline",
                preview="Your Friday flight to Delhi now leaves at 9pm. Check in online.",
            )
        ]
    )
    token = bind_decision(TaskDecision(family="mail", manner="readout", latest=True, source="mimo"))
    try:
        spoken = _spoken_from_evidence(hits, "read that email out")
    finally:
        reset_decision(token)
    assert spoken.lower().startswith("reading it out")
    assert "delhi" in spoken.lower() or "9pm" in spoken.lower() or "friday" in spoken.lower()


def test_particular_mail_speaks_received_time_and_focus_when_is_the_clock() -> None:
    items = [
        _mail(
            "Flight change",
            "Airline",
            preview="Your Friday flight to Delhi now leaves at 9pm. Check in online.",
        )
    ]
    stamp = speak_received(items[0])
    assert stamp
    gist = speak_mail(
        "what was the last mail",
        items,
        decision=TaskDecision(
            family="mail", manner="particular", focus="gist", latest=True, source="mimo"
        ),
    )
    assert stamp.lower() in gist.lower()
    assert "airline" in gist.lower()
    assert LONG_BODY not in gist
    when = speak_mail(
        "when did I get this last mail",
        items,
        decision=TaskDecision(
            family="mail", manner="particular", focus="when", latest=True, source="mimo"
        ),
    )
    assert stamp.lower() in when.lower()
    assert when.lower().startswith("that mail from")
    assert "arrived" in when.lower()
    assert "delhi" not in when.lower()
    assert LONG_BODY not in when


@pytest.mark.asyncio
async def test_mimo_decides_last_mail_instead_of_skipping(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    async def fake_decide(state, questions, *, actor=None):
        called["n"] += 1
        return SimpleNamespace(
            status="ok",
            error=None,
            decision_answers={
                "family": DecisionAnswer(type="choice", choice="mail"),
                "manner": DecisionAnswer(type="choice", choice="particular"),
                "focus": DecisionAnswer(type="choice", choice="gist"),
                "latest": DecisionAnswer(type="choice", choice="true"),
            },
        )

    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    monkeypatch.setattr("app.gateway.roles.decide_via_role", fake_decide)
    decision = await decide_task("what was the last mail I got", family_hint="mail")
    assert called["n"] == 1
    assert decision.source == "mimo"
    assert decision.family == "mail"
    assert decision.manner == "particular"
    assert decision.latest is True
    assert decision.focus != "readout"


@pytest.mark.asyncio
async def test_mimo_maps_followup_when_without_a_phrase_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    _decide_with(
        monkeypatch, family="mail", manner="particular", focus="when", latest=True, seen=seen
    )

    remember_life_job(
        LifeJob(
            family="mail",
            tool="list_mail",
            query="what was the last mail I got",
            who="Airline",
            subject="Flight change",
            when="2026-09-05T10:00:00+00:00",
            gist="Friday flight to Delhi now leaves at 9pm.",
        )
    )
    for phrase in (
        "when did I get this last mail",
        "what time was that",
        "when did that arrive",
    ):
        decision = await decide_task(phrase, family_hint="mail")
        assert decision.source == "mimo", phrase
        assert decision.focus == "when", phrase
        assert decision.manner == "particular", phrase
        assert decision.latest is True, phrase
        assert decision.family == "mail", phrase
    assert "Flight change" in str((seen.get("state") or {}).get("context") or "")


def test_mimo_dark_followup_without_mail_word_stays_on_last_envelope() -> None:
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
    follow = fallback_task_decision("when did I get it", family_hint="mail")
    assert follow.family == "mail"
    assert follow.manner == "particular"
    assert follow.latest is True
    inbox = fallback_task_decision("check my inbox", family_hint="mail")
    assert inbox.manner == "digest"
