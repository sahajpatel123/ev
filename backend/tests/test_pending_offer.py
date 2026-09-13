"""The owner's 'yes' binds to Evie's own last offer (generic pending offer)."""

from __future__ import annotations

import pytest

from app.cognitive import intent as intent_mod
from app.cognitive.context import compile_context
from app.cognitive.session_store import CognitiveSession
from app.ev.continuity import classify_memory_intent, is_affirmative_reply, is_negative_reply


def test_short_yes_no_are_continuations() -> None:
    for phrase in ("yes", "yeah", "yep", "sure", "ok", "okay", "go ahead", "do it", "yes please"):
        assert is_affirmative_reply(phrase), phrase
        assert classify_memory_intent(phrase) == "continuation", phrase
    for phrase in ("no", "nope", "not now", "never mind", "cancel"):
        assert is_negative_reply(phrase), phrase
        assert classify_memory_intent(phrase) == "continuation", phrase


def test_statements_are_not_affirmatives() -> None:
    for phrase in (
        "yes the mail from Rahul was long",
        "no idea what the weather is",
        "okay so open Safari and search YouTube",
        "sure thing but first read the mail",
    ):
        assert not is_affirmative_reply(phrase), phrase
        assert not is_negative_reply(phrase), phrase


def test_pending_offer_round_trip() -> None:
    session = CognitiveSession(session_id="offer-test")
    intent_mod.set_pending_offer(session, "Do you want me to read out the full mail?")
    found = intent_mod.pending_offer(session)
    assert found is not None
    assert "read out the full mail" in found["text"]
    intent_mod.clear_pending_offer(session)
    assert intent_mod.pending_offer(session) is None


def test_flat_statement_is_not_an_offer() -> None:
    session = CognitiveSession(session_id="offer-flat")
    intent_mod.set_pending_offer(session, "I searched Mail for youtube.")
    assert intent_mod.pending_offer(session) is None


def test_expired_offer_does_not_bind() -> None:
    session = CognitiveSession(session_id="offer-expired")
    intent_mod.set_pending_offer(
        session, "Want me to read the full mail?", ttl_seconds=-1
    )
    assert intent_mod.pending_offer(session) is None


@pytest.mark.asyncio
async def test_offer_newer_than_parked_send_outranks_it(db_session) -> None:
    """A "yes" answers the newest question Evie asked, not an older queue.

    A parked WhatsApp send has its own yes handler that runs ahead of every
    other interpretation. When Evie has asked the owner something since that
    send was parked, the answer belongs to the newer question.
    """

    from app.cognitive.kernel import _offer_outranks_parked_send
    from app.cognitive.session_store import current, forget_live_cache
    from app.ev.messaging.approval import park_send

    forget_live_cache()
    parked = await park_send(
        db_session,
        to="John Smith",
        text="running late",
        display="John Smith",
        actor="voice",
    )

    offer = {
        "text": "Do you want me to read out the full mail?",
        "at": "2000-01-01T00:00:00+00:00",
    }
    older = await _offer_outranks_parked_send(
        db_session, offer=offer, device_id=None, live_session_id=None
    )
    assert older is False, "an older offer must not outrank a newer parked send"

    # The offer Evie has just asked about is newer than the parked send.
    intent_mod.set_pending_offer(current(), "Do you want me to read out the full mail?")
    live = intent_mod.pending_offer(current())
    assert live is not None and live.get("at"), "pending_offer must expose its timestamp"
    newer = await _offer_outranks_parked_send(
        db_session, offer=live, device_id=None, live_session_id=None
    )
    assert newer is True, "the newest question must win"
    assert parked is not None


@pytest.mark.asyncio
async def test_laughter_never_approves_a_parked_send(db_session) -> None:
    """Laughter is not consent, and neither is asking Evie to read something."""

    from app.ev.messaging.approval import handle_send_approval, is_affirmative, park_send

    await park_send(
        db_session,
        to="John Smith",
        text="running late",
        display="John Smith",
        actor="voice",
    )
    for phrase in ("ha ha", "ha ha ha", "ok read the mail", "send the whole thing"):
        assert not is_affirmative(phrase), phrase
        assert await handle_send_approval(db_session, phrase) is None, phrase


def test_context_renders_pending_offer() -> None:
    session = CognitiveSession(session_id="offer-context")
    intent_mod.set_pending_offer(session, "Do you want me to read out the full mail?")
    text = compile_context(
        transcript="yes",
        modality="voice",
        device_id=None,
        cognition=session,
        capability_names=["life.mail"],
    )
    assert "PENDING OFFER" in text
    assert "read out the full mail" in text
    assert "carry out exactly that offer" in text


# --- the owner's report, end to end at the kernel boundary -------------------
#
# "search for x in mail" -> Evie found it and asked whether to read it out ->
# the owner said "yes" -> Evie answered with a topic-free greeting. These pin
# the three properties whose loss reproduces that: the offer is armed with the
# action that produced it, a bare "yes" binds to it without destroying the
# referent, and the exchange survives in the durable ledger.

MAIL_OFFER = (
    "The newest mail is from GitHub about a failed CI run on main, "
    "arrived Friday at 2:21 am. Do you want me to read out the full mail?"
)


def test_mail_offer_is_armed_with_its_action() -> None:
    session = CognitiveSession(session_id="owner-report-arm")
    intent_mod.set_pending_offer(
        session,
        MAIL_OFFER,
        action={"tool": "life.mail", "args": {"query": "search for x in mail"}},
    )
    armed = intent_mod.pending_offer(session)
    assert armed is not None
    assert armed["action"]["tool"] == "life.mail"
    assert armed["readout"] is True
    assert intent_mod.continuation_readout("yes", session) is True


