"""Voice transcripts reach the durable agent lane (control + tasks).

Regression: in realtime_delegate topology, work-shaped voice turns went to
the brain-judge computer broker, which looped open_app with
verification_failed on Ghostty and never split panes or ran commands —
while the proven agent lane was never consulted. The bridge below is the
contract: control turns answer from the ledger, task goals run the agent
loop, everything else falls through to the existing brokers.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.agent import ledger, runner
from app.config import settings


@pytest.fixture()
def _isolated_ledger(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "agent_state_db", str(tmp_path / "tasks.db"))
    ledger.init()
    yield tmp_path
    runner._MIRRORS.clear()


def _live():
    from app.voice.live.session import LiveSession

    return LiveSession(session_id=str(uuid4()))


def _capture_speech(monkeypatch, live) -> list:
    spoken: list[str] = []

    async def fake_speak(self, text, **kwargs):
        del kwargs
        spoken.append(str(text))

    monkeypatch.setattr(type(live), "speak_honesty", fake_speak)
    return spoken


@pytest.mark.asyncio
async def test_non_task_transcript_falls_through(_isolated_ledger, monkeypatch) -> None:
    live = _live()
    spoken = _capture_speech(monkeypatch, live)
    consumed = await live._maybe_agent_lane("what is the weather today", from_live=False)
    assert consumed is False
    assert spoken == []


@pytest.mark.asyncio
async def test_status_turn_answers_from_ledger(_isolated_ledger, monkeypatch) -> None:
    live = _live()
    spoken = _capture_speech(monkeypatch, live)
    row = ledger.create(goal="organize the desktop", source="voice", boot_id=runner.BOOT_ID)
    assert row["status"] == "running"
    consumed = await live._maybe_agent_lane("what are you working on", from_live=False)
    assert consumed is True
    assert spoken and "organize the desktop" in spoken[0]


@pytest.mark.asyncio
async def test_yes_resumes_parked_task_on_voice(_isolated_ledger, monkeypatch) -> None:
    import os

    import app.ev.continuity as continuity

    live = _live()
    spoken = _capture_speech(monkeypatch, live)
    monkeypatch.setattr(continuity, "is_affirmative_reply", lambda text: text.strip().lower().startswith("yes"))
    # run_id set: a durable run parks here (no run_id would route to the
    # deterministic no-brain plan path instead).
    parked = ledger.create(goal="write note.txt", source="voice", boot_id=runner.BOOT_ID,
                           pid=os.getpid(), run_id="run-test-voice-1")
    ledger.update(parked["id"], status="awaiting_approval", approval_tool="write_file")
    answers: list[tuple[str, str]] = []

    async def fake_wait(task_id: str, timeout: float):
        answers.append((task_id, "waited"))
        return {"id": task_id, "status": "completed", "summary": "Wrote note.txt."}

    monkeypatch.setattr(runner, "wait_task", fake_wait)
    monkeypatch.setattr(runner, "approve_task", lambda task_id, decision: answers.append((task_id, decision)))
    consumed = await live._maybe_agent_lane("yes, go ahead", from_live=False)
    assert consumed is True
    assert (parked["id"], "approve_run") in answers
    assert spoken and "Wrote note.txt." in spoken[0]


@pytest.mark.asyncio
async def test_task_goal_runs_and_speaks(_isolated_ledger, monkeypatch) -> None:
    import app.agent.intake as intake_module

    live = _live()
    spoken = _capture_speech(monkeypatch, live)
    progressed: list[str] = []

    async def fake_progress(self, name: str, **kwargs):
        del kwargs
        progressed.append(name)

    monkeypatch.setattr(type(live), "push_progress", fake_progress)

    async def fake_handle_task(text, **kwargs):
        del kwargs
        assert "ghostty" in text.lower()
        return {"spoken": "On it — opening the terminal.", "kind": "agent_task",
                "task_id": "task-1", "ok": True}

    monkeypatch.setattr(intake_module, "handle_task", fake_handle_task)
    consumed = await live._maybe_agent_lane(
        "launch ghostty and run those commands", from_live=False
    )
    assert consumed is True
    assert progressed == ["agent"]
    assert spoken == ["On it — opening the terminal."]


@pytest.mark.asyncio
async def test_lane_failure_falls_through(_isolated_ledger, monkeypatch) -> None:
    import app.agent.intake as intake_module

    live = _live()
    spoken = _capture_speech(monkeypatch, live)

    async def boom(text, **kwargs):
        raise RuntimeError("lane exploded")

    monkeypatch.setattr(intake_module, "handle_control", boom)
    monkeypatch.setattr(intake_module, "handle_task", boom)
    consumed = await live._maybe_agent_lane("do something complicated", from_live=False)
    assert consumed is False
    assert spoken == []
