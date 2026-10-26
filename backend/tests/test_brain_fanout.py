"""Per-turn brain-call guard (0.3): dedupe + quota + same-goal agent dedupe.

Regression: the loops nest — one kernel round can submit an ``agent.run``
that burns up to 24 brain calls of its own, invisibly to the turn, and a
dithering brain can re-issue the same work across rounds (the recon's
listing turn showed runaway multiplication on DeepSeek-low; on Muse the
same turn uses 4-5 calls, but the harness structure is identical). The
guard gives the turn's whole causal tree one quota, memoizes identical
requests, and reuses a same-turn agent receipt instead of stacking runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.contracts import ChatMessage, ChatResult, ToolCall, ToolSpec
from app.gateway.turn_guard import (
    TurnBudgetExceeded,
    TurnGuard,
    brain_call_key,
    current_guard,
    turn_guard,
)


def _spec(name: str = "memory.search") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="test tool",
        parameters={"type": "object", "properties": {}},
    )


def _messages(text: str = "hello") -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="You are Evie."),
        ChatMessage(role="user", content=text),
    ]


# --------------------------------------------------------------------------- #
# Guard unit behavior
# --------------------------------------------------------------------------- #


def test_identical_calls_are_memoized() -> None:
    guard = TurnGuard(turn_id="t", max_calls=60, dedupe_enabled=True)
    key = brain_call_key(
        messages=_messages(), tools=[_spec()], model="m", temperature=0.7, effort="low"
    )
    assert guard.before_call(key) is None
    reply = ChatResult(text="hi", usage={"prompt_tokens": 5}, model="m")
    guard.after_call(key, reply)

    memo = guard.before_call(key)
    assert memo is not None
    assert memo.text == "hi"
    assert memo is not reply
    assert guard.calls == 1
    assert guard.dedupe_hits == 1


def test_key_changes_with_messages_tools_and_params() -> None:
    base = dict(
        messages=_messages(), tools=[_spec()], model="m", temperature=0.7, effort="low"
    )
    same = brain_call_key(**base)
    assert brain_call_key(**base) == same
    assert brain_call_key(**{**base, "messages": _messages("other")}) != same
    assert brain_call_key(**{**base, "tools": [_spec("other.tool")]}) != same
    assert brain_call_key(**{**base, "effort": "high"}) != same
    assert brain_call_key(**{**base, "temperature": 0.2}) != same
    assert brain_call_key(**{**base, "model": "other"}) != same
    assert brain_call_key(**{**base, "tools": None}) != same


def test_quota_raises_and_reports() -> None:
    guard = TurnGuard(turn_id="t", max_calls=2, dedupe_enabled=True)
    guard.after_call("k1", ChatResult(text="a"))
    guard.after_call("k2", ChatResult(text="b"))
    try:
        guard.before_call("k3")
    except TurnBudgetExceeded as exc:
        assert "quota" in str(exc)
    else:
        raise AssertionError("spent quota must raise")
    assert guard.cap_hit is True
    summary = guard.summary()
    assert summary["brain_calls"] == 2
    assert summary["cap_hit"] is True


def test_zero_max_calls_disables_the_cap() -> None:
    guard = TurnGuard(turn_id="t", max_calls=0, dedupe_enabled=True)
    for index in range(5):
        assert guard.before_call(f"k{index}") is None
        guard.after_call(f"k{index}", ChatResult(text="x"))
    assert guard.cap_hit is False


def test_nested_guard_reuses_the_outer() -> None:
    with turn_guard("outer", max_calls=7) as outer:
        assert current_guard() is outer
        with turn_guard("inner", max_calls=1) as inner:
            assert inner is outer
        assert current_guard() is outer
    assert current_guard() is None


def test_same_goal_agent_runs_share_one_receipt() -> None:
    guard = TurnGuard(turn_id="t", max_calls=60, dedupe_enabled=True)
    assert guard.check_agent_run("List the files") is None
    receipt = {"ok": True, "task_id": "task-1"}
    guard.record_agent_run("List the files", receipt)
    assert guard.check_agent_run("list   the FILES") == receipt
    assert guard.check_agent_run("something else") is None


# --------------------------------------------------------------------------- #
# Loop integration (scripted brains, deterministic)
# --------------------------------------------------------------------------- #


class _ScriptedBrain:
    def __init__(self, replies: list[ChatResult]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.reasoning_effort: str | None = None

    async def chat_with_tools(self, messages, specs, **kwargs):
        del messages, specs, kwargs
        self.calls += 1
        return self.replies.pop(0)


@pytest.fixture
def _kernel_mode(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(settings, "cognitive_mode", "realtime_delegate")
    monkeypatch.setattr(settings, "cognitive_role", "kernel")
    monkeypatch.setattr(settings, "laptop_files", True)
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    from app.cognitive import telemetry
    from app.cognitive.session_store import reset_for_tests

    reset_for_tests()
    telemetry.reset_for_tests()
    yield
    reset_for_tests()
    telemetry.reset_for_tests()


@pytest.fixture
def _isolated_lane(monkeypatch, tmp_path: Path):
    from app.agent import ledger, runner

    monkeypatch.setattr(settings, "agent_state_db", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(settings, "agent_root", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir(exist_ok=True)
    ledger.init()

    async def fake_announce(title: str, body: str, *, kind: str = "agent") -> None:
        del title, body, kind

    import app.agent.notify as notify

    monkeypatch.setattr(notify, "announce", fake_announce)
    yield
    runner._MIRRORS.clear()


def _install_agent_provider(monkeypatch, provider: _ScriptedBrain) -> None:
    import app.agent.loop as loop
    from app.agent.brain import BrainChoice

    choice = BrainChoice("scripted", "scripted", "test", True)
    monkeypatch.setattr("app.agent.service.resolve_agent_brain", lambda *a, **k: choice)
    monkeypatch.setattr(loop, "build_agent_provider", lambda c: provider)


def _finish(summary: str) -> ToolCall:
    return ToolCall(
        id="finish-1", name="finish", arguments={"summary": summary, "ok": True}
    )


async def test_kernel_quota_stop_is_honest(
    _kernel_mode, monkeypatch, db_session: AsyncSession
) -> None:
    from app.cognitive import kernel
    from app.cognitive.telemetry import snapshot

    rounds = [
        ChatResult(
            text="",
            tool_calls=[
                ToolCall(
                    id=f"c{i}", name="memory.search", arguments={"query": f"q{i}"}
                )
            ],
        )
        for i in range(10)
    ]
    brain = _ScriptedBrain(rounds)
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    monkeypatch.setattr("app.gateway.roles.require_text_provider", lambda: brain)

    with turn_guard("quota-test", max_calls=3):
        result = await kernel.handle_turn(
            transcript="Tell me how you are today.",
            modality="voice",
            session=db_session,
        )
    assert brain.calls == 3
    assert result.kind == "capped"
    assert "3 brain calls" in result.spoken
    assert result.usage["brain_calls"] == 3
    assert snapshot()["brain_call_cap_hits"] == 1


async def test_agent_quota_stop_is_honest(_isolated_lane, tmp_path: Path) -> None:
    from app.agent.loop import run_agent
    from app.cognitive.telemetry import snapshot

    provider = _ScriptedBrain(
        [
            ChatResult(
                text="",
                tool_calls=[
                    ToolCall(id=f"c{i}", name="list_dir", arguments={"path": f"d{i}"})
                ],
            )
            for i in range(10)
        ]
    )
    for index in range(3):
        (tmp_path / "workspace" / f"d{index}").mkdir()
    with turn_guard("quota-test", max_calls=2):
        result = await run_agent(
            "look around", root=tmp_path / "workspace", provider=provider
        )
    assert provider.calls == 2
    assert result.status == "capped"
    assert result.error == "turn_brain_quota"
    assert "2 brain calls" in result.summary
    assert result.usage["brain_calls"] == 2
    assert snapshot()["brain_call_cap_hits"] == 1


async def test_second_identical_agent_run_is_memoized(
    _isolated_lane, tmp_path: Path
) -> None:
    from app.agent.loop import run_agent

    async def one(provider: _ScriptedBrain):
        return await run_agent(
            "list the workspace files", root=tmp_path / "workspace", provider=provider
        )

    with turn_guard("memo-test", max_calls=60) as guard:
        first = await one(_ScriptedBrain([ChatResult(text="", tool_calls=[_finish("listed.")])]))
        assert first.ok is True
        # Same goal + same root: the first brain request is byte-identical,
        # so the second run's opening call is served from the memo.
        provider = _ScriptedBrain([ChatResult(text="", tool_calls=[_finish("listed.")])])
        second = await one(provider)
        assert second.ok is True
        assert provider.calls == 0
        assert guard.dedupe_hits == 1


async def test_listing_nesting_does_not_stack_agent_runs(
    _kernel_mode, _isolated_lane, monkeypatch, db_session: AsyncSession, tmp_path: Path
) -> None:
    """The dossier's listing case: re-issued agent goals reuse one run."""

    from app.agent import ledger
    from app.cognitive import kernel
    from app.cognitive.telemetry import snapshot

    (tmp_path / "workspace" / "notes.txt").write_text("hello\n")
    brain = _ScriptedBrain([
        ChatResult(
            text="",
            tool_calls=[
                ToolCall(
                    id="k1", name="agent.run",
                    arguments={"goal": "List the workspace files"},
                )
            ],
        ),
        # Same goal modulo case: the kernel executes it (different tool
        # signature) but the bridge must not start a second run.
        ChatResult(
            text="",
            tool_calls=[
                ToolCall(
                    id="k2", name="agent.run",
                    arguments={"goal": "list the workspace files"},
                )
            ],
        ),
        ChatResult(text="The workspace holds notes.txt."),
    ])
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    monkeypatch.setattr("app.gateway.roles.require_text_provider", lambda: brain)
    agent_provider = _ScriptedBrain([
        ChatResult(
            text="",
            tool_calls=[ToolCall(id="a1", name="list_dir", arguments={})],
        ),
        ChatResult(text="", tool_calls=[_finish("The workspace holds notes.txt.")]),
    ])
    _install_agent_provider(monkeypatch, agent_provider)

    with turn_guard("listing-test", max_calls=60):
        result = await kernel.handle_turn(
            transcript="What files are in the current workspace folder?",
            modality="typed",
            session=db_session,
        )
    assert brain.calls >= 1, "routing changed: the brain was never consulted"
    assert result.kind == "brain"
    assert "notes.txt" in result.spoken
    assert brain.calls == 3
    # One agent run (2 brain calls), not two stacked runs (4).
    assert agent_provider.calls == 2
    assert len(ledger.list_tasks(limit=10)) == 1
    assert snapshot()["duplicate_agent_runs"] == 1
