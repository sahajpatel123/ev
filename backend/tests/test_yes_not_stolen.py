"""A bare "yes" with a live offer answers Evie; it is not a send body or a goal.

The owner's report: Evie found the mail, asked whether to read it out, the
owner said "yes", and the affirmative was consumed elsewhere — as the body of a
waiting send, or as a re-arm of the previous Mac goal. The live offer owns the
referent, so those consumers must stand down. With no offer armed nothing
changes.
"""

from __future__ import annotations

import pytest

from app.cognitive import session_store
from app.cognitive.intent import set_pending_offer
from app.ev.computer_runtime import ensure_state, note_goal, reset_computer_states
from app.ev.send_intent import looks_like_message_body

OFFER = "Do you want me to read out the full mail?"
GOAL = "Open Safari and open YouTube in it."


@pytest.fixture(autouse=True)
def _clean_state():
    session_store.reset_for_tests()
    reset_computer_states()
    yield
    session_store.reset_for_tests()
    reset_computer_states()


def _arm_offer() -> None:
    set_pending_offer(session_store.current(), OFFER)
    assert session_store.current().constraints.get("pending_offer") is not None


def test_yes_with_live_offer_is_not_a_message_body() -> None:
    assert looks_like_message_body("yes") is True

    _arm_offer()
    for reply in ("yes", "yes, please", "yeah", "sure", "no", "nope"):
        assert looks_like_message_body(reply) is False, reply

    # Only a reply is an answer. A real body or a statement that merely opens
    # with "yes" keeps filling the waiting send.
    assert looks_like_message_body("running late, see you at six") is True
    assert looks_like_message_body("yes the mail from Rahul was long") is True


def test_yes_without_offer_is_still_a_message_body() -> None:
    assert looks_like_message_body("yes") is True
    assert looks_like_message_body("ok") is True


def test_yes_with_live_offer_is_not_a_goal_continuation() -> None:
    state = ensure_state("yes-live-offer")
    note_goal(state, GOAL)
    assert state.goal is not None
    goal = state.goal
    traces = list(state.traces)

    _arm_offer()
    note_goal(state, "yes")

    assert state.goal is goal
    assert state.pending_goal == GOAL
    assert state.original_owner_request == GOAL
    assert state.traces == traces

    # A negative reply is the same answer, not a fresh Mac goal either.
    note_goal(state, "no")
    assert state.goal is goal
    assert state.pending_goal == GOAL
    assert state.traces == traces


def test_yes_without_offer_still_continues_the_goal() -> None:
    state = ensure_state("yes-no-offer")
    note_goal(state, GOAL)
    assert state.goal is not None
    goal_id = state.goal.goal_id

    note_goal(state, "yes")

    assert state.goal is not None
    assert state.goal.goal_id == goal_id
    assert state.pending_goal == "yes"
    assert "goal_continue: yes" in state.traces
