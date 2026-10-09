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
from app.models import ApprovedAction, ResearchSession

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
        # Present only when the graph planner handed the job back to legacy;
        # None means the job ran the path it was admitted for.
        "graph_fallback": dict(row.budget or {}).get("graph_fallback"),
    }


# Terminal-or-parked job states: no further worker callback will fire, so live
# sessions may drop local tracking. Single vocabulary — every consumer imports
# this instead of re-listing states (a re-listed set once missed "answered").
FINAL_DELEGATE_STATES = frozenset({
    "answered", "completed", "complete", "waiting",
    "failed", "cancelled", "interrupted",
})

_TERMINAL_STATES = frozenset({
    "answered", "completed", "complete", "failed", "cancelled", "interrupted",
})

_STATE_WORDS = {
    "queued": "planned",
    "running": "working",
    "waiting": "waiting_owner",
    "answered": "answered",
    "completed": "answered",
    "complete": "answered",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "interrupted",
}


def status_brief(*, status: str, budget: dict[str, Any] | None,
                 evidence: dict[str, Any] | None,
                 conclusion: str | None = None) -> dict[str, Any]:
    """Per-step truth for a status ask, derived from verdicts — never guessed.

    Steps come from the graph receipts+verdicts with the same DONE/NEEDS_OWNER/
    NOT_DONE mapping as the supervisor shift report. Jobs without graph
    evidence (admitted but unplanned, legacy turns) report the durable state
    with no invented steps.
    """

    graph = ((evidence or {}).get("result") or {}).get("graph") or {}
    receipts = graph.get("receipts") or []
    verdicts = graph.get("verdicts") or []
    steps: list[dict[str, Any]] = []
    for raw_v in verdicts:
        if not isinstance(raw_v, dict):
            continue
        node_id = str(raw_v.get("node_id") or "?")
        nxt = str(raw_v.get("next") or "")
        if nxt == "accept":
            mark = "DONE"
        elif nxt == "ask_owner":
            mark = "NEEDS_OWNER"
        else:
            mark = "NOT_DONE"
        count = 0
        for raw_r in receipts:
            if isinstance(raw_r, dict) and str(raw_r.get("node_id") or "") == node_id:
                count = len(raw_r.get("evidence") or []) + len(raw_r.get("artifacts") or [])
                break
        steps.append({"node_id": node_id, "mark": mark,
                      "state": str(raw_v.get("state") or "unknown"),
                      "score": raw_v.get("score"), "evidence": count})
    events = [str(entry.get("text") or "") for entry in (budget or {}).get("status_events") or []
              if isinstance(entry, dict) and str(entry.get("text") or "")]
    delivery = (budget or {}).get("delivery") or {}
    return {
        "state": _STATE_WORDS.get(status, status),
        "steps": steps,
        "nodes_accepted": sum(1 for step in steps if step["mark"] == "DONE"),
        "nodes_total": len(steps),
        "waiting_question": (conclusion or "") if status == "waiting" else None,
        "recent_events": events[-5:],
        "announced": bool(delivery.get("announced")),
    }


def delegate_status_snapshot(row: ResearchSession) -> dict[str, Any]:
    """Admission receipt enriched with verdict-derived per-step truth."""

    snapshot = _receipt(row)
    snapshot["brief"] = status_brief(
        status=str(row.status), budget=dict(row.budget or {}),
        evidence=dict(row.evidence or {}), conclusion=row.conclusion,
    )
    return snapshot


async def mark_delegate_announced(job_id: str | UUID, *, channel: str) -> None:
    """Record that a terminal result reached the owner via a live surface.

    First channel wins: speech, inbox, and status reads each count as
    delivery, and a later surface must not rewrite the provenance.
    """

    try:
        identity = job_id if isinstance(job_id, UUID) else UUID(str(job_id))
    except (ValueError, TypeError):
        return
    async with SessionLocal() as db:
        row = await db.get(ResearchSession, identity)
        if row is None:
            return
        budget = dict(row.budget or {})
        delivery = dict(budget.get("delivery") or {})
        if delivery.get("announced"):
            return
        delivery["announced"] = True
        delivery["channel"] = channel
        budget["delivery"] = delivery
        row.budget = budget
        await db.commit()


