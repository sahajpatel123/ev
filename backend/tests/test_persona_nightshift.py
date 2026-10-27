"""Night Shift tests: triggers, nudge caps, plans, validation, briefs."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.persona.context import PersonaContext, WorkState
from app.persona.nightshift import (
    FORBIDDEN,
    DoneItem,
    MorningBrief,
    NightShiftPlan,
    NightState,
    fatigue_signals,
    nudge_recorded,
    plan_from_context,
    render_offer,
    render_plan_summary,
    should_nudge,
    validate,
)


def ctx(**kwargs) -> PersonaContext:
    kwargs.setdefault("channel", "text")
    return PersonaContext(**kwargs)


def test_fatigue_tired_phrases() -> None:
    assert fatigue_signals(["I'm so tired"], ()) == ["tired_phrase"]
    assert fatigue_signals(["one more deploy then bed"], ()) == ["tired_phrase"]
    assert fatigue_signals(["last thing before I go"], ()) == ["tired_phrase"]
    assert fatigue_signals(["all good, wide awake"], ()) == []


def test_fatigue_repeated_failure_needs_three() -> None:
    receipts = (
        {"ok": False, "name": "build", "error": "timeout"},
        {"ok": False, "name": "build", "error": "timeout"},
    )
    assert fatigue_signals([], receipts) == []
    receipts = receipts + ({"ok": False, "name": "build", "error": "timeout"},)
    assert fatigue_signals([], receipts) == ["repeated_failure"]


def test_fatigue_typo_rate_rising() -> None:
    clean = ["run the tests now", "show me the diff please"]
    messy = ["teh build is broekn agin", "waht is happpening with this"]
    assert fatigue_signals(clean + messy, ()) == ["typo_rate_up"]
    assert fatigue_signals(clean + clean, ()) == []


def test_should_nudge_needs_night_window() -> None:
    day = ctx(
        local_time=datetime(2026, 10, 27, 15, 0),
        session_started=datetime(2026, 10, 27, 9, 0),
    )
    assert should_nudge(day, None, recent_owner_msgs=["tired"]) is False
    early = ctx(
        local_time=datetime(2026, 10, 27, 0, 10),
        session_started=datetime(2026, 10, 26, 20, 0),
    )
    assert should_nudge(early, None, recent_owner_msgs=["tired"]) is False


def test_should_nudge_long_session_triggers() -> None:
    night = ctx(
        local_time=datetime(2026, 10, 27, 1, 0),
        session_started=datetime(2026, 10, 26, 22, 0),
    )
    assert should_nudge(night, None) is True


def test_should_nudge_needs_session_or_fatigue() -> None:
    night = ctx(local_time=datetime(2026, 10, 27, 1, 0))
    assert should_nudge(night, None) is False
    assert should_nudge(night, None, recent_owner_msgs=["tired"]) is True


def test_should_nudge_caps_gap_and_decline() -> None:
    night = ctx(
        local_time=datetime(2026, 10, 27, 1, 0),
        session_started=datetime(2026, 10, 26, 22, 0),
    )
    first = NightState(nudges=1, last_nudge_ts=datetime(2026, 10, 27, 0, 30))
    # 60 minutes later: inside the 90-minute gap.
    assert should_nudge(night, first) is False
    # 2 hours later with new signals: allowed (second nudge).
    later = ctx(
        local_time=datetime(2026, 10, 27, 2, 40),
        session_started=datetime(2026, 10, 26, 22, 0),
    )
    assert should_nudge(later, first, recent_owner_msgs=["tired"]) is True
    # Same signals, no news: not allowed.
    assert should_nudge(later, first) is False
    # Decline respected without new signals.
    declined = NightState(nudges=1, last_nudge_ts=datetime(2026, 10, 27, 0, 30),
                          declined=True)
    assert should_nudge(later, declined) is False
    assert should_nudge(later, declined, recent_owner_msgs=["tired"]) is True
    # Hard cap at 2.
    assert should_nudge(later, NightState(nudges=2)) is False


def test_nudge_recorded_helper() -> None:
    now = datetime(2026, 10, 27, 1, 0)
    state = nudge_recorded(None, now, ["tired_phrase"], declined=True)
    assert (state.nudges, state.declined, state.last_signals) == (
        1, True, ("tired_phrase",))
    assert state.date == "2026-10-27"


def test_plan_from_context_needs_clear_task() -> None:
    assert plan_from_context(ctx(), allow_execution=True) is None
    assert plan_from_context(ctx(work_state=WorkState(task="   ")),
                             allow_execution=True) is None
    plan = plan_from_context(
        ctx(work_state=WorkState(task="auth refactor"),
            local_time=datetime(2026, 10, 27, 1, 0)),
        allow_execution=True,
    )
    assert plan is not None
    assert plan.goal == "auth refactor"
    assert plan.workspace == "evie/night-20261027"
    assert validate(plan) == ()


def test_honesty_gate_blocks_takeover_by_default() -> None:
    from app.persona.nightshift import takeover_allowed
    from app.persona.settings import NIGHT_SHIFT_EXECUTION_AVAILABLE

    assert NIGHT_SHIFT_EXECUTION_AVAILABLE is False
    assert takeover_allowed() is False
    assert takeover_allowed(allow_execution=True) is True
    clear = ctx(work_state=WorkState(task="auth refactor"),
                local_time=datetime(2026, 10, 27, 1, 0))
    assert plan_from_context(clear) is None
    assert plan_from_context(clear, allow_execution=True) is not None


def test_validate_rejects_forbidden_capability() -> None:
    plan = NightShiftPlan(goal="g", task_ref="t", workspace="evie/night-20261027",
                          allowed=("read", "deploy"))
    errors = validate(plan)
    assert any("deploy" in error for error in errors)


def test_validate_rejects_weakened_forbidden_and_scope() -> None:
    plan = NightShiftPlan(goal="g", task_ref="t", workspace="evie/night-20261027",
                          forbidden=("merge",))
    assert any("forbidden" in error for error in validate(plan))
    plan = NightShiftPlan(goal="g", task_ref="t", workspace="evie/night-20261027",
                          new_features=True)
    assert any("new features" in error for error in validate(plan))
    plan = NightShiftPlan(goal="g", task_ref="t", workspace="main")
    assert any("workspace" in error for error in validate(plan))
    plan = NightShiftPlan(goal="", task_ref="", workspace="evie/night-20261027")
    assert len(validate(plan)) >= 2


def test_forbidden_list_is_complete() -> None:
    assert frozenset({
        "push_to_main", "merge", "deploy", "send", "delete_outside_workspace",
        "spend_money", "use_credentials", "first_contact",
    }) == FORBIDDEN


def test_stop_conditions_fixed() -> None:
    plan = NightShiftPlan(goal="g", task_ref="t", workspace="evie/night-20261027")
    assert plan.stop_conditions == (
        "goal_met", "time_budget", "wake_minus_30_min",
        "three_consecutive_failures",
    )
    assert plan.confirmation_queue == ()
    assert plan.checkpoint_every_min >= 5


def test_render_helpers_return_slots_not_wording() -> None:
    plan = NightShiftPlan(goal="auth refactor", task_ref="auth refactor",
                          workspace="evie/night-20261027")
    offer = render_offer(plan)
    assert offer["task"] == "auth refactor"
    assert sorted(offer["wont"]) == sorted(FORBIDDEN)
    summary = render_plan_summary(plan)
    assert summary["workspace"] == "evie/night-20261027"
    assert "read" in summary["allowed"]


def test_morning_brief_requires_receipt_refs() -> None:
    with pytest.raises(ValueError):
        DoneItem(summary="fixed it", ref="")
    with pytest.raises(ValueError):
        DoneItem(summary="fixed it", ref="   ")
    brief = MorningBrief(
        done=(DoneItem(summary="fixed auth", ref="run-123"),),
        tests="12 passed",
        waiting_for_you=("approve PR",),
        next_steps=("deploy at will",),
        night_line_slot="02:40–05:10, 3 test runs",
    )
    payload = brief.to_dict()
    assert payload["done"] == [{"summary": "fixed auth", "ref": "run-123"}]
    assert payload["waiting_for_you"] == ["approve PR"]
