"""Explicit workspace roots (B4): the agent never defaults to $HOME.

Regression: a hello-world listing walked the owner's whole home folder
because an unset EV_AGENT_ROOT fell back to ``Path.home()``. A missing
root now fails closed with a setup hint, and filesystem-free tools use
an Evie-owned context instead of implying an owner folder.
"""

from __future__ import annotations

from pathlib import Path

from app.agent import tools as agent_tools
from app.agent.loop import run_agent
from app.config import settings
from app.contracts import ChatResult, ToolCall


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, turns: list[ChatResult]) -> None:
        self._turns = list(turns)
        self.seen: list[list] = []

    async def chat_with_tools(self, messages, tools, **kwargs):
        self.seen.append(list(messages))
        if self._turns:
            return self._turns.pop(0)
        return ChatResult(text="nothing left", model="scripted")


def _finish(summary: str = "done") -> ChatResult:
    return ChatResult(
        text="",
        tool_calls=[
            ToolCall(
                id="finish-1",
                name="finish",
                arguments={"summary": summary, "ok": True},
            )
        ],
        model="scripted",
    )


def test_missing_root_fails_closed_with_setup_hint(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_root", "")
    assert agent_tools.configured_root() is None
    try:
        agent_tools._norm_root(None)
    except agent_tools.NoWorkspaceRoot as exc:
        assert "EV_AGENT_ROOT" in str(exc)
        assert str(Path.home()) not in str(exc)
    else:
        raise AssertionError("missing root must raise NoWorkspaceRoot, not default to $HOME")


def test_explicit_setting_root_is_used(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "agent_root", str(tmp_path))
    assert agent_tools.configured_root() == tmp_path.resolve()
    assert agent_tools._norm_root(None) == tmp_path.resolve()


async def test_run_without_root_fails_before_any_brain_call(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_root", "")
    provider = _ScriptedProvider([_finish()])
    result = await run_agent("list the workspace files", root=None, provider=provider)
    assert result.ok is False
    assert result.status == "failed"
    assert result.error == "no_workspace_root"
    assert "EV_AGENT_ROOT" in result.summary
    assert result.steps == []
    assert result.tool_calls == 0
    assert provider.seen == []


async def test_run_with_explicit_root_reaches_the_brain(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "agent_root", "")
    provider = _ScriptedProvider([_finish("listed.")])
    result = await run_agent(
        "list the workspace files", root=tmp_path, provider=provider
    )
    assert result.ok is True
    assert result.status == "completed"
    assert result.root == str(tmp_path.resolve())
    assert len(provider.seen) == 1


def test_action_context_never_implies_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "agent_root", "")
    monkeypatch.setattr(settings, "agent_trace_dir", str(tmp_path))
    ctx = agent_tools.action_context(run_id="probe")
    assert ctx.root != Path.home()
    assert tmp_path.resolve() in ctx.root.resolve().parents


def test_action_context_falls_back_to_evie_state_dir(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_root", "")
    monkeypatch.setattr(settings, "agent_trace_dir", "")
    ctx = agent_tools.action_context(run_id="probe")
    assert ctx.root != Path.home()
    assert (Path.home() / ".ev").resolve() in ctx.root.resolve().parents
