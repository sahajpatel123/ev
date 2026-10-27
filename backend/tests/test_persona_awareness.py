"""Awareness tests: NOW line omits missing signals, invents nothing, ~40 tokens."""

from __future__ import annotations

from datetime import datetime

from app.persona.awareness import build_now_line, signals_from_context
from app.persona.context import PersonaContext, WorkState
from app.persona.tokens import estimate


def test_empty_signals_yield_empty_line() -> None:
    assert build_now_line({}) == ""


def test_missing_signals_omitted_never_guessed() -> None:
    line = build_now_line({"task": "auth refactor"})
    assert line == "NOW: working: auth refactor"
    assert "session" not in line
    assert "tomorrow" not in line
    assert "error" not in line and "win" not in line


def test_full_line_shape() -> None:
    line = build_now_line(
        {
            "local_time": datetime(2026, 10, 27, 2, 40),
            "session_started": datetime(2026, 10, 26, 21, 30),
            "task": "auth refactor",
            "last_result": "3 tests failing",
            "recent_decline": "sleep at 01:15",
            "first_commitment_tomorrow": "10:00 class",
        }
    )
    assert line.startswith("NOW: Tue 02:40 · session 5h10m · working: auth refactor")
    assert "declined: sleep at 01:15" in line
    assert "tomorrow first: 10:00 class" in line


def test_now_line_stays_within_40_tokens() -> None:
    line = build_now_line(
        {
            "local_time": datetime(2026, 10, 27, 2, 40),
            "session_started": datetime(2026, 10, 26, 21, 30),
            "task": "x" * 200,
            "last_result": "y" * 200,
            "last_error": "z" * 200,
            "recent_offer": "w" * 200,
            "recent_decline": "v" * 200,
            "first_commitment_tomorrow": "u" * 200,
        }
    )
    assert estimate(line) <= 40


def test_error_beats_win_last_result_kept() -> None:
    line = build_now_line({"last_error": "timeout", "last_win": "deploy ok"})
    assert "error: timeout" in line
    assert "win:" not in line


def test_signals_from_context_reads_receipts() -> None:
    ctx = PersonaContext(
        channel="text",
        receipts=({"ok": True, "name": "build"}, {"ok": False, "name": "send", "error": "no route"}),
    )
    signals = signals_from_context(ctx)
    assert signals["last_error"] == "send failed: no route"
    assert signals["last_win"] == "build ok"


def test_signals_from_context_supports_object_receipts() -> None:
    class Receipt:
        ok = False
        name = "mail"
        error = "auth expired"

    ctx = PersonaContext(channel="text", receipts=(Receipt(),))
    assert signals_from_context(ctx)["last_error"] == "mail failed: auth expired"


def test_short_detail_keeps_first_two_segments() -> None:
    full = build_now_line(
        {
            "local_time": datetime(2026, 10, 27, 2, 40),
            "session_started": datetime(2026, 10, 26, 21, 30),
            "task": "auth refactor",
        }
    )
    short = build_now_line(
        {
            "local_time": datetime(2026, 10, 27, 2, 40),
            "session_started": datetime(2026, 10, 26, 21, 30),
            "task": "auth refactor",
        },
        detail="short",
    )
    assert full == "NOW: Tue 02:40 · session 5h10m · working: auth refactor"
    assert short == "NOW: Tue 02:40 · session 5h10m"


def test_work_state_task_flows_through() -> None:
    ctx = PersonaContext(
        channel="text",
        work_state=WorkState(task="ship it", last_result="green"),
    )
    line = build_now_line(signals_from_context(ctx))
    assert "working: ship it" in line
    assert "last: green" in line
