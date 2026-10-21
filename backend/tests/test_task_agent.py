"""General task agent: goal -> plan -> approve once -> verified run."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.ev.desk_names import reset_desk_names
from app.ev.task_agent import describe_plan, plan_task, run_task


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


def test_compound_goal_decomposes_across_clauses() -> None:
    plan = plan_task("create a folder called Trips then delete the folder OldStuff")
    assert [step["kind"] for step in plan["steps"]] == ["file_op", "file_op"]
    assert [step["arguments"]["action"] for step in plan["steps"]] == ["mkdir", "delete"]
    assert plan["mutating"] is True
    assert "Trips" in describe_plan(plan)


def test_second_imperative_after_and_is_its_own_step() -> None:
    plan = plan_task(
        "Create a folder on my Desktop called FallbackProof and put a file "
        "inside it named hello.txt"
    )
    assert len(plan["steps"]) == 2
    assert plan["steps"][0]["arguments"]["action"] == "mkdir"
    assert "FallbackProof" in str(plan["steps"][0]["arguments"].get("path"))


def test_bare_and_without_verb_does_not_split() -> None:
    plan = plan_task("create two files on my desktop called a.txt and b.txt")
    assert len(plan["steps"]) == 1


@pytest.mark.asyncio
async def test_mutating_plan_waits_for_one_approval(files_root: Path) -> None:
    (files_root / "OldStuff").mkdir()
    goal = "create a folder called Trips then delete the folder OldStuff"
    pending = await run_task(goal)
    assert pending["needs_confirm"] is True
    assert not (files_root / "Trips").exists()
    assert (files_root / "OldStuff").is_dir()

    done = await run_task(goal, approved=True)
    assert done["ok"] is True
    assert (files_root / "Trips").is_dir()
    assert not (files_root / "OldStuff").exists()
    assert len(done["receipts"]) == 2


@pytest.mark.asyncio
async def test_read_only_plan_runs_without_approval(files_root: Path) -> None:
    (files_root / "note.txt").write_text("hi", encoding="utf-8")
    done = await run_task("list the files on my desktop")
    assert done["ok"] is True
    assert done["receipts"][0]["kind"] == "file_op"


@pytest.mark.asyncio
async def test_chat_single_step_executes_without_brain(files_root: Path) -> None:
    from app.api.core import _maybe_deterministic_single_step

    spoken = await _maybe_deterministic_single_step("create a folder called Trips")
    assert spoken is not None
    assert (files_root / "Trips").is_dir()
    assert await _maybe_deterministic_single_step("what is the time") is None
    assert (
        await _maybe_deterministic_single_step(
            "create a folder called A then delete the folder B"
        )
        is None
    )


@pytest.mark.asyncio
async def test_failed_step_stops_honestly(files_root: Path) -> None:
    del files_root
    done = await run_task("delete the folder GhostMissing", approved=True)
    assert done["ok"] is False
    assert done["error"]
    assert len(done["receipts"]) == 1


def test_gui_clause_infers_named_app() -> None:
    plan = plan_task("frobulate the Finder")
    gui = plan["steps"][0]
    assert gui["kind"] == "gui"
    assert gui["arguments"]["app"] == "Finder"


@pytest.mark.asyncio
async def test_brain_arbitrates_contested_clause(monkeypatch) -> None:
    import app.gateway.roles as roles

    monkeypatch.setattr(roles, "text_role_available", lambda: True)

    class _Answer:
        type = "choice"
        choice = "app:Notes"

    class _Call:
        status = "ok"
        decision_answers = {"clause0": _Answer()}

    async def fake_decide(state, questions, *, session=None, actor="system"):
        assert "clause0" in questions
        return _Call()

    monkeypatch.setattr(roles, "decide_via_role", fake_decide)
    monkeypatch.setattr(roles, "answer_choice", lambda call, qid: "app:Notes")

    from app.ev.task_agent import plan_task_async

    plan = await plan_task_async("do the thing with my stuff")
    assert plan["planner"] == "brain_arbitrated"
    assert plan["degraded"] is False
    assert plan["steps"][0]["arguments"]["app"] == "Notes"


@pytest.mark.asyncio
async def test_offline_contested_clause_stays_deterministic() -> None:
    from app.ev.task_agent import plan_task_async

    plan = await plan_task_async("do the thing with my stuff")
    assert plan["planner"] == "deterministic"
    assert plan["degraded"] is True
    assert plan["steps"][0]["kind"] == "gui"


def test_shell_command_extracts_from_clause() -> None:
    plan = plan_task("Please run the shell command `echo traced-ok` and report it.")
    assert [step["kind"] for step in plan["steps"]] == ["shell"]
    assert plan["steps"][0]["arguments"]["command"] == "echo traced-ok"
    assert plan["mutating"] is True


@pytest.mark.asyncio
async def test_shell_step_executes_and_reports_output() -> None:
    done = await run_task("run the shell command `echo traced-ok`", approved=True)
    assert done["ok"] is True
    assert "traced-ok" in done["spoken"]


@pytest.mark.asyncio
async def test_shell_step_refuses_destructive_commands() -> None:
    done = await run_task("run the shell command `rm -rf /`", approved=True)
    assert done["ok"] is False


@pytest.mark.asyncio
async def test_injected_runner_proves_open_step_loop() -> None:
    seen: list[str] = []

    async def fake_runner(step: dict) -> dict:
        seen.append(str(step["kind"]))
        return {"ok": True, "executed": True, "verified": True, "spoken": "ok"}

    plan = {
        "goal": "anything",
        "mutating": False,
        "steps": [
            {"kind": "web_search", "label": "look it up", "mutating": False},
            {"kind": "gui", "label": "click through", "mutating": False},
            {"kind": "shell", "label": "run it", "mutating": False},
        ],
    }
    done = await run_task("anything", runner=fake_runner, plan=plan)
    assert done["ok"] is True
    assert seen == ["web_search", "gui", "shell"]
