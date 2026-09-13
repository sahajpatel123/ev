"""A bare "yes" is small talk only when it answers nothing.

``is_casual_social_turn`` recognised "ok"/"yeah"/"yep" but not the plain
"yes", so the same conversational move was small talk in one surface and a
substantive turn in another. And when Evie has asked a question — "Do you want
me to read out the full mail?" — the owner's "yes" carries a decision: it must
not be dropped as casual small talk (the quiet memory branch) or the offer is
lost. With no offer armed, the small-talk classification is unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.cognitive import session_store
from app.cognitive.intent import set_pending_offer
from app.context.compiler import ContextCompiler, is_casual_social_turn

OFFER = "Do you want me to read out the full mail?"


@pytest.fixture(autouse=True)
def _clean_session():
    session_store.reset_for_tests()
    yield
    session_store.reset_for_tests()


def test_yes_and_yeah_are_the_same_small_talk() -> None:
    for word in ("yes", "yeah", "yep", "ok", "okay"):
        assert is_casual_social_turn(word) is True, word
    assert is_casual_social_turn("Yes.") is True
    assert is_casual_social_turn("yes, thanks!") is True
    assert is_casual_social_turn("ok, got it.") is True


def test_bare_yes_answering_a_live_offer_is_not_small_talk() -> None:
    set_pending_offer(session_store.current(), OFFER)
    assert session_store.current().constraints.get("pending_offer") is not None

    for reply in ("yes", "yes, please", "yeah", "yep", "ok", "no", "nope"):
        assert is_casual_social_turn(reply) is False, reply

    # A greeting is still a greeting while Evie waits for the answer.
    assert is_casual_social_turn("hey") is True
    assert is_casual_social_turn("how are you?") is True


def test_small_talk_without_an_offer_is_unchanged() -> None:
    assert session_store.current().constraints.get("pending_offer") is None

    assert is_casual_social_turn("ok") is True
    assert is_casual_social_turn("yeah") is True
    assert is_casual_social_turn("hey") is True
    assert is_casual_social_turn("thanks!") is True
    # Real questions were never small talk, offer or not.
    assert is_casual_social_turn("Did I get any email from Alex?") is False


def test_unreadable_session_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom():
        raise RuntimeError("durable session unavailable")

    monkeypatch.setattr("app.cognitive.session_store.current", _boom)

    assert is_casual_social_turn("yes") is False
    assert is_casual_social_turn("hey") is True


def test_answering_yes_is_not_quiet_in_the_compiled_window() -> None:
    """The consumer: a "yes" answering Evie must not take the quiet memory drop."""

    compiler = ContextCompiler()
    memories = [
        SimpleNamespace(text="Decided to use PostgreSQL for event storage", memory_type="decision"),
    ]
    user_state = SimpleNamespace(
        activity="coding",
        active_project="ev",
        active_goal=None,
        current_task="reading mail",
        recent_topics=[],
        open_decisions=[],
        live_context=[],
    )

    def _plan(message: str):
        return compiler.compile_progressive(
            memories=memories,
            user_state=user_state,
            strategy_text="STRATEGY: concise",
            budget=10000,
            message=message,
        )

    # No offer: "yes" and "yeah" both read as small talk and drop memories.
    for word in ("yes", "yeah"):
        plan = _plan(word)
        assert plan.metadata["quiet"] is True, word
        assert _included(plan) == 0, word

    set_pending_offer(session_store.current(), OFFER)

    plan = _plan("yes")
    assert plan.metadata["quiet"] is False
    assert _included(plan) > 0


def _included(plan) -> int:
    section = next((s for s in plan.sections if s.name == "retrieved_memory"), None)
    assert section is not None
    return section.items_included
