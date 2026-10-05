"""Durable Realtime task handoff to the existing permissioned MiMo kernel.

Receipts commit before admission. No automatic retry after an interrupted tool
run: replaying actions without evidence could execute the owner's action twice.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from app.cognitive.mode import _DELEGATE_BINDING, _DELEGATE_TASK, _WORKER_MODE
from app.db import SessionLocal
from app.models import ResearchSession

logger = logging.getLogger(__name__)
Callback = Callable[[dict[str, Any]], Awaitable[None]]
_TAG = "rt_delegate"
_MAX_PENDING = 8
_TIMEOUT = 180
_tasks: dict[str, asyncio.Task] = {}
_callbacks: dict[str, Callback] = {}
_admission = asyncio.Lock()
_worker = asyncio.Lock()


def _receipt(row: ResearchSession) -> dict[str, Any]:
    # Facts only. The live model phrases the status in its own natural words;
    # canned sentences read as robotic ("queued", "confirmed") when spoken.
    return {
        "job_id": str(row.id), "status": row.status,
        "accepted": row.status not in {"failed", "cancelled", "interrupted"},
        "spoken": row.conclusion,
        "result": dict(row.evidence or {}).get("result"),
        "live_session_id": dict(row.budget or {}).get("live_session_id"),
        "device_id": dict(row.budget or {}).get("device_id"),
    }


async def submit_delegate(
    *, task: str, request_id: str, live_session_id: str | None = None,
    device_id: str | None = None, actor: str = "master",
    on_complete: Callback | None = None, phone_text_context: Any = None,
    owner_transcript: str | None = None, owner_turn_id: str | None = None,
) -> dict[str, Any]:
    """Commit admission, schedule separately, and return without model inference."""
    from app.cognitive.mode import realtime_delegate_active

    if not realtime_delegate_active():
        return {"accepted": False, "status": "failed", "spoken": "Task delegation is disabled."}
    task_hint = task[:8000]
    task = (owner_transcript if owner_transcript is not None else task).strip()
    if not task or len(task) > 8000 or not request_id or len(request_id) > 256:
        return {"accepted": False, "status": "failed", "spoken": "That task request is invalid."}
    identity = f"ev:{_TAG}:{actor}:{device_id}:{live_session_id}:{owner_turn_id or request_id}"
    job_id = uuid5(NAMESPACE_URL, identity)
    digest = hashlib.sha256(task.encode()).hexdigest()
    async with _admission:
        async with SessionLocal() as db:
            row = await db.get(ResearchSession, job_id)
            if row:
                if row.mode != _TAG or row.owner != actor or row.budget.get("task_digest") != digest:
                    return {"accepted": False, "status": "failed", "spoken": "That request identifier was already used."}
                if on_complete and row.status in {"queued", "running"}:
                    _callbacks[str(job_id)] = on_complete
                return _receipt(row)
            # Expired process-owned rows are interrupted, never replayed.
            stale = (await db.scalars(select(ResearchSession).where(
                ResearchSession.mode == _TAG, ResearchSession.status.in_(["queued", "running"]),
            ))).all()
            for old in stale:
                pid = int(old.budget.get("worker_pid") or 0)
                alive = False
                if pid:
                    try:
                        os.kill(pid, 0)
                        alive = pid != os.getpid() or str(old.id) in _tasks
                    except OSError:
                        pass
                if not alive:
                    old.status = "interrupted"
                    old.conclusion = "The task worker was interrupted. Review its actions before retrying."
            await db.flush()
            pending = await db.scalar(select(func.count()).select_from(ResearchSession).where(
                ResearchSession.mode == _TAG,
                ResearchSession.status.in_(["queued", "running"]),
            ))
            if (pending or 0) >= _MAX_PENDING:
                return {"accepted": False, "status": "busy", "spoken": "The task queue is full. Please try again shortly."}
            from app.device_gateway.cognitive_phone import capture_phone_binding, is_phone_turn

            phone_binding = None
            if device_id and await is_phone_turn(db, device_id) and live_session_id:
                phone_binding = await capture_phone_binding(
                    db, device_id=device_id, live_session_id=live_session_id,
                )
                if phone_binding is None:
                    return {"accepted": False, "status": "failed",
                            "spoken": "The phone session is no longer authorized."}
            row = ResearchSession(
                id=job_id, owner=actor, mode=_TAG, status="queued", question=task,
                goal=task, budget={"live_session_id": live_session_id, "device_id": device_id,
                                   "task_digest": digest, "worker_pid": os.getpid(), "owner_turn_id": owner_turn_id, "task_hint": task_hint},
            )
            db.add(row)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                row = await db.get(ResearchSession, job_id)
                if row is None or row.owner != actor or row.mode != _TAG or row.budget.get("task_digest") != digest:
                    raise
                return _receipt(row)
            receipt = _receipt(row)
        if on_complete:
            _callbacks[str(job_id)] = on_complete
        future = asyncio.create_task(
            _run(job_id, on_complete=on_complete, phone_text_context=phone_text_context, phone_binding=phone_binding),
            name=f"ev-delegate-{job_id}",
        )
        _tasks[str(job_id)] = future
        future.add_done_callback(lambda _: _tasks.pop(str(job_id), None))
        return receipt


async def _maybe_run_graph_job(
    job_id: UUID, task: str, *, actor: str, binding: dict[str, Any]
) -> dict[str, Any] | None:
    """Graph path for one delegated job. None means "use the legacy path".

    Returns None when the flag is off or the planner cannot serve
    pre-execution (no key, no structured output, bad plan). Anything raised
    after a worker executes is an honest job failure, never a legacy rerun.
    """

    from app.cognitive.graph import (
        GraphPlanError,
        GraphUnavailable,
        StatusEvent,
        StatusThrottler,
        delegate_graph_active,
        run_graph,
    )

    if not delegate_graph_active():
        return None
    throttler = StatusThrottler()

    async def progress(event: StatusEvent) -> None:
        async with SessionLocal() as db:
            row = await db.get(ResearchSession, job_id)
            if row is None or row.cancel_requested:
                return
            budget = dict(row.budget or {})
            events = list(budget.get("status_events") or [])[-19:]
            events.append(event.model_dump(mode="json"))
            budget["status_events"] = events
            row.budget = budget
            if throttler.speakable(event):
                row.conclusion = event.text[:500]
            await db.commit()

    try:
        outcome = await run_graph(
            task,
            job_id=str(job_id),
            actor=actor,
            live_session_id=binding.get("live_session_id"),
            device_id=binding.get("device_id"),
            progress=progress,
        )
    except (GraphUnavailable, GraphPlanError):
        return None
    return await _finish(
        job_id, outcome.status, outcome.spoken, {"graph": outcome.model_dump(mode="json")}
    )


async def _run(job_id: UUID, *, on_complete: Callback | None, phone_text_context: Any, phone_binding: Any) -> None:
    token = _WORKER_MODE.set(True)
    binding_token = _DELEGATE_BINDING.set(phone_binding)
    task_token = _DELEGATE_TASK.set("")
    try:
        async with _worker:
            async with SessionLocal() as db:
                claimed = await db.execute(update(ResearchSession).where(
                    ResearchSession.id == job_id, ResearchSession.status == "queued",
                ).values(status="running", attempts=ResearchSession.attempts + 1))
                await db.commit()
                if not cast(CursorResult[Any], claimed).rowcount:
                    return
                row = await db.get(ResearchSession, job_id)
                if row is None:
                    return
                task, actor, binding = row.question, row.owner, dict(row.budget)
                _DELEGATE_TASK.set(str(binding.get("task_hint") or ""))
            async with SessionLocal() as db:
                from app.device_gateway.cognitive_phone import phone_turn_authority_changed

                changed = await phone_turn_authority_changed(
                    db, device_id=binding.get("device_id"),
                    live_session_id=binding.get("live_session_id"),
                    expected_binding=phone_binding, text_context=phone_text_context,
                )
                if changed:
                    raise RuntimeError(changed)
            graph_receipt = await _maybe_run_graph_job(
                job_id, task, actor=actor, binding=binding,
            )
            receipt: dict[str, Any] | None
            if graph_receipt is not None:
                receipt = graph_receipt
                result = None
                state = str(graph_receipt.get("status") or "failed")
                used_graph = True
            else:
                used_graph = False
                from app.cognitive.kernel import handle_turn

                async with SessionLocal() as db:
                    result = await asyncio.wait_for(handle_turn(
                        transcript=task, session=db, actor=actor, modality="text",
                        live_session_id=binding.get("live_session_id"),
                        device_id=binding.get("device_id"), phone_text_context=phone_text_context,
                    ), timeout=_TIMEOUT)
                    await db.commit()
            if not used_graph:
                assert result is not None
                payload = result.as_dict()
                # A model/tool reply is a result, not proof every requested effect
                # completed. Existing background/approval receipts remain pending.
                waiting = result.kind in {"in_flight", "code", "send_prompt", "send_approval"}
                if result.goal_id and result.persist:
                    from app.models import PresenceContract

                    async with SessionLocal() as goal_db:
                        try:
                            goal = await goal_db.get(PresenceContract, UUID(result.goal_id))
                        except ValueError:
                            goal = None
                        if goal is not None and goal.state not in {"COMPLETED", "CANCELLED", "FAILED", "EXPIRED"}:
                            waiting = True
                state = "failed" if result.unavailable or result.kind == "failed" else "waiting" if waiting else "answered"
                receipt = await _finish(job_id, state, result.spoken, payload)
        if (
            not used_graph
            and result is not None
            and state == "waiting"
            and result.goal_id
            and result.kind not in {"send_prompt", "send_approval"}
        ):
            if on_complete and receipt:
                foreground = _WORKER_MODE.set(False)
                try:
                    await on_complete(receipt)
                finally:
                    _WORKER_MODE.reset(foreground)
            # Existing goal execution remains the truth. Observe its terminal
            # state rather than interpreting a model acknowledgement as done.
            receipt = await _monitor_goal(job_id, result.goal_id)
    except asyncio.CancelledError:
        receipt = await _finish(job_id, "cancelled", "The delegated task was cancelled. Actions already performed may remain.", None)
    except Exception as exc:
        logger.exception("Delegated task failed: %s", job_id)
        receipt = await _finish(job_id, "failed", "The task agent couldn't finish that request.", None, type(exc).__name__)
    finally:
        _DELEGATE_TASK.reset(task_token)
        _DELEGATE_BINDING.reset(binding_token)
        _WORKER_MODE.reset(token)
    callback = _callbacks.pop(str(job_id), None)
    if callback and receipt:
        try:
            await callback(receipt)
        except Exception:
            # Results remain durable for polling/reconnection even if the
            # original live socket is already closed.
            logger.exception("Delegated task delivery failed: %s", job_id)


async def _finish(job_id: UUID, status: str, spoken: str, result: dict | None,
                  error: str | None = None) -> dict[str, Any] | None:
    async with SessionLocal() as db:
        row = await db.get(ResearchSession, job_id)
        if row is None:
            return None
        if row.cancel_requested:
            status, spoken = "cancelled", "The task was cancelled. Actions already performed may remain."
        row.status, row.conclusion, row.last_error = status, spoken, error
        row.evidence = {"result": result, "finished_at": datetime.now(UTC).isoformat()}
        await db.commit()
        return _receipt(row)


async def get_delegate(job_id: str, *, actor: str = "master") -> dict[str, Any] | None:
    try:
        identity = UUID(job_id)
    except (ValueError, TypeError):
        return None
    async with SessionLocal() as db:
        row = await db.get(ResearchSession, identity)
        if row is None or row.owner != actor or row.mode != _TAG:
            return None
        receipt = _receipt(row)
        # A process restart cannot safely replay a side-effecting worker.
        pid = int(row.budget.get("worker_pid") or 0)
        running_process = False
        if pid:
            try:
                os.kill(pid, 0)
                running_process = pid != os.getpid() or job_id in _tasks
            except (OSError, ValueError):
                pass
        if row.status in {"queued", "running"} and not running_process:
            row.status = "interrupted"
            row.conclusion = "The task worker was interrupted. Review its actions before retrying."
            await db.commit()
            receipt = _receipt(row)
        return receipt


async def cancel_delegate(job_id: str, *, actor: str = "master") -> dict[str, Any] | None:
    receipt = await get_delegate(job_id, actor=actor)
    if receipt is None:
        return None
    if receipt["status"] not in {"queued", "running", "interrupted", "waiting"}:
        return receipt
    async with SessionLocal() as db:
        row = await db.get(ResearchSession, UUID(job_id))
        if row is None:
            return None
        goal_id = dict(row.evidence or {}).get("result", {}) or {}
        goal_id = goal_id.get("goal_id")
        if goal_id:
            from app.presence.contract import GoalState
            from app.presence.service import get_contract, transition

            goal = await get_contract(db, goal_id)
            if goal is not None:
                await transition(db, goal, GoalState.CANCELLED.value, reason="owner_delegation_cancel")
        row.cancel_requested = True
        row.status = "cancelled"
        row.conclusion = "The task was cancelled. Actions already performed may remain."
        await db.commit()
        receipt = _receipt(row)
    future = _tasks.get(job_id)
    if future:
        future.cancel()
    callback = _callbacks.pop(job_id, None)
    if callback:
        try:
            await callback(receipt)
        except Exception:
            logger.exception("Delegated cancellation delivery failed: %s", job_id)
    return receipt


def delegate_task_spec() -> dict[str, Any]:
    """Operational tool contract shared by both Realtime transports."""
    return {
        "type": "function", "name": "delegate_task",
        "description": (
            "Assign an explicit owner task to MiMo, the permissioned task agent. "
            "Answer greetings and ordinary general conversation directly. Use this "
            "tool for actions, substantial research, current information or personal "
            "data needing tools. Never use it for greetings, small talk, thanks, "
            "identity questions, capability questions, or answers you already know. "
            "MiMo can read, summarise, and send WhatsApp "
            "(background, never opening a window), read mail, iMessage, calendar and "
            "contacts, work with files (create, edit, find, reveal), run code, search "
            "the web, check personal memory, and act on the Mac (Finder, apps, "
            "windows). Never tell the owner that you lack access to those — call "
            "delegate_task with their exact request instead, and do not answer for "
            "them yourself. Also delegate the owner's answer to a pending worker "
            "clarification or confirmation, including yes/no, with that context. Preserve the owner's task and constraints accurately. "
            "Use operation=status to review tasks, operation=cancel when the owner "
            "explicitly asks to cancel; job_id selects a prior receipt. Default "
            "operation=submit assigns work. The return is an admission receipt, "
            "never proof of completed execution. "
            "Tell the owner when work is accepted; the worker reports results later. "
            "Never promise completion without the worker's evidence."
        ),
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {"task": {"type": "string", "minLength": 1,
                                               "maxLength": 8000},
                                      "operation": {"type": "string", "enum": ["submit", "status", "cancel"]},
                                      "job_id": {"type": "string", "maxLength": 64}}, "required": []},
    }


async def list_delegates(*, actor: str = "master", live_session_id: str | None = None,
                         device_id: str | None = None) -> list[dict[str, Any]]:
    """Durable reconnect retrieval; original bindings are never reauthorized."""
    async with SessionLocal() as db:
        query = select(ResearchSession).where(
            ResearchSession.mode == _TAG, ResearchSession.owner == actor,
        )
        if live_session_id is not None:
            query = query.where(ResearchSession.budget["live_session_id"].as_string() == live_session_id)
        if device_id is not None:
            query = query.where(ResearchSession.budget["device_id"].as_string() == device_id)
        rows = (await db.scalars(query.order_by(ResearchSession.created_at.desc()).limit(100))).all()
        return [_receipt(row) for row in rows]


async def cancel_session_delegates(live_session_id: str, *, actor: str = "master") -> list[dict[str, Any]]:
    rows = await list_delegates(actor=actor, live_session_id=live_session_id)
    receipts = []
    for row in rows:
        if row["status"] in {"queued", "running", "interrupted", "waiting"}:
            receipt = await cancel_delegate(row["job_id"], actor=actor)
            if receipt:
                receipts.append(receipt)
    return receipts


async def _monitor_goal(job_id: UUID, goal_id: str) -> dict[str, Any] | None:
    from app.models import PresenceContract

    try:
        identity = UUID(goal_id)
    except ValueError:
        return None
    deadline = asyncio.get_running_loop().time() + 300
    while asyncio.get_running_loop().time() < deadline:
        async with SessionLocal() as db:
            row = await db.get(PresenceContract, identity)
            job = await db.get(ResearchSession, job_id)
            if job is None or job.cancel_requested:
                return None
            if row is None:
                return None
            state = str(row.state).upper()
            if state in {"COMPLETED", "STALLED", "BLOCKED", "FAILED", "CANCELLED", "EXPIRED"}:
                verified = row.confidence == "COMPLETED_VERIFIED" and bool(row.evidence) if state == "COMPLETED" else False
                spoken = ("The task is complete. You can review the result." if verified else
                          "The task agent reported completion, but verification is missing." if state == "COMPLETED" else
                          str(row.blocked_reason or "The task could not finish."))
                result = {"goal_id": goal_id, "goal_state": state, "verified": verified,
                          "verification": dict(row.verification or {}), "artifacts": dict(row.artifacts or {})}
                status = "completed" if state == "COMPLETED" and verified else "answered" if state == "COMPLETED" else "failed" if state == "FAILED" else state.lower()
                break
        await asyncio.sleep(1)
    else:
        return await _finish(job_id, "waiting", "The task is still in progress. Its saved status is available to review.", {"goal_id": goal_id})
    return await _finish(job_id, status, spoken, result)


async def dispatch_delegate_control(
    *, operation: str, live_session_id: str | None, device_id: str | None = None,
    job_id: str | None = None, actor: str = "master", owner_transcript: str | None = None,
) -> dict[str, Any]:
    """Status/cancellation limited to the originating authenticated conversation."""
    if operation not in {"status", "cancel"}:
        return {"ok": False, "spoken": "That task operation is unavailable."}
    rows = await list_delegates(actor=actor, live_session_id=live_session_id, device_id=device_id)
    if job_id:
        rows = [row for row in rows if row["job_id"] == job_id]
    if operation == "status":
        return {"ok": True, "tasks": rows,
                "spoken": rows[0]["spoken"] if rows else "There are no assigned tasks for this conversation."}
    text = str(owner_transcript or "").strip().lower()
    negated = re.search(r"\b(?:don['’]t|do\s+not|never|not)\s+(?:cancel|stop)\b", text)
    if negated or not re.search(r"\b(?:cancel|stop|nevermind|never\s+mind)\b", text):
        return {"ok": False, "spoken": "I need an explicit owner cancellation request."}
    receipts = []
    for row in rows:
        if row["status"] in {"queued", "running", "waiting", "interrupted"}:
            receipt = await cancel_delegate(row["job_id"], actor=actor)
            if receipt:
                receipts.append(receipt)
    return {"ok": True, "tasks": receipts, "spoken": "The assigned tasks were cancelled." if receipts else
            "There are no active assigned tasks for this conversation."}
