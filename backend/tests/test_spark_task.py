"""Muse Spark 1.3 decides readout vs gist for any phrasing of a life task."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.contracts import ChatResult
from app.ev.spark_task import TaskDecision, decide_task, fallback_task_decision
from app.memory.mail_speak import speak_mail
from app.memory.recall import _spoken_from_evidence
from app.services.life_stream_daemon import LifeStreamDaemon

from tests.test_mail_speak import LONG_BODY, _mail


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
        decision=TaskDecision(family="mail", manner="readout", latest=True, source="spark"),
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
        decision=TaskDecision(family="mail", manner="digest", source="spark"),
    )
    assert digest.lower().startswith("recent mail:")
    assert "reading it out" not in digest.lower()
    assert LONG_BODY not in digest
    last = speak_mail(
        "what was the last mail",
        items,
        decision=TaskDecision(family="mail", manner="particular", latest=True, source="spark"),
    )
    assert "reading it out" not in last.lower()
    assert "lunch" in last.lower() or "noon" in last.lower() or "alex" in last.lower()
    assert LONG_BODY not in last


@pytest.mark.asyncio
async def test_spark_layer_wakes_for_unseen_phrasing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spark, not a regex, maps a novel wording onto readout."""

    async def fake_structured(*_args, **_kwargs):
        return ChatResult(
            text='{"family":"mail","manner":"readout","who":"","about":"","latest":true}',
            model="muse-spark-1.3-contributor",
        )

    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr(
        "app.gateway.muse_spark.muse_spark_provider",
        lambda: SimpleNamespace(chat_structured=fake_structured),
    )
    decision = await decide_task("walk me through that email", family_hint="mail")
    assert decision.source == "spark"
    assert decision.manner == "readout"
    assert decision.family == "mail"


@pytest.mark.asyncio
async def test_spark_layer_chooses_digest_for_whats_new(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_structured(*_args, **_kwargs):
        return ChatResult(
            text='{"family":"mail","manner":"digest","who":"","about":"","latest":false}',
            model="muse-spark-1.3-contributor",
        )

    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr(
        "app.gateway.muse_spark.muse_spark_provider",
        lambda: SimpleNamespace(chat_structured=fake_structured),
    )
    decision = await decide_task("what showed up in the mailbox overnight", family_hint="mail")
    assert decision.source == "spark"
    assert decision.manner == "digest"


@pytest.mark.asyncio
async def test_spark_down_falls_back_without_waking_a_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: False)
    decision = await decide_task("walk me through that email", family_hint="mail")
    assert decision.source == "fallback"
    assert decision.manner == "readout"


@pytest.mark.asyncio
async def test_spark_cannot_promote_latest_to_readout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_structured(*_args, **_kwargs):
        return ChatResult(
            text='{"family":"messages","manner":"readout","who":"","about":"","latest":true}',
            model="muse-spark-1.3-contributor",
        )

    monkeypatch.setattr("app.gateway.muse.muse_intelligence_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr(
        "app.gateway.muse_spark.muse_spark_provider",
        lambda: SimpleNamespace(chat_structured=fake_structured),
    )
    decision = await decide_task("what are my latest messages", family_hint="messages")
    assert decision.source == "spark"
    assert decision.manner != "readout"


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
    token = bind_decision(TaskDecision(family="mail", manner="readout", latest=True, source="spark"))
    try:
        spoken = _spoken_from_evidence(hits, "read that email out")
    finally:
        reset_decision(token)
    assert spoken.lower().startswith("reading it out")
    assert "delhi" in spoken.lower() or "9pm" in spoken.lower() or "friday" in spoken.lower()
