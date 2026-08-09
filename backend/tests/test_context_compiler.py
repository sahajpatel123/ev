"""ContextCompiler: window planning and real-time budget monitoring (plan 2.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.context.compiler import ContextCompiler
from app.schemas import UserStateOut
from app.utils.text import token_estimate


def _memory(text: str, *, memory_type: str = "fact", score: float = 0.8) -> SimpleNamespace:
    return SimpleNamespace(
        memory_type=memory_type,
        score=score,
        event_time=datetime(2026, 8, 1, tzinfo=UTC),
        confidence=0.85,
        text=text,
    )


def _state(*, live: list[str] | None = None) -> UserStateOut:
    return UserStateOut(
        activity="coding",
        active_project="EV",
        active_goal="Ship EV",
        current_task="context compiler",
        recent_topics=["compiler", "budget"],
        live_context=live or ["[screen-activity/focus_change] screen app=Xcode"],
    )


def test_compiler_builds_full_window_in_priority_order() -> None:
    compiler = ContextCompiler()
    plan = compiler.compile(
        memories=[_memory("decision one"), _memory("goal two")],
        user_state=_state(),
        strategy_text="STRATEGY: coaching",
        budget=10_000,
        history=[{"role": "user", "text": "continue"}],
        rollup_summary="ROLLING SUMMARY (1 turn)",
        open_questions=["what next?"],
    )

    assert plan.used_tokens == token_estimate(plan.text)
    assert plan.over_budget is False
    assert plan.remaining_tokens == 10_000 - plan.used_tokens
    assert "STRATEGY: coaching" in plan.text
    assert "USER STATE: activity=coding" in plan.text
    assert "LIVE CONTEXT" in plan.text
    assert "ROLLING SUMMARY" in plan.text
    assert "RETRIEVED MEMORY" in plan.text
    assert "CONVERSATION HISTORY" in plan.text
    assert "OPEN QUESTIONS" in plan.text

    names = [section.name for section in plan.sections]
    assert names.index("strategy") < names.index("user_state")
    assert names.index("user_state") < names.index("retrieved_memory")
    assert names.index("retrieved_memory") < names.index("conversation_history")
    assert names.index("conversation_history") < names.index("open_questions")


def test_compiler_drops_low_priority_sections_when_budget_tight() -> None:
    plan = ContextCompiler().compile(
        memories=[_memory("decision one"), _memory("goal two")],
        user_state=_state(live=[]),
        strategy_text="STRATEGY: coaching",
        budget=80,
        history=[{"role": "user", "text": "continue"}],
        rollup_summary="ROLLING SUMMARY (1 turn)",
        open_questions=["what next?"],
    )

    assert plan.over_budget is False
    assert "USER STATE" in plan.text
    # Lower-priority sections are dropped rather than blowing the budget.
    assert "RETRIEVED MEMORY" not in plan.text
    assert "CONVERSATION HISTORY" not in plan.text
    assert "OPEN QUESTIONS" not in plan.text
    dropped = {
        section.name: section.items_dropped
        for section in plan.sections
        if section.items_dropped
    }
    assert "retrieved_memory" in dropped
    assert "conversation_history" in dropped
    assert "open_questions" in dropped


def test_compiler_caps_live_lines_and_reports_truncation() -> None:
    live = [f"[screen-activity/{i}] event {i}" for i in range(7)]
    plan = ContextCompiler().compile(
        memories=[],
        user_state=_state(live=live),
        strategy_text="STRATEGY: casual",
        budget=10_000,
    )

    live_section = next(
        section for section in plan.sections if section.name == "live_context_header"
    )
    assert live_section.items_included == 5
    assert live_section.items_dropped == 2
    assert live_section.truncated is True
    assert plan.text.count("[screen-activity/") == 5


def test_compiler_is_deterministic_and_truncates_history_lines() -> None:
    long_text = "word " * 1200
    kwargs = dict(
        memories=[_memory("fact")],
        user_state=_state(),
        strategy_text="STRATEGY: casual",
        budget=10_000,
        history=[{"role": "user", "text": long_text}],
    )
    first = ContextCompiler().compile(**kwargs)
    second = ContextCompiler().compile(**kwargs)
    assert first.text == second.text
    assert first.used_tokens == second.used_tokens
    assert "word " * 999 in first.text  # capped at 1000 chars per history line
