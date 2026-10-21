"""Agent HTTP surface (additive): start, watch, approve, cancel local agent runs.

Master-key only. The runtime itself lives in :mod:`app.agent`; this module is a
thin adapter over the in-process run registry and the persisted trace store, so
there is exactly one engine behind ``python -m app.agent`` and ``ev agent``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent import store
from app.agent.brain import brain_status
from app.agent.service import SERVICE
from app.auth import require_master
from app.config import settings

router = APIRouter(prefix="/v1/agent", tags=["agent"])


class RunIn(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    approved: bool = False
    root: str | None = Field(default=None, max_length=512)
    context: str = Field(default="", max_length=8000)
    provider: str | None = Field(default=None, max_length=32)
    max_steps: int | None = Field(default=None, ge=1, le=64)
    max_seconds: int | None = Field(default=None, ge=5, le=3600)
    wait: bool = False
    wait_timeout_s: float = Field(default=120.0, ge=1.0, le=1800.0)


class DecisionIn(BaseModel):
    decision: Literal["approve", "approve_run", "deny"] = "approve"


class BrainOut(BaseModel):
    provider_setting: str
    auto_pick: dict[str, Any]
    candidates: dict[str, Any]


def _require_enabled() -> None:
    if not bool(getattr(settings, "agent_enabled", True)):
        raise HTTPException(status_code=404, detail="agent runtime is disabled (EV_AGENT_ENABLED=0)")


@router.get("/brain", response_model=BrainOut)
async def agent_brain(_: str = Depends(require_master)) -> dict[str, Any]:
    _require_enabled()
    return brain_status()


@router.post("/runs")
async def start_run(data: RunIn, actor: str = Depends(require_master)) -> dict[str, Any]:
    _require_enabled()
    handle = SERVICE.start(
        data.goal,
        approved=data.approved,
        root=data.root,
        max_steps=data.max_steps,
        max_seconds=data.max_seconds,
        provider_name=data.provider,
        context=data.context,
    )
    if data.wait:
        snapshot = await SERVICE.wait(handle.run_id, timeout=data.wait_timeout_s)
        return snapshot or handle.snapshot()
    return handle.snapshot(events=0)


@router.get("/runs")
async def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    _: str = Depends(require_master),
) -> dict[str, Any]:
    _require_enabled()
    live = SERVICE.live(limit=limit)
    persisted = store.list_runs(limit=limit)
    seen = {row.get("run_id") for row in live}
    return {
        "live": live,
        "persisted": [row for row in persisted if row.get("run_id") not in seen],
    }


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    events: int = Query(default=100, ge=0, le=500),
    _: str = Depends(require_master),
) -> dict[str, Any]:
    _require_enabled()
    live = SERVICE.snapshot(run_id, events=events)
    if live is not None:
        return live
    persisted = store.load_run(run_id)
    if persisted is None:
        raise HTTPException(status_code=404, detail="run not found")
    return persisted


@router.post("/runs/{run_id}/decision")
async def decide_run(
    run_id: str,
    data: DecisionIn,
    _: str = Depends(require_master),
) -> dict[str, Any]:
    _require_enabled()
    if not SERVICE.approve(run_id, data.decision):
        raise HTTPException(status_code=409, detail="no approval is waiting for that run")
    return {"ok": True, "run_id": run_id, "decision": data.decision}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, _: str = Depends(require_master)) -> dict[str, Any]:
    _require_enabled()
    if not SERVICE.cancel(run_id):
        raise HTTPException(status_code=404, detail="run not found")
    return {"ok": True, "run_id": run_id, "status": "cancelled"}


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    _: str = Depends(require_master),
) -> StreamingResponse:
    _require_enabled()
    handle = SERVICE.get(run_id)
    if handle is None:
        persisted = store.load_run(run_id)
        if persisted is None:
            raise HTTPException(status_code=404, detail="run not found")

        async def replay() -> Any:
            yield f"event: result\ndata: {json.dumps(persisted, default=str)}\n\n"

        return StreamingResponse(replay(), media_type="text/event-stream")

    async def pump() -> Any:
        index = 0
        while True:
            events = list(handle.events)
            while index < len(events):
                event = events[index]
                index += 1
                yield f"event: {event.get('kind', 'message')}\ndata: {json.dumps(event, default=str)}\n\n"
            if handle.status != "running" and index >= len(list(handle.events)):
                snapshot = handle.snapshot(events=0)
                yield f"event: result\ndata: {json.dumps(snapshot, default=str)}\n\n"
                return
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(pump(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# Durable task lane (survives the turn; readable after a restart)
# --------------------------------------------------------------------------- #


@router.get("/tasks")
async def list_tasks(
    limit: int = Query(default=20, ge=1, le=100),
    _: str = Depends(require_master),
) -> dict[str, Any]:
    _require_enabled()
    from app.agent import ledger, runner

    active = ledger.list_tasks(active=True, limit=limit)
    recent = ledger.list_tasks(limit=limit)
    return {
        "active": active,
        "recent": [row for row in recent if row["status"] not in ledger.ACTIVE_STATUSES],
        "pending_approval": runner.pending_approval(),
        "status_line": runner.status_reply(),
    }


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, _: str = Depends(require_master)) -> dict[str, Any]:
    _require_enabled()
    from app.agent import ledger

    row = ledger.get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return row


@router.post("/tasks/{task_id}/decision")
async def decide_task(
    task_id: str,
    data: DecisionIn,
    _: str = Depends(require_master),
) -> dict[str, Any]:
    _require_enabled()
    from app.agent import runner

    row = runner.approve_task(task_id, data.decision)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True, "task_id": task_id, "decision": data.decision, "task": row}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, _: str = Depends(require_master)) -> dict[str, Any]:
    _require_enabled()
    from app.agent import runner

    row = runner.cancel_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"ok": True, "task_id": task_id, "status": row["status"], "task": row}