async def _status_snapshots(*, actor: str, live_session_id: str | None,
                            device_id: str | None,
                            job_id: str | None) -> list[dict[str, Any]]:
    """Status-op rows with per-step truth, same scoping as list_delegates."""

    async with SessionLocal() as db:
        query = select(ResearchSession).where(
            ResearchSession.mode == _TAG, ResearchSession.owner == actor,
        )
        if live_session_id is not None:
            query = query.where(ResearchSession.budget["live_session_id"].as_string() == live_session_id)
        if device_id is not None:
            query = query.where(ResearchSession.budget["device_id"].as_string() == device_id)
        if job_id:
            try:
                query = query.where(ResearchSession.id == UUID(str(job_id)))
            except (ValueError, TypeError):
                return []
        rows = (await db.scalars(query.order_by(ResearchSession.created_at.desc()).limit(100))).all()
        return [delegate_status_snapshot(row) for row in rows]


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
            from app.device_gateway.cognitive_phone import (
                capture_phone_binding,
                is_phone_turn,
                notify_reconnect_required,
            )

            phone_binding = None
            if device_id and await is_phone_turn(db, device_id) and live_session_id:
                phone_binding = await capture_phone_binding(
                    db, device_id=device_id, live_session_id=live_session_id,
                )
                if phone_binding is None:
                    notify_reconnect_required(live_session_id, "PHONE_SESSION_CHANGED")
                    return {"accepted": False, "status": "failed",
                            "error_code": "PHONE_SESSION_CHANGED",
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
    except (GraphUnavailable, GraphPlanError) as exc:
        # Planner outages fall back to the legacy turn, but never silently:
        # a 25s planner stall that vanishes from the receipt is
        # indistinguishable from a graph that actually ran.
        await _note_graph_fallback(job_id, reason=f"{type(exc).__name__}: {exc}")
        return None
    return await _finish(
        job_id, outcome.status, outcome.spoken, {"graph": outcome.model_dump(mode="json")}
    )


async def _note_graph_fallback(job_id: UUID, *, reason: str) -> None:
    """Record why the graph path handed the job back to the legacy turn."""

    from app.cognitive import telemetry

    telemetry.inc("graph_plan_fallbacks")
    telemetry.note(last_graph_fallback=reason[:200])
    try:
        async with SessionLocal() as db:
            row = await db.get(ResearchSession, job_id)
            if row is None:
                return
            budget = dict(row.budget or {})
            budget["graph_fallback"] = {
                "reason": reason[:300],
                "at": datetime.now(UTC).isoformat(),
            }
            row.budget = budget
            await db.commit()
    except Exception:  # noqa: BLE001 - the fallback itself must never fail a job
        logger.debug("graph fallback note failed", exc_info=True)


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
            "explicitly asks to cancel, operation=answer to deliver the owner's "
            "yes/no to a waiting task; job_id selects a prior receipt. Default "
            "operation=submit assigns work. The return is an admission receipt, "
            "never proof of completed execution. "
            "Tell the owner when work is accepted; the worker reports results later. "
            "Never promise completion without the worker's evidence."
        ),
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {"task": {"type": "string", "minLength": 1,
                                               "maxLength": 8000},
                                      "operation": {"type": "string", "enum": ["submit", "status", "cancel", "answer"]},
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


_APPROVAL_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok(?:ay)?|do it|go ahead|approved?|confirm(?:ed)?|proceed|please do)\b",
    re.IGNORECASE,
)
_DENIAL_RE = re.compile(
    r"\b(no|nope|nah|don't|do not|never|cancel(?: it)?|stop|not really)\b",
    re.IGNORECASE,
)


def _parse_approval(text: str | None) -> bool | None:
    """Owner answer → True/False, or None when it is not an answer at all.

    Denials win over approvals so "don't do it" never reads as approval.
    Anything long or ambiguous is not an answer: the job stays waiting and
    the owner is asked again instead of having words put in their mouth.
    """

    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > 300:
        return None
    if _DENIAL_RE.search(cleaned):
        return False
    if _APPROVAL_RE.search(cleaned):
        return True
    return None


