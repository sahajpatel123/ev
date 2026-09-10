"""Muse Spark 1.3 kernel can reach Mac Envelope Index / iMessage via life tools."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cognitive.capabilities import tool_specs
from app.cognitive.context import compile_context
from app.cognitive.executor import execute_semantic
from app.cognitive.session_store import current
from app.ev.spark_task import TaskDecision
from app.memory.mail_speak import speak_mail, speak_received


def test_spark_kernel_lists_mac_life_mail_and_messages() -> None:
    names = {spec.name for spec in tool_specs()}
    assert "life.mail" in names
    assert "life.messages" in names


def test_spark_context_tells_contributor_to_use_life_mail() -> None:
    text = compile_context(
        transcript="when did I get it",
        modality="voice",
        device_id="mac",
        cognition=current(),
    )
    assert "life.mail" in text
    assert "life.messages" in text
    assert "when they ask the time" in text.lower() or "when it arrived" in text.lower()


@pytest.mark.asyncio
async def test_life_mail_runs_list_mail_not_gmail(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    seen: dict[str, object] = {}

    async def fake_run(session, name, arguments, **kwargs):
        del session
        seen["name"] = name
        seen["arguments"] = arguments
        seen["kind"] = kwargs.get("kind")
        return {
            "ok": True,
            "spoken": "Mail from Airline, Friday at 10:00 am: Flight change.",
            "messages": [
                {
                    "sender": "Airline",
                    "subject": "Flight change",
                    "when": "2026-09-05T10:00:00+00:00",
                }
            ],
        }

    monkeypatch.setattr("app.cognitive.executor._run_existing", fake_run)
    body = await execute_semantic(
        db_session,
        "life.mail",
        {"query": "when did I get this last mail"},
        cognition=current(),
        actor="master",
        live_session_id=None,
        steering_seen=int(current().steering_version),
    )
    assert seen.get("name") == "list_mail"
    assert seen.get("kind") == "life.mail"
    assert "when did I get this last mail" in str(seen.get("arguments") or "")
    assert body.get("ok") is True


def test_when_focus_still_speaks_envelope_time() -> None:
    item = {
        "subject": "Flight change",
        "sender": "Airline",
        "received": "2026-09-05T10:00:00+00:00",
        "preview": "Your Friday flight to Delhi now leaves at 9pm.",
        "memory_type": "mail.envelope.received",
    }
    stamp = speak_received(item)
    spoken = speak_mail(
        "when did I get it",
        [item],
        decision=TaskDecision(
            family="mail", manner="particular", focus="when", latest=True, source="spark"
        ),
    )
    assert stamp
    assert stamp.lower() in spoken.lower()
    assert "arrived" in spoken.lower()