def test_long_mail_summary_still_arms_the_offer() -> None:
    """A paragraph-long summary used to be discarded by a 600-char cap."""

    session = CognitiveSession(session_id="owner-report-long")
    long_reply = (
        "The newest mail is from GitHub about a failed CI run on main. " * 12
        + "Do you want me to read out the full mail?"
    )
    assert len(long_reply) > 600
    intent_mod.set_pending_offer(session, long_reply)
    assert intent_mod.pending_offer(session) is not None


def test_greeting_does_not_replace_the_offer() -> None:
    from app.cognitive.kernel import KernelResult, _record_turn
    from app.cognitive.session_store import current, forget_live_cache

    forget_live_cache()
    _record_turn(
        transcript="search for x in mail",
        result=KernelResult(
            spoken=MAIL_OFFER,
            kind="muse",
            last_tool="life.mail",
            last_tool_args={"query": "search for x in mail"},
        ),
    )
    armed = intent_mod.pending_offer(current())
    assert armed is not None and armed["text"] == MAIL_OFFER

    # The Mac client greets on every live open.
    _record_turn(
        transcript="Hi.",
        result=KernelResult(spoken="Hey there. How can I help you?", kind="muse"),
    )
    still = intent_mod.pending_offer(current())
    assert still is not None, "a greeting displaced the owner's unanswered offer"
    assert still["text"] == MAIL_OFFER

    # And the "yes" still answers the mail question.
    assert intent_mod.continuation_readout("yes") is True
    _record_turn(
        transcript="yes",
        result=KernelResult(spoken="It reads: Register for Grok Bot Galaxy.", kind="muse"),
    )
    assert intent_mod.pending_offer(current()) is None, "answered offer was not cleared"

    rows = intent_mod.recent_exchanges(current())
    assert [row["owner"] for row in rows] == ["search for x in mail", "Hi.", "yes"]
    assert "read out the full mail" in rows[0]["evie"]
    assert "Register for Grok Bot Galaxy" in rows[-1]["evie"]


@pytest.mark.asyncio
async def test_a_crashed_turn_still_speaks_and_keeps_context(monkeypatch, tmp_path) -> None:
    """A raise must not reach the owner as silence."""

    from app.cognitive import kernel
    from app.cognitive.session_store import current, reset_for_tests
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    reset_for_tests()

    async def _boom(**_kwargs):
        raise RuntimeError("provider blew up")

    monkeypatch.setattr(kernel, "_handle_turn", _boom)

    result = await kernel.handle_turn(transcript="search for x in mail", modality="voice")
    assert result.kind == "failed"
    assert result.spoken and "failed" in result.spoken.lower()

    rows = intent_mod.recent_exchanges(current())
    assert [row["owner"] for row in rows] == ["search for x in mail"]
    reset_for_tests()


@pytest.mark.asyncio
async def test_yes_turn_prompt_carries_the_referent(monkeypatch, tmp_path) -> None:
    """The decisive one: what the model actually receives on the "yes" turn.

    The kernel prompt is `[system, user]` with no chat history, so if the offer
    and the ledger are not rendered into the system message the model sees a
    bare "yes" and answers with a topic-free greeting — the owner's report.
    """

    from app.cognitive import kernel
    from app.cognitive.session_store import current, reset_for_tests
    from app.config import settings
    from app.gateway.providers import ChatResult

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    reset_for_tests()

    seen: list[str] = []

    class ScriptedMuse:
        def __init__(self) -> None:
            self.replies = [
                ChatResult(text=MAIL_OFFER),
                ChatResult(text="It reads: Register for Grok Bot Galaxy, Sept 15-17."),
            ]

        async def chat_with_tools(self, messages, specs, **kwargs):
            del specs, kwargs
            seen.append("\n".join(str(getattr(m, "content", "")) for m in messages))
            return self.replies.pop(0)

    muse = ScriptedMuse()
    monkeypatch.setattr(kernel, "muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: muse)

    first = await kernel.handle_turn(transcript="search for x in mail", modality="voice")
    assert first.kind == "muse"
    assert first.spoken == MAIL_OFFER
    assert intent_mod.pending_offer(current()) is not None

    second = await kernel.handle_turn(transcript="yes", modality="voice")
    assert second.spoken.startswith("It reads:")

    prompt = seen[-1]
    assert MAIL_OFFER in prompt, "the model never saw the question it was answering"
    assert "search for x in mail" in prompt, "the model never saw the mail request"
    assert "RECENT EXCHANGES" in prompt
    assert "PENDING OFFER" in prompt
    reset_for_tests()


def test_durable_state_is_private_and_credential_free(tmp_path, monkeypatch) -> None:
    """The ledger outlives the input filter, so it must not park a secret."""

    import stat as _stat

    from app.cognitive.session_store import _path, save
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    session = CognitiveSession(session_id="privacy")
    intent_mod.set_pending_offer(
        session,
        "Should I send it with sk-abcdefghijklmnopqrstuvwxyz0123456789?",
    )
    intent_mod.remember_exchange(
        session,
        owner="my token is ghp_" + "a" * 36,
        assistant="Understood.",
    )
    save(session)

    path = _path()
    assert _stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    body = path.read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in body
    assert "ghp_" + "a" * 36 not in body
