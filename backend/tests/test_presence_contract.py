"""Presence OS V1 — durable intent contract (pure, hermetic, no DB)."""

from __future__ import annotations

import pytest

from app.presence.contract import (
    TERMINAL_STATES,
    AutonomyPolicy,
    GoalState,
    InterruptionPolicy,
    ShortCommand,
    autonomy_from_text,
    can_transition,
    classify_short_command,
    interruption_from_text,
    owner_line_for,
)


def test_goal_state_values() -> None:
    assert {s.value for s in GoalState} == {
        "DRAFT",
        "ACTIVE",
        "WAITING",
        "WAITING_FOR_CONDITION",
        "WAITING_FOR_DEVICE",
        "WAITING_FOR_APPROVAL",
        "PARKED",
        "STALLED",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "EXPIRED",
    }


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        ("DRAFT", "ACTIVE"),
        ("ACTIVE", "WAITING_FOR_DEVICE"),
        ("WAITING_FOR_DEVICE", "ACTIVE"),
        ("ACTIVE", "PARKED"),
        ("PARKED", "ACTIVE"),
        ("ACTIVE", "COMPLETED"),
    ],
)
def test_can_transition_legal(frm: str, to: str) -> None:
    assert can_transition(frm, to) is True


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        ("COMPLETED", "ACTIVE"),
        ("DRAFT", "COMPLETED"),
        ("PARKED", "WAITING"),
        ("BOGUS", "ACTIVE"),
        ("ACTIVE", "BOGUS"),
        ("", ""),
    ],
)
def test_can_transition_illegal(frm: str, to: str) -> None:
    assert can_transition(frm, to) is False


def test_terminal_states_membership() -> None:
    for state in (
        GoalState.COMPLETED,
        GoalState.FAILED,
        GoalState.CANCELLED,
        GoalState.EXPIRED,
    ):
        assert state in TERMINAL_STATES
    for state in (
        GoalState.DRAFT,
        GoalState.ACTIVE,
        GoalState.PARKED,
        GoalState.WAITING_FOR_DEVICE,
        GoalState.STALLED,
    ):
        assert state not in TERMINAL_STATES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("park this for now", ShortCommand.PARK),
        ("resume what we were doing", ShortCommand.RESUME),
        ("continue", ShortCommand.CONTINUE),
        ("stop", ShortCommand.STOP),
        ("move this to my mac", ShortCommand.MOVE_TO_MAC),
        ("bring this here", ShortCommand.BRING_HERE),
        ("what changed since yesterday?", ShortCommand.WHAT_CHANGED),
        ("why are we waiting on this?", ShortCommand.WHY_WAITING),
        ("what needs me right now?", ShortCommand.WHAT_NEEDS_ME),
        ("tell me a joke about cats", ShortCommand.UNKNOWN),
    ],
)
def test_classify_short_command(text: str, expected: ShortCommand) -> None:
    assert classify_short_command(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("contact me only if blocked", InterruptionPolicy.ONLY_IF_BLOCKED),
        ("silent until complete, don't interrupt", InterruptionPolicy.SILENT_UNTIL_COMPLETE),
        ("this is urgent, do it asap", InterruptionPolicy.URGENT),
        ("keep me posted on progress", InterruptionPolicy.NORMAL),
    ],
)
def test_interruption_from_text(text: str, expected: InterruptionPolicy) -> None:
    assert interruption_from_text(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("just look, don't change anything", AutonomyPolicy.READ_ONLY),
        ("don't send anything without asking", AutonomyPolicy.CONFIRM_EXTERNAL_WRITE),
        ("go ahead and handle it", AutonomyPolicy.SAFE_DIGITAL),
    ],
)
def test_autonomy_from_text(text: str, expected: AutonomyPolicy) -> None:
    assert autonomy_from_text(text) is expected


def test_owner_line_waiting_for_device_never_claims_done() -> None:
    line = owner_line_for(GoalState.WAITING_FOR_DEVICE, title="Demo").lower()
    assert "done" not in line
    assert "verified" not in line
    assert "complete" not in line.replace("incomplete", "")


def test_owner_line_completed_says_verified() -> None:
    assert "verified" in owner_line_for(GoalState.COMPLETED).lower()
