"""ContextCompiler: window planning and real-time budget monitoring (plan 2.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.context.compiler import ContextCompiler, budget_adherence_report
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

    assert plan.used_tokens <= 10_000
    # Per-part accounting is the authoritative monitor (newlines add minor
    # overhead to a full re-estimate of the joined text).
    assert abs(token_estimate(plan.text) - plan.used_tokens) <= 8
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
    kwargs = dict(
        memories=[_memory("decision one"), _memory("goal two")],
        user_state=_state(live=[]),
        strategy_text="STRATEGY: coaching",
        history=[{"role": "user", "text": "continue"}],
        rollup_summary="ROLLING SUMMARY (1 turn)",
        open_questions=["what next?"],
    )
    full = ContextCompiler().compile(budget=100_000, **kwargs)
    plan = ContextCompiler().compile(budget=full.used_tokens - 1, **kwargs)

    assert plan.over_budget is False
    assert "USER STATE" in plan.text
    # The budget monitor reports the section that had to give something up.
    assert any(section.truncated or section.items_dropped for section in plan.sections)
    # Top-priority sections survive even when the tail is cut.
    assert any(section.name == "user_state" and section.items_included for section in plan.sections)


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
    assert live_section.items_included == 6  # header + 5 lines
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
    history_line = next(
        line for line in first.text.splitlines() if line.startswith("- user:")
    )
    assert history_line == "- user: " + "word " * 200  # capped at 1000 chars


def test_progressive_starts_shallow_for_simple_message() -> None:
    memories = [_memory(f"fact {i}", memory_type="fact") for i in range(20)]
    plan = ContextCompiler().compile_progressive(
        memories=memories,
        user_state=_state(),
        strategy_text="STRATEGY: casual",
        budget=10_000,
        message="hello",
    )
    assert plan.metadata["depth"] == "shallow"
    assert plan.metadata["attempts"] == 1
    memory_section = next(
        s for s in plan.sections if s.name == "retrieved_memory"
    )
    assert memory_section.items_included <= 10


def test_progressive_deepens_for_deep_question() -> None:
    memories = [_memory(f"fact {i}", memory_type="fact") for i in range(30)]
    plan = ContextCompiler().compile_progressive(
        memories=memories,
        user_state=_state(),
        strategy_text="STRATEGY: casual",
        budget=100_000,
        message="Why did I decide to use Postgres in March?",
        shallow_k=5,
        deep_k=20,
    )
    assert plan.metadata["depth"] == "deep"
    assert plan.metadata["attempts"] == 2
    memory_section = next(
        s for s in plan.sections if s.name == "retrieved_memory"
    )
    assert memory_section.items_included > 5
    assert plan.over_budget is False


def test_budget_adherence_p95_across_50_varied_questions() -> None:
    questions = [
        "hello",
        "continue",
        "why did I decide to use SQLite?",
        "what was I thinking in March?",
        "who did I meet last month?",
        "do I prefer tea or coffee?",
        "as of last Tuesday, what were my goals?",
        "remember my project plan",
        "which embedding model is best?",
        "when did I move?",
        "how has my thinking changed since January?",
    ]
    while len(questions) < 50:
        questions.append(f"follow-up question number {len(questions)} about decisions and preferences?")
    report = budget_adherence_report(questions, budget=10_000)
    assert report["questions"] == 50
    assert report["over_budget_count"] == 0
    assert report["p95_utilization"] <= 1.0
    assert report["max_utilization"] <= 1.0
    print(
        "\nCONTEXT BUDGET ADHERENCE: "
        f"n={report['questions']} p95={report['p95_utilization']:.1%} "
        f"max={report['max_utilization']:.1%} over={report['over_budget_count']}"
    )
