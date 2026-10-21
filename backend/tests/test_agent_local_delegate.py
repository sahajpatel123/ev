"""Voice delegate -> durable local agent lane (DeepSeek/main brain path)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cognitive import delegation
from app.models import ResearchSession


@pytest.mark.asyncio
async def test_local_delegate_finishes_with_the_agent_result(
    db_session: AsyncSession, monkeypatch
) -> None:
    job_id = uuid.uuid4()
    row = ResearchSession(
        id=job_id,
        owner="master",
        mode=delegation._TAG,
        status="running",
        question="open ghostty and run echo OK",
        goal="open ghostty and run echo OK",
        budget={"live_session_id": None, "device_id": None},
    )
    db_session.add(row)
    await db_session.commit()

    async def fake_handle_task(task, *, source="chat", approved=None, inline_wait=None):
        assert source == "voice"
        return {"task_id": "task-delegate", "spoken": "On it.", "ok": True}

    async def fake_wait_task(task_id, timeout):
        assert task_id == "task-delegate"
        return {"status": "completed", "summary": "Ghostty printed OK."}

    from app.agent import intake, runner

    monkeypatch.setattr(intake, "handle_task", fake_handle_task)
    monkeypatch.setattr(runner, "wait_task", fake_wait_task)

    receipt = await delegation._run_local_lane(job_id, "open ghostty", binding={})
    assert receipt is not None
    assert receipt["status"] == "answered"
    assert "Ghostty printed OK." in receipt["spoken"]

    await db_session.refresh(row)
    assert row.status == "answered"
    assert "Ghostty printed OK." in (row.conclusion or "")


@pytest.mark.asyncio
async def test_local_delegate_parks_an_agent_approval(
    db_session: AsyncSession, monkeypatch
) -> None:
    job_id = uuid.uuid4()
    row = ResearchSession(
        id=job_id, owner="master", mode=delegation._TAG, status="running",
        question="write a file", goal="write a file", budget={},
    )
    db_session.add(row)
    await db_session.commit()

    async def fake_handle_task(task, *, source="chat", approved=None, inline_wait=None):
        return {"task_id": "task-approval", "spoken": "Say yes to continue.",
                "ok": False, "needs_confirm": True}

    async def fake_wait_task(task_id, timeout):
        return {"status": "awaiting_approval"}

    from app.agent import intake, runner

    monkeypatch.setattr(intake, "handle_task", fake_handle_task)
    monkeypatch.setattr(runner, "wait_task", fake_wait_task)

    receipt = await delegation._run_local_lane(job_id, "write a file", binding={})
    assert receipt is not None and receipt["status"] == "waiting"
    await db_session.refresh(row)
    assert (row.budget or {}).get("agent_task_id") == "task-approval"
