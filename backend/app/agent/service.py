"""Agent run service: background runs, approvals, cancellation, snapshots.

This is the in-process registry the HTTP surface and the CLI talk to. A run is
an asyncio task; the owner can watch it, answer its approval requests, or
cancel it from any request. Finished runs are persisted to the trace store so
a server restart still leaves an audit trail; live runs are memory-only and
say so by being absent from ``list_persisted`` until they finish.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent import store
from app.agent.brain import resolve_agent_brain
from app.agent.loop import (
    AgentLimits,
    AgentResult,
    CancelToken,
    QueueGate,
    run_agent,
)

logger = logging.getLogger("ev.agent.service")

MAX_LIVE_RUNS = 100
MAX_EVENTS = 500


@dataclass
class RunHandle:
    run_id: str
    goal: str
    root: str
    approved: bool | None
    gate: QueueGate
    cancel: CancelToken
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    result: dict[str, Any] | None = None
    error: str | None = None
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # The run's aggregated usage reached the ledger (logged exactly once
    # across waiters and the mirror).
    usage_logged: bool = False
    task: asyncio.Task | None = None

    def snapshot(self, *, events: int = 100) -> dict[str, Any]:
        status = self.status
        pending = None
        if status == "running" and self.gate.waiting:
            status = "awaiting_approval"
            pending = self.gate.last_request.to_dict() if self.gate.last_request else None
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "root": self.root,
            "approved": bool(self.approved),
            "status": status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pending_approval": pending,
            "error": self.error,
            "events": list(self.events)[-max(1, min(events, MAX_EVENTS)):],
            "result": self.result,
        }


class AgentRunService:
    """One registry for live runs. Safe for a single event loop."""

    def __init__(self) -> None:
        self._runs: OrderedDict[str, RunHandle] = OrderedDict()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(
        self,
        goal: str,
        *,
        approved: bool | None = None,
        root: str | Path | None = None,
        max_steps: int | None = None,
        max_seconds: int | None = None,
        provider_name: str | None = None,
        context: str = "",
    ) -> RunHandle:
        choice = resolve_agent_brain(provider_name)
        limits = AgentLimits.from_settings()
        if max_steps is not None:
            limits.max_steps = max(1, min(int(max_steps), 64))
        if max_seconds is not None:
            limits.max_seconds = max(5, min(int(max_seconds), 3600))
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        handle = RunHandle(
            run_id=run_id,
            goal=goal,
            root=str(root or ""),
            approved=approved,
            gate=QueueGate(),
            cancel=CancelToken(),
        )
        handle.task = asyncio.create_task(
            self._execute(handle, choice, limits, root, context),
            name=f"ev-agent-{run_id}",
        )
        self._runs[run_id] = handle
        self._trim()
        return handle

    async def _execute(
        self,
        handle: RunHandle,
        choice,
        limits: AgentLimits,
        root: str | Path | None,
        context: str,
    ) -> None:
        from app.fabric.presence_bus import agent_sink

        def on_event(event: dict[str, Any]) -> None:
            handle.events.append(event)
            agent_sink(event)

        try:
            outcome: AgentResult = await run_agent(
                handle.goal,
                root=root,
                approved=handle.approved,
                brain=choice,
                limits=limits,
                gate=handle.gate,
                cancel=handle.cancel,
                on_event=on_event,
                run_id=handle.run_id,
                context=context,
            )
            payload = outcome.to_dict()
            handle.status = "done"
            handle.result = payload
            if payload.get("status") == "failed":
                handle.error = payload.get("error")
            store.write_run(payload)
        except asyncio.CancelledError:
            handle.status = "cancelled"
            handle.error = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - a crashed run is a reported failure
            logger.exception("agent run crashed")
            handle.status = "failed"
            handle.error = f"{type(exc).__name__}: {exc}"
        finally:
            handle.finished_at = time.time()

    def _trim(self) -> None:
        finished = [rid for rid, handle in self._runs.items() if handle.status != "running"]
        while len(self._runs) > MAX_LIVE_RUNS and finished:
            self._runs.pop(finished.pop(0), None)

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #

    def get(self, run_id: str) -> RunHandle | None:
        return self._runs.get(run_id)

    def snapshot(self, run_id: str, *, events: int = 100) -> dict[str, Any] | None:
        handle = self._runs.get(run_id)
        if handle is None:
            return None
        return handle.snapshot(events=events)

    def approve(self, run_id: str, decision: str = "approve") -> bool:
        """Answer a parked approval; 'approve', 'approve_run', or 'deny'."""
        handle = self._runs.get(run_id)
        if handle is None or decision not in {"approve", "approve_run", "deny"}:
            return False
        return handle.gate.decide(decision)

    def cancel(self, run_id: str) -> bool:
        handle = self._runs.get(run_id)
        if handle is None:
            return False
        handle.cancel.cancel()
        if handle.task is not None and not handle.task.done():
            handle.task.cancel()
            handle.status = "cancelled"
        return True

    async def wait(self, run_id: str, timeout: float | None = None) -> dict[str, Any] | None:
        handle = self._runs.get(run_id)
        if handle is None:
            return None
        if handle.task is not None:
            with_timeout = timeout if timeout is not None else 3600.0
            try:
                await asyncio.wait_for(asyncio.shield(handle.task), timeout=with_timeout)
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                pass
        return handle.snapshot()

    def live(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = [handle.snapshot(events=0) for handle in reversed(self._runs.values())]
        return rows[: max(1, min(limit, MAX_LIVE_RUNS))]

    def shutdown(self) -> None:
        for handle in self._runs.values():
            if handle.task is not None and not handle.task.done():
                handle.task.cancel()


SERVICE = AgentRunService()
