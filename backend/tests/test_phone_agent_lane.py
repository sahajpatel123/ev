"""Phone requests become real Home Station work, not broker rows.

The mobile lane must map a durable task's real status to the phone contract:
a completed run is COMPLETED with its summary, a live run is PARTIAL with a
delivery scheduled, and a non-task turn returns None so the existing
conversation path keeps it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.device_gateway import pipeline


def _device() -> SimpleNamespace:
    return SimpleNamespace(id="00000000-0000-0000-0000-0000000000aa", name="iPhone")


@pytest.mark.asyncio
async def test_completed_task_returns_the_real_result(monkeypatch) -> None:
    async def fake_handle_task(text: str, **_: Any) -> dict[str, Any]:
        return {"spoken": "Created ev.txt on the Desktop.", "ok": True, "task_id": "task-1"}

    monkeypatch.setattr("app.agent.intake.handle_task", fake_handle_task)
    monkeypatch.setattr(
        "app.agent.ledger.get",
        lambda task_id: {"id": task_id, "status": "completed", "summary": "Created ev.txt."},
    )
    out = await pipeline._phone_agent_lane(_device(), "create ev.txt on my desktop")
    assert out is not None
    assert out["action_result"] == "COMPLETED"
    assert out["executed"] is True and out["verified"] is True
    assert out["reply"] == "Created ev.txt on the Desktop."
    assert out["route"] == "AGENT_TASK"


@pytest.mark.asyncio
async def test_running_task_is_partial_and_schedules_delivery(monkeypatch) -> None:
    delivered: list[tuple[str, str, str]] = []

    async def fake_handle_task(text: str, **_: Any) -> dict[str, Any]:
        return {"spoken": "On it — opening Ghostty.", "ok": True, "task_id": "task-2"}

    async def fake_deliver(device_id: str, task_id: str, goal: str) -> None:
        delivered.append((device_id, task_id, goal))

    monkeypatch.setattr("app.agent.intake.handle_task", fake_handle_task)
    monkeypatch.setattr(
        "app.agent.ledger.get",
        lambda task_id: {"id": task_id, "status": "running", "summary": ""},
    )
    monkeypatch.setattr(pipeline, "_deliver_phone_task", fake_deliver)
    out = await pipeline._phone_agent_lane(_device(), "open Ghostty and run my session")
    assert out is not None
    assert out["action_result"] == "PARTIAL"
    assert out["executed"] is False
    # Let the scheduled delivery task run.
    import asyncio

    await asyncio.sleep(0)
    assert delivered and delivered[0][1] == "task-2"


@pytest.mark.asyncio
async def test_non_task_turn_stays_with_the_conversation(monkeypatch) -> None:
    async def fake_handle_task(text: str, **_: Any) -> None:
        return None

    monkeypatch.setattr("app.agent.intake.handle_task", fake_handle_task)
    assert await pipeline._phone_agent_lane(_device(), "what is the weather") is None


@pytest.mark.asyncio
async def test_finished_task_is_pushed_to_the_phone_inbox(monkeypatch) -> None:
    pushed: list[dict[str, Any]] = []
    touched: list[tuple[str, dict[str, Any]]] = []

    async def fake_wait(task_id: str, timeout: float = 0) -> dict[str, Any]:
        return {"id": task_id, "status": "completed", "summary": "Opened Ghostty and ran it."}

    async def fake_push(session: Any, **kwargs: Any) -> dict[str, Any]:
        pushed.append(kwargs)
        return {"id": "inbox-1"}

    class DummySession:
        async def __aenter__(self) -> DummySession:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def commit(self) -> None:
            return None

    monkeypatch.setattr("app.agent.runner.wait_task", fake_wait)
    monkeypatch.setattr("app.db.SessionLocal", lambda: DummySession())
    monkeypatch.setattr("app.everywhere.inbox.push_inbox", fake_push)
    monkeypatch.setattr(
        "app.agent.ledger.update",
        lambda task_id, **fields: touched.append((task_id, fields)),
    )

    await pipeline._deliver_phone_task("dev-1", "task-9", "open Ghostty")
    assert pushed and pushed[0]["device_id"] == "dev-1"
    assert pushed[0]["kind"] == "task_finished"
    assert "Opened Ghostty" in pushed[0]["body"]
    assert pushed[0]["payload"]["task_id"] == "task-9"
    assert touched == [("task-9", {"notified": 1})]

