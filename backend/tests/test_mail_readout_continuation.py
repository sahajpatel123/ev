"""A bare "yes" that answers Evie's read-aloud offer must reach the readout.

Owner-observed failure: Evie found the mail, asked "do you want me to read out
the full mail?", the owner said "yes", and got "yes I am here what would you
like me to do?". The reply word carries no read-aloud cue of its own, so the
live offer has to choose the manner; without it the turn is digested and the
body the owner asked for is never spoken.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.cognitive import intent, session_store
from app.ev import spark_task
from app.ev import tools as ev_tools
from app.memory import live_life, mail_speak
from app.services import life_stream_daemon
from app.services.life_stream_daemon import LifeStreamDaemon

OFFER = "Do you want me to read out the full mail from Rahul?"

# The marker sits past the ~220-char gist cap, so it can only be spoken when the
# body itself is read out.
MAIL_BODY = (
    "Please review the attached invoice for March and confirm the wire by Friday. "
    "The amount is forty two thousand rupees. "
    "Annexure two lists the servers and the support window for the next quarter. "
    "The signature block names the SWIFT code to use for the transfer."
)
BODY_MARKER = "swift code"

CHAT_HIT: dict[str, Any] = {
    "when": "2026-09-12T09:05:00+00:00",
    "text": "Mansi: are we still on for lunch tomorrow at the cafe?",
    "handle": "Mansi",
    "preview": "are we still on for lunch tomorrow at the cafe?",
    "kind": "live_mac",
    "memory_type": "message.imessage.received",
    "channel": "imessage",
    "shelf": "chats",
}


def _daemon() -> LifeStreamDaemon:
    return LifeStreamDaemon(chat_db_path="/nonexistent/chat.db", mail_index_path="")


def _mail_hits() -> list[dict[str, Any]]:
    """Canonical Envelope Index hits, built without reading the owner's Mail."""

    return _daemon().peek_mail(
        [
            {
                "subject": "March invoice",
                "sender": "Rahul Sharma",
                "received": "2026-09-12T09:00:00+00:00",
                "snippet": MAIL_BODY,
                "rowid": 42,
            }
        ],
        query="any new email",
        limit=8,
    )


def _mail_hit(subject: str, sender: str, snippet: str) -> dict[str, Any]:
    return _daemon().peek_mail(
        [
            {
                "subject": subject,
                "sender": sender,
                "received": "2026-09-12T09:00:00+00:00",
                "snippet": snippet,
                "rowid": 43,
            }
        ],
        query="any new email",
        limit=8,
    )[0]


def _stub_hub(
    monkeypatch: pytest.MonkeyPatch,
    hits: list[dict[str, Any]],
    *,
    decision: spark_task.TaskDecision | None = None,
) -> None:
    """Point the life-read dispatch at in-memory Mac hits, never Apple data."""

    async def _decide(utterance: str, *, family_hint: str = "") -> spark_task.TaskDecision:
        if decision is not None:
            return decision
        # Spark-dark default: the reply word alone lands as an inbox digest.
        return spark_task.TaskDecision(
            family=family_hint or "other", manner="digest", focus="gist", source="test"
        )

    async def _no_account(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(life_stream_daemon, "life_stream_should_run", lambda: True)
    monkeypatch.setattr(life_stream_daemon, "get_life_stream_daemon", _daemon)
    monkeypatch.setattr(
        live_life, "peek_mac_life", lambda *args, **kwargs: [dict(hit) for hit in hits]
    )
    monkeypatch.setattr(live_life, "peek_account_life", _no_account)
    monkeypatch.setattr(spark_task, "decide_task", _decide)


@pytest.fixture(autouse=True)
def _own_session():
    session_store.reset_for_tests()
    yield
    session_store.reset_for_tests()


@pytest.fixture
def body_fetches(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every ask-time body fetch while still doing the real one."""

    seen: list[dict[str, Any]] = []
    real = mail_speak.fill_readout

    def spy(item: dict[str, Any], *, mail_index_path: str = "") -> None:
        seen.append(dict(item))
        real(item, mail_index_path=mail_index_path)

    monkeypatch.setattr(mail_speak, "fill_readout", spy)
    return seen


async def test_bare_yes_answers_read_aloud_offer(
    monkeypatch: pytest.MonkeyPatch, body_fetches: list[dict[str, Any]]
) -> None:
    _stub_hub(monkeypatch, _mail_hits())
    intent.set_pending_offer(session_store.current(), OFFER)

    out = await ev_tools._mac_hub_life_read("list_mail", {"query": "yes"})

    assert out is not None
    assert out["task_decision"]["manner"] == "readout"
    assert body_fetches, "the offered body was never fetched"
    spoken = str(out["spoken"]).lower()
    assert spoken.startswith("reading it out")
    assert BODY_MARKER in spoken


async def test_yes_without_read_aloud_offer_stays_a_gist(
    monkeypatch: pytest.MonkeyPatch, body_fetches: list[dict[str, Any]]
) -> None:
    _stub_hub(monkeypatch, _mail_hits())

    out = await ev_tools._mac_hub_life_read("list_mail", {"query": "yes"})

    assert out is not None
    assert out["task_decision"]["manner"] == "digest"
    assert body_fetches == []
    assert BODY_MARKER not in str(out["spoken"]).lower()


async def test_plain_question_does_not_read_the_body_out(
    monkeypatch: pytest.MonkeyPatch, body_fetches: list[dict[str, Any]]
) -> None:
    _stub_hub(monkeypatch, _mail_hits())
    intent.set_pending_offer(session_store.current(), OFFER)

    out = await ev_tools._mac_hub_life_read("list_mail", {"query": "what's the weather"})

    assert out is not None
    assert out["task_decision"]["manner"] == "digest"
    assert body_fetches == []
    assert BODY_MARKER not in str(out["spoken"]).lower()


async def test_bare_yes_reads_a_chat_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_hub(monkeypatch, [dict(CHAT_HIT)])
    intent.set_pending_offer(session_store.current(), OFFER)

    out = await ev_tools._mac_hub_life_read("list_messages", {"query": "yes"})

    assert out is not None
    assert out["task_decision"]["manner"] == "readout"
    assert str(out["spoken"]).lower().startswith("reading it out")
    assert "lunch tomorrow" in str(out["spoken"]).lower()
    assert out["messages"][0]["text"].startswith("Mansi:")


async def test_readout_promotion_never_re_filters_a_digest(
    monkeypatch: pytest.MonkeyPatch, body_fetches: list[dict[str, Any]]
) -> None:
    """A misfired "who" must not wipe the live digest the owner is hearing."""

    invoice = _mail_hits()[0]
    github = _mail_hit("Failed CI run on main", "GitHub", "The pipeline failed on main again.")
    _stub_hub(
        monkeypatch,
        [invoice],
        decision=spark_task.TaskDecision(
            family="mail", manner="digest", focus="gist", who="GitHub", source="test"
        ),
    )

    def peek(ask: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [dict(github)] if "github" in (kwargs.get("tokens") or []) else [dict(invoice)]

    monkeypatch.setattr(live_life, "peek_mac_life", peek)
    intent.set_pending_offer(session_store.current(), OFFER)

    out = await ev_tools._mac_hub_life_read("list_mail", {"query": "yes"})

    assert out is not None
    assert out["task_decision"]["manner"] == "readout"
    assert body_fetches, "the offered body was never fetched"
    spoken = str(out["spoken"]).lower()
    assert "march invoice" in spoken
    assert "github" not in spoken
