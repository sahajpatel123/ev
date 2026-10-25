"""agent_task tool: complicated goals execute in-turn, never 'submitted'."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.ev.desk_names import reset_desk_names
from app.ev.tools import _run_agent_task, list_tools


@pytest.fixture(autouse=True)
def _clear_desk_names():
    reset_desk_names()
    yield
    reset_desk_names()


@pytest.fixture
def files_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "laptop_files", True)
    monkeypatch.setattr(settings, "laptop_files_root", str(tmp_path))
    monkeypatch.setattr(settings, "agent_enabled", True)
    return tmp_path


def test_agent_task_tool_is_registered() -> None:
    names = {tool["name"] for tool in list_tools()}
    assert "agent_task" in names
    spec = next(tool for tool in list_tools() if tool["name"] == "agent_task")
    assert "submitted" in spec["description"]


@pytest.mark.asyncio
async def test_mutating_goal_parks_instead_of_submitting(files_root: Path) -> None:
    result = await _run_agent_task(
        None, {"goal": "create a folder called Trips"}, actor="test"
    )
    assert result["needs_confirm"] is True
    assert result["executed"] is False
    assert not (files_root / "Trips").exists()


@pytest.mark.asyncio
async def test_approved_goal_executes_synchronously_without_brain(
    files_root: Path,
) -> None:
    result = await _run_agent_task(
        None, {"goal": "create a folder called Trips"}, actor="test", approved=True
    )
    assert result["ok"] is True
    assert result["executed"] is True
    assert (files_root / "Trips").is_dir()
    assert "submitted" not in result["spoken"].lower()


@pytest.mark.asyncio
async def test_brain_path_maps_agent_result(monkeypatch, files_root: Path) -> None:
    del files_root
    import app.agent.brain as brain_module
    import app.agent.loop as loop_module
    from app.agent.brain import BrainChoice
    from app.agent.loop import AgentResult

    monkeypatch.setattr(
        brain_module,
        "resolve_agent_brain",
        lambda preferred=None: BrainChoice("deepseek", "m", "key", True),
    )

    async def fake_run_agent(goal, **kwargs):
        assert kwargs["approved"] is True
        return AgentResult(
            run_id="run-1",
            goal=goal,
            status="finished",
            ok=True,
            summary="Tidied the folder tree and saved the summary.",
            brain={"provider": "muse"},
            root="/tmp",
            approved=True,
            tool_calls=3,
        )

    monkeypatch.setattr(loop_module, "run_agent", fake_run_agent)
    result = await _run_agent_task(
        None,
        {"goal": "tidy my downloads and summarise what you moved"},
        actor="test",
        approved=True,
    )
    assert result["ok"] is True
    assert result["run_id"] == "run-1"
    assert result["spoken"] == "Tidied the folder tree and saved the summary."
