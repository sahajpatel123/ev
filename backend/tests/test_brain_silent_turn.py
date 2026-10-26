"""Background results are never silent (0.4/B5).

Regression: "Clean up the old report." answered with progress
("On it — running search_files ...") and then nothing on the asking
channel — the task id was dropped from the turn response, and the
finished result only reached voice/bus announcements, never the thread.
Now the turn response carries the task handle, and a background
completion is recorded as an assistant event in the thread.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.contracts import ChatResult, ToolCall
from app.models import Event


class ScriptedProvider:
    name = "scripted"

    def __init__(self, turns: list[ChatResult], *, delay: float = 0.0) -> None:
        self.turns = deque(turns)
        self.delay = delay
        self.calls = 0

    async def chat_with_tools(self, messages, tools, **kwargs):
        del messages, tools, kwargs
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.turns:
            return self.turns.popleft()
        return ChatResult(text="nothing left", model="scripted")


def _turn(*calls: ToolCall, text: str = "") -> ChatResult:
    return ChatResult(text=text, tool_calls=list(calls), usage={}, model="scripted")


def _finish(summary: str) -> ToolCall:
    return ToolCall(
        id="finish-1", name="finish", arguments={"summary": summary, "ok": True}
    )


def _text(row) -> str:
    content = row.content or {}
    return str(content.get("text") or "")


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

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "old-report.txt").write_text("stale\n")
    monkeypatch.setattr(settings, "agent_state_db", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(settings, "agent_root", str(workspace))
    ledger.init()

    async def fake_announce(title: str, body: str, *, kind: str = "agent") -> None:
        del title, body, kind

    import app.agent.notify as notify

    monkeypatch.setattr(notify, "announce", fake_announce)
    yield workspace
    runner._MIRRORS.clear()


def _install_provider(monkeypatch, provider: ScriptedProvider) -> None:
    import app.agent.loop as loop
    from app.agent.brain import BrainChoice

    choice = BrainChoice("scripted", "scripted", "test", True)
    monkeypatch.setattr("app.agent.service.resolve_agent_brain", lambda *a, **k: choice)
    monkeypatch.setattr(loop, "build_agent_provider", lambda c: provider)


async def _wait_for_thread_event(
    db_session: AsyncSession, task_id: str, *, timeout: float = 15.0
):
    """Poll the event ledger for the background completion (mirror is async)."""

    from app.models import Event as EventModel

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        rows = (
            await db_session.execute(
                select(EventModel).where(
                    EventModel.event_type == "message.assistant"
                )
            )
        ).scalars().all()
        for row in rows:
            meta = row.metadata_ or {}
            if isinstance(meta, dict) and meta.get("task_id") == task_id:
                return row
        await asyncio.sleep(0.25)
    return None


async def test_cleanup_turn_returns_a_task_handle_then_delivers_to_thread(
    _kernel_mode, _isolated_lane, monkeypatch, db_session: AsyncSession
) -> None:
    from app.agent import runner
    from app.cognitive.kernel import handle_turn

    provider = ScriptedProvider(
        [
            _turn(
                ToolCall(
                    id="c1",
                    name="search_files",
                    arguments={"query": "old report"},
                )
            ),
            _turn(_finish("Cleaned up the old report.")),
        ],
        delay=2.0,
    )
    _install_provider(monkeypatch, provider)
    # In production the routing model sends this utterance to the task lane;
    # pin that decision so the repro is deterministic (routing itself is
    # covered by the intake classifier tests).
    import app.agent.intake as intake_module

    async def _route_to_task(raw: str, **kwargs) -> str:
        del raw, kwargs
        return "task"

    monkeypatch.setattr(intake_module, "route_goal", _route_to_task)
    # The inline wait ends while the run is still working: this is the
    # background path that used to go silent.
    monkeypatch.setattr(settings, "agent_chat_inline_seconds", 1.0)

    result = await handle_turn(
        transcript="Clean up the old report.",
        modality="typed",
        session=db_session,
    )
    assert result.kind == "agent_task", result.kind
    assert "On it" in result.spoken
    assert result.task_id, "the turn must carry the background task handle"
    body = result.as_dict()
    assert body["task_id"] == result.task_id
    assert body["task_status"] in {"running", "awaiting_approval"}

    done = await runner.wait_task(result.task_id, timeout=15)
    assert done["status"] == "completed"

    event = await _wait_for_thread_event(db_session, result.task_id)
    assert event is not None, "background result never reached the thread"
    assert "Cleaned up the old report." in _text(event)


async def test_announce_final_records_completed_and_failed(
    _isolated_lane, monkeypatch, db_session: AsyncSession
) -> None:
    from app.agent import ledger, runner

    announced: list[tuple[str, str]] = []

    async def fake_announce(title: str, body: str, *, kind: str = "agent") -> None:
        announced.append((title, body))

    import app.agent.notify as notify

    monkeypatch.setattr(notify, "announce", fake_announce)

    good = ledger.create(goal="sort demos", source="chat", boot_id=runner.BOOT_ID)
    ledger.update(good["id"], status="completed", summary="Sorted two demos.")
    await runner._announce_final(ledger.get(good["id"]))

    bad = ledger.create(goal="fix demos", source="chat", boot_id=runner.BOOT_ID)
    ledger.update(bad["id"], status="failed", error="no demos found")
    await runner._announce_final(ledger.get(bad["id"]))

    rows = (
        await db_session.execute(
            select(Event).where(Event.event_type == "message.assistant")
        )
    ).scalars().all()
    by_task = {}
    for row in rows:
        meta = row.metadata_ or {}
        if isinstance(meta, dict) and meta.get("task_id"):
            by_task[meta["task_id"]] = row
    assert "Sorted two demos." in _text(by_task[good["id"]])
    assert "failed" in _text(by_task[bad["id"]]).lower()
    # Voice/bus announcement still fires alongside the thread delivery.
    assert len(announced) == 2


async def test_inline_finished_tasks_do_not_double_deliver(
    _isolated_lane, monkeypatch, db_session: AsyncSession
) -> None:
    from app.agent import intake, runner

    provider = ScriptedProvider([_turn(_finish("Cleaned the folder."))])
    _install_provider(monkeypatch, provider)
    out = await intake.handle_task(
        "clean up my downloads folder", source="chat", inline_wait=10
    )
    assert out is not None and out["ok"] is True
    # The turn consumed the result; the mirror must stay quiet.
    await asyncio.sleep(1.5)
    rows = (
        await db_session.execute(
            select(Event).where(Event.event_type == "message.assistant")
        )
    ).scalars().all()
    assert [
        row for row in rows
        if ((row.metadata_ or {}).get("task_id") == out["task_id"])
    ] == []
    runner._MIRRORS.clear()