async def _answer_waiting_job(
    *, actor: str, live_session_id: str | None, device_id: str | None,
    job_id: str | None, owner_transcript: str | None,
) -> dict[str, Any]:
    """Deliver the owner's answer to a waiting job and resume it when approved."""

    rows = await list_delegates(actor=actor, live_session_id=live_session_id, device_id=device_id)
    if job_id:
        rows = [row for row in rows if row["job_id"] == job_id]
    waiting = [row for row in rows if row["status"] == "waiting"]
    if not waiting:
        return {"ok": False, "spoken": "There is no task waiting for your answer."}
    target = waiting[0]
    verdict = _parse_approval(owner_transcript)
    if verdict is None:
        question = target.get("spoken") or "that pending step"
        return {
            "ok": False,
            "job_id": target["job_id"],
            "spoken": f"I still need a yes or no: {question}",
        }
    async with SessionLocal() as db:
        row = await db.get(ResearchSession, UUID(target["job_id"]))
        if row is None or row.status != "waiting":
            return {"ok": False, "spoken": "That task is no longer waiting."}
        graph = dict((row.evidence or {}).get("result", {}) or {}).get("graph", {}) or {}
    resumption: dict[str, Any] | None = None
    for raw in graph.get("receipts") or []:
        if isinstance(raw, dict) and isinstance(raw.get("resumption"), dict):
            resumption = raw["resumption"]
            break
    if verdict is False:
        await _deny_resumed_ticket(resumption)
        await _finish(
            UUID(target["job_id"]), "failed", "Understood — I left that step undone.",
            {"graph": graph, "owner_answer": "declined"},
        )
        return {
            "ok": True, "job_id": target["job_id"],
            "spoken": "Understood — I left that step undone.",
        }
    op = (resumption or {}).get("op")
    params = (resumption or {}).get("params")
    node_raw = (resumption or {}).get("node")
    if not op or not isinstance(params, dict) or not isinstance(node_raw, dict):
        return {
            "ok": False, "job_id": target["job_id"],
            "spoken": "That step can't resume automatically — tell me how to proceed instead.",
        }
    if op == "approved_action":
        action_id = params.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            return {
                "ok": False, "job_id": target["job_id"],
                "spoken": "That step can't resume automatically — tell me how to proceed instead.",
            }
        return await _resume_approved_action(
            UUID(target["job_id"]), graph=graph, node_raw=node_raw, action_id=action_id,
        )
    return await _resume_approved_job(
        UUID(target["job_id"]), graph=graph, node_raw=node_raw, op=str(op), params=params,
    )


async def _resumption_context(
    job_id: UUID,
) -> tuple[str, str, dict[str, Any], Any] | None:
    """Job question/actor/budget plus a status-event writer for a resume."""

    from app.cognitive.graph import StatusEvent

    async with SessionLocal() as db:
        row = await db.get(ResearchSession, job_id)
        if row is None:
            return None
        budget = dict(row.budget or {})
        question = row.question or row.goal or ""
        actor = row.owner or "master"

    async def progress(event: StatusEvent) -> None:
        async with SessionLocal() as db:
            target = await db.get(ResearchSession, job_id)
            if target is None or target.cancel_requested:
                return
            current = dict(target.budget or {})
            events = list(current.get("status_events") or [])[-19:]
            events.append(event.model_dump(mode="json"))
            current["status_events"] = events
            target.budget = current
            await db.commit()

    return question, actor, budget, progress


