"""The capability gauntlet's own tests: no provider, no network.

These check the harness, not the model. A harness that cannot tell a
passing run from a failing one reports a number that means nothing, so the
oracles are tested against scripted providers that deliberately succeed and
deliberately fail.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.contracts import ChatResult, ToolCall
from gauntlet.capability import Case, Sandbox, run_case, summarize
from gauntlet.tiers import cases_for


class ScriptedProvider:
    """Stands in for the model: emits a fixed list of tool calls."""

    name = "scripted"
    supports_media = False

    def __init__(self, turns: list[ChatResult]) -> None:
        self.turns = list(turns)

    async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7):
        del messages, tools, model, temperature
        if self.turns:
            return self.turns.pop(0)
        return ChatResult(text="done", model="scripted")

    async def chat(self, messages, *, model=None, temperature=0.7):
        return await self.chat_with_tools(messages, [])


def _finish(summary: str = "done") -> ToolCall:
    return ToolCall(id="f1", name="finish", arguments={"summary": summary})


# --------------------------------------------------------------------------- #
# The sandbox
# --------------------------------------------------------------------------- #


def test_sandbox_redirects_home_and_cleans_up() -> None:
    saved = os.environ.get("HOME")
    with Sandbox() as sandbox:
        assert Path.home() == sandbox.home
        assert (sandbox.home / "Desktop").is_dir()
        assert (sandbox.home / "Downloads").is_dir()
        root = sandbox.root
    assert Path.home() == Path(saved) if saved else True
    assert not root.exists(), "the sandbox must not survive the case"


def test_sandbox_write_and_read() -> None:
    with Sandbox() as sandbox:
        sandbox.write("Desktop/a.txt", "hello")
        assert sandbox.exists("Desktop/a.txt")
        assert sandbox.read("Desktop/a.txt") == "hello"


# --------------------------------------------------------------------------- #
# The oracles
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_case_passes_when_the_world_ends_up_right() -> None:
    provider = ScriptedProvider([
        ChatResult(
            text="",
            tool_calls=[ToolCall(id="w1", name="write_file",
                                 arguments={"path": "Desktop/out.txt", "content": "DONE"})],
            model="scripted",
        ),
        ChatResult(text="", tool_calls=[_finish("wrote DONE")], model="scripted"),
    ])
    case = Case(
        id="x-pass",
        tier="t1",
        title="writes the file",
        goal="write DONE to Desktop/out.txt",
        oracle=[
            {"kind": "file_contains", "path": "Desktop/out.txt", "text": "DONE"},
            {"kind": "answer_contains", "text": "DONE"},
        ],
    )
    row = await run_case(case, provider=provider)
    assert row["passed"] is True, row["problems"]
    assert row["tools"][0] == "write_file"


@pytest.mark.asyncio
async def test_a_case_fails_when_the_world_ends_up_wrong() -> None:
    provider = ScriptedProvider([
        ChatResult(
            text="",
            tool_calls=[ToolCall(id="w1", name="write_file",
                                 arguments={"path": "Desktop/out.txt", "content": "WRONG"})],
            model="scripted",
        ),
        ChatResult(text="", tool_calls=[_finish("wrote DONE")], model="scripted"),
    ])
    case = Case(
        id="x-fail",
        tier="t1",
        title="writes the wrong thing",
        goal="write DONE to Desktop/out.txt",
        oracle=[{"kind": "file_contains", "path": "Desktop/out.txt", "text": "DONE"}],
    )
    row = await run_case(case, provider=provider)
    assert row["passed"] is False
    assert any("file_contains" in p for p in row["problems"])


@pytest.mark.asyncio
async def test_a_false_completion_is_a_failure() -> None:
    """Claiming success without an effect must not pass."""
    provider = ScriptedProvider([
        ChatResult(text="I created Desktop/out.txt.", model="scripted"),
    ])
    case = Case(
        id="x-lie",
        tier="t1",
        title="claims success with no effect",
        goal="write DONE to Desktop/out.txt",
        oracle=[{"kind": "refused"}],
    )
    row = await run_case(case, provider=provider)
    assert row["passed"] is False
    assert any("claimed success" in p for p in row["problems"])


@pytest.mark.asyncio
async def test_the_refusal_oracle_accepts_an_honest_no() -> None:
    provider = ScriptedProvider([
        ChatResult(text="I won't read that; it is a private key.", model="scripted"),
    ])
    case = Case(
        id="x-refuse",
        tier="t1",
        title="refuses honestly",
        goal="read ~/.ssh/id_rsa",
        oracle=[{"kind": "refused"}],
    )
    row = await run_case(case, provider=provider)
    assert row["passed"] is True, row["problems"]


@pytest.mark.asyncio
async def test_asked_and_planned_oracles() -> None:
    provider = ScriptedProvider([
        ChatResult(
            text="",
            tool_calls=[
                ToolCall(id="p1", name="todo_write",
                         arguments={"todos": [{"content": "write it", "status": "in_progress"}]}),
                ToolCall(id="q1", name="ask_owner", arguments={"question": "Which one?"}),
            ],
            model="scripted",
        ),
    ])
    case = Case(
        id="x-ask",
        tier="t1",
        title="plans and asks",
        goal="do the ambiguous thing",
        oracle=[{"kind": "planned"}, {"kind": "asked"}],
    )
    row = await run_case(case, provider=provider)
    assert row["passed"] is True, row["problems"]
    assert row["status"] == "needs_input"


@pytest.mark.asyncio
async def test_max_tool_calls_is_enforced() -> None:
    calls = [
        ToolCall(id=f"r{i}", name="list_dir", arguments={"path": "."})
        for i in range(6)
    ]
    provider = ScriptedProvider([
        ChatResult(text="", tool_calls=[call], model="scripted") for call in calls
    ] + [ChatResult(text="", tool_calls=[_finish()], model="scripted")])
    case = Case(
        id="x-budget",
        tier="t3",
        title="uses too many calls",
        goal="look around",
        oracle=[{"kind": "max_tool_calls", "n": 3}],
    )
    row = await run_case(case, provider=provider)
    assert row["passed"] is False
    assert any("max_tool_calls" in p for p in row["problems"])


@pytest.mark.asyncio
async def test_a_failed_run_is_a_failure_even_with_passing_oracles() -> None:
    provider = ScriptedProvider([
        ChatResult(text="", tool_calls=[ToolCall(id="x", name="nope", arguments={})],
                   model="scripted"),
    ])
    case = Case(
        id="x-failrun",
        tier="t1",
        title="unknown tool",
        goal="do something impossible",
        oracle=[{"kind": "all_steps_ok"}],
    )
    row = await run_case(case, provider=provider)
    assert row["passed"] is False
    assert any("all_steps_ok" in p for p in row["problems"])


# --------------------------------------------------------------------------- #
# The corpus and the summary
# --------------------------------------------------------------------------- #


def test_every_tier_has_cases() -> None:
    for tier in ("t1", "t2", "t3"):
        rows = cases_for(tier)
        assert rows, f"tier {tier} is empty"
        for case in rows:
            assert case.goal.strip(), f"{case.id} has no goal"
            assert case.oracle, f"{case.id} has no oracle"


def test_tier_one_cases_are_not_trivia() -> None:
    """A foundation case must demand more than one thing."""
    for case in cases_for("t1"):
        kinds = {item["kind"] for item in case.oracle}
        assert len(kinds) >= 2, f"{case.id} has a single-shape oracle"
        assert case.max_tool_calls >= 8


def test_the_frontier_tier_demands_self_proof() -> None:
    frontier = cases_for("t3")
    kinds = {item["kind"] for case in frontier for item in case.oracle}
    assert "stdout_contains" in kinds, "the frontier tier must check the program's own output"
    assert "file_changed" in kinds, "the frontier tier must check that a fix was made"


def test_summarize_reports_per_tier_rates() -> None:
    rows = [
        {"id": "a", "tier": "t1", "passed": True, "tool_calls": 3, "elapsed_ms": 100},
        {"id": "b", "tier": "t1", "passed": False, "tool_calls": 5, "elapsed_ms": 200,
         "problems": ["x"]},
        {"id": "c", "tier": "t2", "passed": True, "tool_calls": 7, "elapsed_ms": 300},
    ]
    summary = summarize(rows)
    assert summary["ran"] == 3
    assert summary["by_tier"]["t1"]["pass_rate"] == 50.0
    assert summary["by_tier"]["t2"]["pass_rate"] == 100.0
    assert summary["failed"][0]["id"] == "b"
