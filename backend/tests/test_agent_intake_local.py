"""Durable intake: no-brain local work runs here, never queued to die."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.intake import handle_task
from app.config import settings
from app.ev.desk_names import reset_desk_names


@pytest.fixture(autouse=True)
def _clear_desk_names():
    reset_desk_names()
    yield
    reset_desk_names()


@pytest.fixture
def files_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "laptop_files", True)
    monkeypatch.setattr(settings, "laptop_files_root", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_local_work_without_brain_runs_synchronously(files_root: Path) -> None:
    (files_root / "OldStuff").mkdir()
    result = await handle_task(
        "create a folder called Trips then delete the folder OldStuff",
        source="chat",
    )
    assert result is not None
    assert result["ok"] is True
    assert result["local"] is True
    assert result["task_id"] == ""
    assert (files_root / "Trips").is_dir()
    assert not (files_root / "OldStuff").exists()
    assert "sent to the backend" not in result["spoken"].lower()
    assert "working upon" not in result["spoken"].lower()


@pytest.mark.asyncio
async def test_voice_local_work_parks_for_approval(files_root: Path) -> None:
    (files_root / "OldStuff").mkdir()
    result = await handle_task(
        "create a folder called Trips then delete the folder OldStuff",
        source="voice",
    )
    assert result is not None
    assert result["needs_confirm"] is True
    assert result["task_id"] != ""
    assert not (files_root / "Trips").exists()
    assert (files_root / "OldStuff").is_dir()


@pytest.mark.asyncio
async def test_approve_followup_executes_parked_local_plan(files_root: Path) -> None:
    from app.agent.intake import handle_control

    (files_root / "OldStuff").mkdir()
    parked = await handle_task(
        "create a folder called Trips then delete the folder OldStuff",
        source="voice",
    )
    assert parked is not None and parked["task_id"] != ""
    decided = await handle_control("approve it", source="voice")
    assert decided is not None
    assert decided["task_id"] == parked["task_id"]
    assert decided["ok"] is True
    assert (files_root / "Trips").is_dir()
    assert not (files_root / "OldStuff").exists()


@pytest.mark.asyncio
async def test_nonlocal_work_still_uses_durable_lane(monkeypatch) -> None:
    import app.agent.intake as intake_module

    calls: list[str] = []

    async def fake_wait(task_id: str, timeout: float):
        return {"id": "t1", "status": "completed", "summary": "Researched."}

    def fake_submit(goal: str, **kwargs):
        calls.append(goal)
        return {"id": "t1", "status": "running"}

    monkeypatch.setattr(intake_module.runner, "submit_task", fake_submit)
    monkeypatch.setattr(intake_module.runner, "wait_task", fake_wait)
    monkeypatch.setattr(intake_module.runner, "mark_consumed", lambda task_id: None)
    result = await handle_task(
        "collect the top headlines then compare them",
        source="chat",
    )
    assert result is not None
    assert result.get("local") is None
    assert calls == ["collect the top headlines then compare them"]


def _brain_script(*, available_first: bool):
    """resolve_agent_brain double: optionally available once, then dark."""
    from app.agent.brain import BrainChoice

    calls: list[str] = []

    def resolve(*args, **kwargs):
        calls.append("resolve")
        if available_first and len(calls) == 1:
            return BrainChoice("muse", "muse-test", "test", True, False)
        return BrainChoice("muse", "muse-test", "no key", False, False)

    return resolve


@pytest.mark.asyncio
async def test_brain_dark_durable_death_falls_back_to_local(
    files_root: Path, monkeypatch
) -> None:
    """Egress-denied durable run still does local work instead of dying."""
    import app.agent.intake as intake_module
    import app.agent.service as agent_service

    (files_root / "OldStuff").mkdir()

    def fake_submit(goal: str, **kwargs):
        return {
            "id": "t9", "status": "failed", "error": "egress_denied",
            "tool_calls": 0, "summary": "", "goal": goal,
        }

    monkeypatch.setattr(intake_module.runner, "submit_task", fake_submit)
    monkeypatch.setattr(intake_module.runner, "mark_consumed", lambda task_id: None)
    monkeypatch.setattr(
        agent_service, "resolve_agent_brain", _brain_script(available_first=True)
    )
    result = await handle_task(
        "create a folder called Trips then delete the folder OldStuff",
        source="chat",
    )
    assert result is not None
    assert result["ok"] is True
    assert result["brain_fallback"] == "egress_denied"
    assert (files_root / "Trips").is_dir()
    assert not (files_root / "OldStuff").exists()


@pytest.mark.asyncio
async def test_wait_path_brain_death_falls_back_to_local(
    files_root: Path, monkeypatch
) -> None:
    """Async durable death (running -> failed/egress) still executes locally."""
    import app.agent.intake as intake_module
    import app.agent.service as agent_service

    (files_root / "OldStuff").mkdir()

    def fake_submit(goal: str, **kwargs):
        return {"id": "t9", "status": "running", "goal": goal}

    async def fake_wait(task_id: str, timeout: float):
        return {
            "id": "t9", "status": "failed", "error": "egress_denied",
            "tool_calls": 0, "summary": "",
        }

    monkeypatch.setattr(intake_module.runner, "submit_task", fake_submit)
    monkeypatch.setattr(intake_module.runner, "wait_task", fake_wait)
    monkeypatch.setattr(intake_module.runner, "mark_consumed", lambda task_id: None)
    monkeypatch.setattr(
        agent_service, "resolve_agent_brain", _brain_script(available_first=True)
    )
    result = await handle_task(
        "create a folder called Trips then delete the folder OldStuff",
        source="chat",
    )
    assert result is not None
    assert result["ok"] is True
    assert result["brain_fallback"] == "egress_denied"
    assert (files_root / "Trips").is_dir()
    assert not (files_root / "OldStuff").exists()


@pytest.mark.asyncio
async def test_nonbrain_durable_failure_keeps_honest_error(
    files_root: Path, monkeypatch
) -> None:
    """A real work failure is never papered over by the fallback."""
    import app.agent.intake as intake_module
    import app.agent.service as agent_service

    def fake_submit(goal: str, **kwargs):
        return {
            "id": "t9", "status": "failed", "error": "policy_denied",
            "tool_calls": 0, "summary": "", "goal": goal,
        }

    monkeypatch.setattr(intake_module.runner, "submit_task", fake_submit)
    monkeypatch.setattr(intake_module.runner, "mark_consumed", lambda task_id: None)
    monkeypatch.setattr(
        agent_service, "resolve_agent_brain", _brain_script(available_first=True)
    )
    result = await handle_task(
        "create a folder called Trips then delete the folder OldStuff",
        source="chat",
    )
    assert result is not None
    assert result["ok"] is False
    assert result.get("brain_fallback") is None
    assert not (files_root / "Trips").exists()


@pytest.mark.asyncio
async def test_partial_durable_run_never_falls_back(
    files_root: Path, monkeypatch
) -> None:
    """Zero-tool-call guard: acted work must not double-apply."""
    import app.agent.intake as intake_module
    import app.agent.service as agent_service

    def fake_submit(goal: str, **kwargs):
        return {
            "id": "t9", "status": "failed", "error": "egress_denied",
            "tool_calls": 2, "summary": "", "goal": goal,
        }

    monkeypatch.setattr(intake_module.runner, "submit_task", fake_submit)
    monkeypatch.setattr(intake_module.runner, "mark_consumed", lambda task_id: None)
    monkeypatch.setattr(
        agent_service, "resolve_agent_brain", _brain_script(available_first=True)
    )
    result = await handle_task(
        "create a folder called Trips then delete the folder OldStuff",
        source="chat",
    )
    assert result is not None
    assert result["ok"] is False
    assert result.get("brain_fallback") is None
    assert not (files_root / "Trips").exists()