async def _complete_resumption(
    job_id: UUID,
    *,
    graph: dict[str, Any],
    node: Any,
    receipt: Any,
    verdict: Any,
    question: str,
    actor: str,
    budget: dict[str, Any],
    progress: Any,
) -> dict[str, Any]:
    """Join a resumed verdict with kept accepts and run remaining nodes."""

    from app.cognitive.graph import (
        SupervisorVerdict,
        TaskNode,
        WorkerReceipt,
        join_outcome,
        run_graph,
    )
    from app.cognitive.supervisor import VerdictNext

    kept_receipts = [
        WorkerReceipt.model_validate(r)
        for r, v in zip(graph.get("receipts") or [], graph.get("verdicts") or [],
                        strict=False)
        if isinstance(r, dict) and isinstance(v, dict)
        and v.get("next") == VerdictNext.ACCEPT.value
    ]
    kept_verdicts = [
        SupervisorVerdict.model_validate(v)
        for v in graph.get("verdicts") or []
        if isinstance(v, dict) and v.get("next") == VerdictNext.ACCEPT.value
    ]
    done_ids = {v.node_id for v in kept_verdicts} | {node.id}
    remaining: list[TaskNode] = []
    for raw in graph.get("evidence", {}).get("plan") or []:
        try:
            candidate = TaskNode.model_validate(raw) if isinstance(raw, dict) else None
        except Exception:  # noqa: BLE001 - a bad stored node never resumes
            candidate = None
        if candidate is not None and candidate.id not in done_ids:
            remaining.append(candidate)
    receipts = [*kept_receipts, receipt]
    verdicts = [*kept_verdicts, verdict]
    if remaining and verdict.next is VerdictNext.ACCEPT:
        outcome = await run_graph(
            question,
            job_id=str(job_id),
            actor=actor,
            live_session_id=budget.get("live_session_id"),
            device_id=budget.get("device_id"),
            progress=progress,
            nodes=remaining,
        )
        receipts.extend(outcome.receipts)
        verdicts.extend(outcome.verdicts)
    plan_total = len(graph.get("evidence", {}).get("plan") or []) or len(receipts)
    outcome = join_outcome(
        job_id=str(job_id), receipts=receipts, verdicts=verdicts, nodes_total=plan_total,
    )
    merged = outcome.model_dump(mode="json")
    merged["resumed"] = True
    await _finish(job_id, outcome.status, outcome.spoken, {"graph": merged})
    return {"ok": True, "job_id": str(job_id), "spoken": outcome.spoken}


async def _resume_approved_job(
    job_id: UUID, *, graph: dict[str, Any], node_raw: dict[str, Any],
    op: str, params: dict[str, Any],
) -> dict[str, Any]:
    """Replay the owner-approved file op, then run the remaining plan nodes."""

    from app.cognitive.graph import StatusEvent, TaskNode
    from app.cognitive.supervisor import supervise
    from app.cognitive.worker import receipt_from_result
    from app.ev.file_sandbox import execute_op

    context = await _resumption_context(job_id)
    if context is None:
        return {"ok": False, "spoken": "That task is gone."}
    question, actor, budget, progress = context
    node = TaskNode.model_validate(node_raw)
    await progress(
        StatusEvent(
            job_id=str(job_id), kind="started", important=True,
            text=f"Approved — running: {node.label}", node_id=node.id,
        )
    )
    try:
        replayed = execute_op(op, params, origin="graph-resume", confirm=True)
    except Exception as exc:  # noqa: BLE001 - replay failure concludes honestly
        replayed = {"ok": False, "error": type(exc).__name__}
    if not isinstance(replayed, dict):
        replayed = {"ok": False, "error": "bad_tool_result"}
    receipt = receipt_from_result(node, replayed, worker="file", duration_ms=0.0)
    verdict = await supervise(node, receipt)
    return await _complete_resumption(
        job_id, graph=graph, node=node, receipt=receipt, verdict=verdict,
        question=question, actor=actor, budget=budget, progress=progress,
    )


async def _deny_resumed_ticket(resumption: dict[str, Any] | None) -> None:
    """Deny the ledger ticket behind a declined graph resumption, if any."""

    import contextlib

    if not isinstance(resumption, dict) or resumption.get("op") != "approved_action":
        return
    params = resumption.get("params")
    action_id = params.get("action_id") if isinstance(params, dict) else None
    if not action_id:
        return
    with contextlib.suppress(Exception):
        from app.services.runtime import decide_action

        try:
            identity = UUID(str(action_id))
        except (ValueError, TypeError):
            return
        async with SessionLocal() as db:
            action = await db.get(ApprovedAction, identity)
            if action is not None and action.status == "pending":
                await decide_action(db, identity, actor="graph",
                                    decision="deny", reason="owner_declined")
                await db.commit()


