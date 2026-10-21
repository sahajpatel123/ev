"""Durable task lane: ledger, runner, intake, and the no-phantom-work contract.

Hermetic: a scripted provider stands in for the brain, the ledger lives in a
tmp SQLite file, and announcements are stubbed. The tests pin the behaviours
that fix "submitted, I'll inform you":

- a task row exists before a run starts;
- every step and parked approval is mirrored into the ledger;
- status/cancel/approval answer from the ledger without a model;
- a process restart marks orphans interrupted instead of "running" forever;
- the explain fast path no longer hijacks an action goal.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

import pytest

from app.agent import intake, ledger, runner
from app.agent.brain import BrainChoice
from app.agent.loop import AgentLimits
from app.config import settings
from app.contracts import ChatResult, ToolCall


class ScriptedProvider:
    name = "scripted"
    supports_media = False

    def __init__(self, turns: list[ChatResult], *, delay: float = 0.0) -> None:
        self.turns = deque(turns)
        self.delay = delay
        self.seen: list[list] = []

    async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7):
        del tools, model, temperature
        self.seen.append(list(messages))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.turns:
            return self.turns.popleft()
        return ChatResult(text="nothing left", model="scripted")

    async def chat(self, messages, *, model=None, temperature=0.7):
        return await self.chat_with_tools(messages, [])


def _turn(*calls: ToolCall, text: str = "") -> ChatResult:
    return ChatResult(text=text, tool_calls=list(calls), usage={}, model="scripted")


def _finish(summary: str = "done", ok: bool = True) -> ToolCall:
    return ToolCall(id="finish-1", name="finish", arguments={"summary": summary, "ok": ok})


@pytest.fixture(autouse=True)
def _isolated_lane(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "agent_state_db", str(tmp_path / "tasks.db"))
    ledger.init()
    announced: list[tuple[str, str]] = []

    async def fake_announce(title: str, body: str, *, kind: str = "agent") -> None:
        announced.append((title, body))

    import app.agent.notify as notify

    monkeypatch.setattr(notify, "announce", fake_announce)
    yield announced
    runner._MIRRORS.clear()


def _install_provider(monkeypatch, provider: ScriptedProvider) -> None:
    import app.agent.loop as loop

    choice = BrainChoice("scripted", "scripted", "test", True)
    monkeypatch.setattr("app.agent.service.resolve_agent_brain", lambda *a, **k: choice)
    monkeypatch.setattr(loop, "build_agent_provider", lambda c: provider)


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


def test_ledger_roundtrip_and_orphan_reconcile(tmp_path: Path) -> None:
    row = ledger.create(goal="clean downloads", source="chat", boot_id="old-boot")
    assert ledger.get(row["id"])["status"] == "running"
    changed = ledger.mark_orphans("new-boot")
    assert changed == 1
    fresh = ledger.get(row["id"])
    assert fresh["status"] == "interrupted"
    assert "process running" in (fresh["error"] or "")


def test_status_reply_reads_the_ledger() -> None:
    row = ledger.create(goal="organize the desktop", source="chat", boot_id=runner.BOOT_ID)
    ledger.update(row["id"], last_event="finished list_dir")
    text = runner.status_reply()
    assert "organize the desktop" in text
    assert "Right now" in text
    ledger.update(row["id"], status="completed", summary="organized")
    assert "Nothing is running" in runner.status_reply()


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_submit_task_mirrors_to_completion(monkeypatch) -> None:
    provider = ScriptedProvider([
        _turn(ToolCall(id="c1", name="list_dir", arguments={})),
        _turn(_finish("Looked at the folder.")),
    ])
    _install_provider(monkeypatch, provider)
    row = runner.submit_task("look at the folder and tell me", source="chat")
    assert row["status"] == "running"
    done = await runner.wait_task(row["id"], timeout=10)
    assert done["status"] == "completed"
    assert done["summary"] == "Looked at the folder."
    assert done["steps"] >= 1
    assert done["last_event"]


@pytest.mark.asyncio
async def test_task_parks_for_approval_then_resumes(monkeypatch, tmp_path: Path) -> None:
    provider = ScriptedProvider([
        _turn(ToolCall(id="c1", name="write_file",
                       arguments={"path": "out.txt", "content": "hello"})),
        _turn(_finish("Wrote out.txt.")),
    ])
    _install_provider(monkeypatch, provider)
    row = runner.submit_task(
        "write out.txt", source="chat", root=str(tmp_path), approved=False
    )
    parked = await _wait_for_status(row["id"], {"awaiting_approval"}, timeout=10)
    assert parked["status"] == "awaiting_approval"
    assert parked["approval_tool"] == "write_file"
    assert not (tmp_path / "out.txt").exists()

    runner.approve_task(row["id"], "approve_run")
    done = await runner.wait_task(row["id"], timeout=10)
    assert done["status"] == "completed"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_cancel_task_marks_the_row_cancelled(monkeypatch) -> None:
    provider = ScriptedProvider([_turn(_finish("never"))], delay=30.0)
    _install_provider(monkeypatch, provider)
    row = runner.submit_task("do something slow", source="chat")
    await asyncio.sleep(0.1)
    runner.cancel_task(row["id"])
    await asyncio.sleep(0.2)
    assert ledger.get(row["id"])["status"] == "cancelled"


async def _wait_for_status(task_id: str, wanted: set[str], *, timeout: float) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        row = ledger.get(task_id)
        if row and row["status"] in wanted:
            return row
        await asyncio.sleep(0.1)
    raise AssertionError(f"task {task_id} never reached {wanted}: {ledger.get(task_id)}")


# --------------------------------------------------------------------------- #
# Intake classification
# --------------------------------------------------------------------------- #


def test_general_task_classifier() -> None:
    assert intake.looks_like_general_task(
        "clean up my Downloads folder and sort the files into subfolders"
    )
    assert intake.looks_like_general_task(
        "find every text file in my Documents and summarize them"
    )
    assert intake.looks_like_general_task(
        "create a folder called Trips then move the tickets into it"
    )
    assert intake.looks_like_general_task(
        "Create a folder called http-demo in the workspace and write a note inside it saying hello."
    )
    assert intake.looks_like_general_task(
        "run the test suite and fix the first failure"
    )
    assert intake.looks_like_general_task(
        "build a website with a contact form and deploy it"
    )
    assert not intake.looks_like_general_task("keep working on the site overnight")
    # DECISION (routing contract): single-shot LOCAL turns run the agent lane.
    # test_local_route.py — green, owner phrasings incl. the Ghostty example —
    # governs local routing: "open my downloads folder" and "what files are on
    # my desktop" must reach hands, not narration. The kernel fast paths
    # dead-end on local work, so these four moved here from the fast-path list.
    for text in (
        "open my Downloads folder",
        "what files are in my Documents folder",
    ):
        assert intake.looks_like_general_task(text), text
    # Dedicated lanes stay authoritative: software artifacts go to code.act,
    # and a single create/write of one file keeps the file fast path.
    for text in (
        "write a python script that prints hello world",
        "make a packing list on my Desktop",
    ):
        assert not intake.looks_like_general_task(text), text
    # Known single NON-LOCAL capabilities keep their fast paths.
    for text in (
        "what is the weather today",
        "remind me to call the dentist",
        "text Sam that I am late",
        "give me info about the wish project",
    ):
        assert not intake.looks_like_general_task(text), text


def test_explain_does_not_hijack_an_action_goal() -> None:
    from app.ev.explain import looks_like_explain_ask

    assert not looks_like_explain_ask(
        "find every text file in the folder I am working in and tell me what is in each one"
    )
    assert looks_like_explain_ask("analyze the wish project")


# --------------------------------------------------------------------------- #
# Intake control + task turns
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_intake_status_and_cancel_without_a_model() -> None:
    row = ledger.create(goal="sort the desktop", source="chat", boot_id=runner.BOOT_ID)
    status = await intake.handle_control("what are you doing?")
    assert status is not None and "sort the desktop" in status["spoken"]
    cancelled = await intake.handle_control("cancel that task")
    assert cancelled is not None and cancelled["kind"] == "agent_cancel"
    assert ledger.get(row["id"])["status"] == "cancelled"


@pytest.mark.asyncio
async def test_intake_task_runs_inline_when_fast(monkeypatch) -> None:
    provider = ScriptedProvider([_turn(_finish("Cleaned the folder."))])
    _install_provider(monkeypatch, provider)
    out = await intake.handle_task(
        "clean up my downloads folder", source="chat", inline_wait=10
    )
    assert out is not None
    assert out["ok"] is True
    assert "Cleaned the folder." in out["spoken"]
    assert ledger.get(out["task_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_intake_yes_resumes_a_parked_task(monkeypatch, tmp_path: Path) -> None:
    provider = ScriptedProvider([
        _turn(ToolCall(id="c1", name="write_file",
                       arguments={"path": "note.txt", "content": "hi"})),
        _turn(_finish("Wrote note.txt.")),
    ])
    _install_provider(monkeypatch, provider)
    # This test pins the approval mechanism, so it selects confirm mode
    # explicitly instead of depending on the trusted product default.
    monkeypatch.setattr(settings, "agent_autonomy", "confirm")
    row = runner.submit_task("write note.txt", source="chat", root=str(tmp_path))
    await _wait_for_status(row["id"], {"awaiting_approval"}, timeout=10)

    out = await intake.handle_control("yes, go ahead", inline_wait=10)
    assert out is not None
    assert "Wrote note.txt." in out["spoken"]
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "hi"
    assert ledger.get(row["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_intake_yes_without_a_parked_task_is_not_ours() -> None:
    assert await intake.handle_control("yes") is None


def test_limits_are_bounded() -> None:
    limits = AgentLimits.from_settings()
    assert 1 <= limits.max_steps <= 64
    assert 5 <= limits.max_seconds <= 3600


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_api_tasks_list_get_and_cancel(client, monkeypatch) -> None:
    row = ledger.create(goal="sort the desktop icons", source="chat", boot_id=runner.BOOT_ID)
    ledger.update(row["id"], last_event="finished list_dir")

    listed = await client.get("/v1/agent/tasks")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert any(item["id"] == row["id"] for item in body["active"])
    assert body["status_line"]

    fetched = await client.get(f"/v1/agent/tasks/{row['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["goal"] == "sort the desktop icons"

    cancelled = await client.post(f"/v1/agent/tasks/{row['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    missing = await client.get("/v1/agent/tasks/task-does-not-exist")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_legacy_chat_pipeline_uses_the_task_lane(
    client, monkeypatch, tmp_path: Path
) -> None:
    """The production mode (kernel off) still gets real task execution."""
    provider = ScriptedProvider([
        _turn(ToolCall(id="c1", name="write_file",
                       arguments={"path": "legacy-demo/note.txt", "content": "hello"})),
        _turn(_finish("Wrote legacy-demo/note.txt and verified it.")),
    ])
    _install_provider(monkeypatch, provider)
    monkeypatch.setattr(settings, "cognitive_mode", "realtime_delegate")
    monkeypatch.setattr(settings, "agent_root", str(tmp_path))
    # Pin confirm mode: this test asserts the park-and-resume mechanism,
    # while the product default is trusted (no parking for in-root writes).
    monkeypatch.setattr(settings, "agent_autonomy", "confirm")

    first = await client.post(
        "/v1/chat",
        json={"message": "Create a folder called legacy-demo and write a note inside it saying hello."},
    )
    assert first.status_code == 200, first.text
    reply = first.json()["reply"]
    assert "task-" in reply
    assert "approval" in reply.lower() or "paused" in reply.lower()

    second = await client.post("/v1/chat", json={"message": "yes, go ahead"})
    assert second.status_code == 200, second.text
    assert "Wrote legacy-demo/note.txt" in second.json()["reply"]
    assert (tmp_path / "legacy-demo" / "note.txt").read_text(encoding="utf-8") == "hello"


def _open_fd_count() -> int:
    import os

    for path in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(path):
            return len(os.listdir(path))
    return -1


def test_ledger_does_not_leak_file_descriptors() -> None:
    """Every ledger call must close its sqlite connection (Errno 24 guard)."""
    row = ledger.create(goal="fd leak probe", source="chat", boot_id=runner.BOOT_ID)
    before = _open_fd_count()
    for index in range(200):
        ledger.update(row["id"], last_event=f"step {index}")
        ledger.get(row["id"])
        ledger.list_tasks(limit=5)
    after = _open_fd_count()
    assert before > 0 and after - before <= 3, (before, after)


# --------------------------------------------------------------------------- #
# Cross-process control (ev.api :8000 vs ev.talk :18000)
# --------------------------------------------------------------------------- #


def test_cross_process_decision_is_written_to_the_ledger(monkeypatch) -> None:
    row = ledger.create(goal="cross process", source="chat", boot_id=runner.BOOT_ID)
    ledger.update(row["id"], run_id="run-other", status="awaiting_approval",
                  pid=__import__("os").getpid())
    monkeypatch.setattr(runner.SERVICE, "get", lambda run_id: None)
    updated = runner.approve_task(row["id"], "approve_run")
    assert updated is not None and updated["decision"] == "approve_run"


def test_apply_pending_control_calls_the_owner_gate(monkeypatch) -> None:
    from types import SimpleNamespace

    row = ledger.create(goal="apply control", source="chat", boot_id=runner.BOOT_ID)
    ledger.update(row["id"], run_id="run-local", status="awaiting_approval")
    ledger.update(row["id"], decision="approve_run")
    calls: list[tuple] = []
    monkeypatch.setattr(runner.SERVICE, "approve", lambda run_id, decision: calls.append((run_id, decision)))
    handle = SimpleNamespace(
        run_id="run-local", status="running",
        gate=SimpleNamespace(waiting=True),
    )
    runner.apply_pending_control(row["id"], handle)
    assert calls == [("run-local", "approve_run")]
    assert not ledger.get(row["id"])["decision"]


def test_cross_process_cancel_requests_the_owner(monkeypatch) -> None:
    row = ledger.create(goal="cancel elsewhere", source="chat", boot_id=runner.BOOT_ID)
    ledger.update(row["id"], run_id="run-other", status="running",
                  pid=__import__("os").getpid())
    monkeypatch.setattr(runner.SERVICE, "get", lambda run_id: None)
    runner.cancel_task(row["id"])
    assert ledger.get(row["id"])["cancel_requested"] == 1


def test_progress_language_never_promises_or_defers() -> None:
    banned = ("tell you", "as soon as", "submitted", "sent to", "queued",
              "in the background", "check later", "moment it's done")
    for source in ("voice", "phone", "chat"):
        row = {
            "id": "task-x", "goal": "open ghostty", "source": source,
            "status": "running", "last_event": "running terminal_open",
        }
        text = runner.progress_text(row).lower()
        assert not any(word in text for word in banned), (source, text)
        assert text.startswith("on it")
    vague = {
        "id": "task-y", "goal": "open ghostty", "source": "voice",
        "status": "running", "last_event": "thinking about the next step",
    }
    assert runner.progress_text(vague) == "On it."