async def _resume_approved_action(
    job_id: UUID, *, graph: dict[str, Any], node_raw: dict[str, Any],
    action_id: str,
) -> dict[str, Any]:
    """Execute an owner-approved tier-D ticket, then run remaining nodes."""

    from app.cognitive.graph import StatusEvent, TaskNode
    from app.cognitive.supervisor import supervise
    from app.cognitive.worker import receipt_from_result
    from app.ev.messaging.approval import approve_graph_action

    context = await _resumption_context(job_id)
    if context is None:
        return {"ok": False, "spoken": "That task is gone."}
    question, actor, budget, progress = context
    try:
        node = TaskNode.model_validate(node_raw)
    except Exception:  # noqa: BLE001 - a bad stored node never resumes
        return {"ok": False, "job_id": str(job_id),
                "spoken": "That step can't resume automatically — tell me how to proceed instead."}
    await progress(
        StatusEvent(
            job_id=str(job_id), kind="started", important=True,
            text=f"Approved — running: {node.label}", node_id=node.id,
        )
    )
    try:
        identity = UUID(str(action_id))
    except (ValueError, TypeError):
        return {"ok": False, "job_id": str(job_id),
                "spoken": "That approval ticket is invalid, so I didn't run it."}
    async with SessionLocal() as db:
        action = await db.get(ApprovedAction, identity)
        if action is None or action.status != "pending":
            return {"ok": False, "job_id": str(job_id),
                    "spoken": "That approval is no longer pending, so I didn't run it."}
        approved_out = await approve_graph_action(db, action, actor=actor)
        await db.commit()
    body = dict(approved_out.get("result") or {})
    body.setdefault("ok", bool(approved_out.get("ok")))
    body.setdefault("spoken", str(approved_out.get("spoken") or ""))
    if "delivery_receipt" in approved_out:
        body["delivery_receipt"] = approved_out["delivery_receipt"]
    receipt = receipt_from_result(node, body, worker="approved", duration_ms=0.0)
    verdict = await supervise(node, receipt, approved=True)
    return await _complete_resumption(
        job_id, graph=graph, node=node, receipt=receipt, verdict=verdict,
        question=question, actor=actor, budget=budget, progress=progress,
    )


async def dispatch_delegate_control(
    *, operation: str, live_session_id: str | None, device_id: str | None = None,
    job_id: str | None = None, actor: str = "master", owner_transcript: str | None = None,
) -> dict[str, Any]:
    """Status/cancellation/answer limited to the originating authenticated conversation."""
    if operation == "answer":
        return await _answer_waiting_job(
            actor=actor, live_session_id=live_session_id, device_id=device_id,
            job_id=job_id, owner_transcript=owner_transcript,
        )
    if operation not in {"status", "cancel"}:
        return {"ok": False, "spoken": "That task operation is unavailable."}
    rows = await list_delegates(actor=actor, live_session_id=live_session_id, device_id=device_id)
    if job_id:
        rows = [row for row in rows if row["job_id"] == job_id]
    if operation == "status":
        tasks = await _status_snapshots(
            actor=actor, live_session_id=live_session_id,
            device_id=device_id, job_id=job_id,
        )
        spoken = "There are no assigned tasks for this conversation."
        if tasks:
            first = tasks[0]
            spoken = str(first.get("spoken") or "")
            brief = first.get("brief") or {}
            if first.get("status") in _TERMINAL_STATES and not brief.get("announced"):
                # Durable redelivery: the completion never reached a live
                # surface (closed session, gone client, speech deadline).
                # Surfacing it here counts as delivery — the model speaks
                # tool results — so record it and say it was missed.
                await mark_delegate_announced(str(first.get("job_id") or ""), channel="status")
                brief["announced"] = True
                if spoken:
                    spoken = f"You missed this result: {spoken}"
        return {"ok": True, "tasks": tasks, "spoken": spoken}
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
