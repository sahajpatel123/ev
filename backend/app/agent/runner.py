"""Durable task runner: submit a goal, mirror it into the ledger, report the truth.

This is the layer that fixes the "submitted, I'll inform you" dead end. A goal
is only acknowledged after a real run handle exists; the handle is mirrored
into the durable ledger on every step; the owner can inspect, approve, cancel,
or retry it by id; and a process death is reconciled as ``interrupted`` on the
next boot instead of leaving a phantom "running" row.

``SERVICE`` (in-process asyncio runs) is the executor. The ledger is what the
conversation, the CLI, and the health surfaces read. They cannot disagree:
the mirror writes the handle's real status, not a promised one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from typing import Any

from app.agent import ledger
from app.agent.service import SERVICE, RunHandle
from app.config import settings

logger = logging.getLogger("ev.agent.runner")

BOOT_ID = uuid.uuid4().hex
_MIRRORS: set[asyncio.Task] = set()


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def _ago(stamp: float | None) -> str:
    if not stamp:
        return ""
    seconds = max(0, int(time.time() - float(stamp)))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def _step_phrase(task: dict[str, Any]) -> str:
    event = str(task.get("last_event") or "").strip()
    if event:
        return event
    if task.get("status") == "awaiting_approval":
        tool = str(task.get("approval_tool") or "an action")
        return f"waiting for your approval before {tool}"
    return "starting up"


def status_line(task: dict[str, Any]) -> str:
    """One truthful sentence about a task."""
    status = str(task.get("status") or "")
    goal = str(task.get("goal") or "")[:90]
    step = _step_phrase(task)
    if status == "running":
        return f"Working on “{goal}” — {step}."
    if status == "awaiting_approval":
        return f"“{goal}” is paused — {step}."
    if status == "completed":
        return f"Finished “{goal}”."
    if status == "needs_input":
        return str(task.get("summary") or f"I have a question about “{goal}”.")
    if status == "cancelled":
        return f"Cancelled “{goal}”."
    if status == "interrupted":
        return f"“{goal}” was interrupted when the app restarted; it did not finish."
    return f"“{goal}” failed: {task.get('error') or 'unknown error'}."


def _studio_line() -> str:
    """Truthful status for the legacy code studio, which keeps its own store."""
    try:
        from app.ev.code_studio import load_studio

        studio = load_studio() or {}
        if str(studio.get("status") or "") in {"queued", "running"}:
            title = str(studio.get("title") or "a coding job")[:100]
            return f"Your coding job “{title}” is {studio.get('status')} in the background."
    except Exception:  # noqa: BLE001 - status must never fail
        pass
    return ""


def status_reply() -> str:
    """Answer 'what are you doing' from the ledger, not from a model."""
    active = ledger.list_tasks(active=True, limit=5)
    if active:
        lines = [status_line(task) for task in active]
        if len(active) == 1:
            extra = f" Task id {active[0]['id']}." if active[0]["status"] == "running" else ""
            return "Right now: " + lines[0] + extra
        return "Right now: " + " ".join(lines)
    recent = [task for task in ledger.list_tasks(limit=8) if task["status"] in ledger.FINAL_STATUSES]
    studio = _studio_line()
    if recent:
        line = "Nothing is running. Last task: " + status_line(recent[0])
        return f"{line} {studio}".strip()
    if studio:
        return studio
    return "Nothing is running right now."


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def _event_phrase(event: dict[str, Any]) -> str:
    kind = str(event.get("kind") or "")
    if kind == "tool_started":
        return f"running {event.get('tool') or 'a tool'}"
    if kind == "tool_result":
        tool = str(event.get("tool") or "tool")
        if event.get("ok"):
            return f"finished {tool}"
        return f"{tool} failed: {event.get('error') or 'unknown error'}"
    if kind == "approval_requested":
        return f"waiting for approval before {event.get('tool') or 'an action'}"
    if kind == "approval_decision":
        return f"approval {event.get('decision')} for {event.get('tool') or 'an action'}"
    if kind == "run_started":
        return "planning the first step"
    if kind == "brain_turn":
        return "thinking about the next step"
    return ""


def _sync_row(task_id: str, handle: RunHandle) -> dict[str, Any] | None:
    snapshot = handle.snapshot(events=0)
    events = list(handle.events)
    last_event = ""
    for event in reversed(events):
        phrase = _event_phrase(event)
        if phrase:
            last_event = phrase
            break
    steps = 0
    tool_calls = 0
    if handle.result:
        steps = len(handle.result.get("steps") or [])
        tool_calls = int(handle.result.get("tool_calls") or 0)
    raw_status = str(snapshot.get("status") or "running")
    result = handle.result or {}
    status = "running"
    if raw_status == "done":
        mapped = str(result.get("status") or "completed")
        if mapped == "completed":
            status = "completed"
        elif mapped == "needs_input":
            status = "needs_input"
        elif mapped == "cancelled":
            status = "cancelled"
        else:
            status = "failed"
    elif raw_status == "cancelled":
        status = "cancelled"
    elif raw_status == "failed":
        status = "failed"
    fields: dict[str, Any] = {
        "last_event": last_event,
        "steps": steps,
        "tool_calls": tool_calls,
    }
    if status == "running" and handle.gate.waiting:
        status = "awaiting_approval"
        request = handle.gate.last_request
        fields["approval_tool"] = request.tool if request else None
        fields["approval_args"] = request.arguments if request else {}
    if status != "awaiting_approval":
        fields["approval_tool"] = None
        fields["approval_args"] = {}
        fields["approval_notified"] = 0
    fields["status"] = status
    if status in ledger.FINAL_STATUSES:
        fields["summary"] = str(result.get("summary") or handle.error or "")[:4000]
        fields["error"] = str(result.get("error") or handle.error or "")[:4000]
        fields["run_id"] = handle.run_id
    ledger.update(task_id, **fields)
    return ledger.get(task_id)


def apply_pending_control(task_id: str, handle: RunHandle) -> None:
    """Apply a decision/cancel written by another process (ev.api vs ev.talk)."""
    row = ledger.get(task_id)
    if row is None:
        return
    if row.get("cancel_requested") and handle.status == "running":
        SERVICE.cancel(handle.run_id)
        ledger.update(task_id, status="cancelled", error="cancelled by owner")
    decision = str(row.get("decision") or "").strip()
    if decision and handle.gate.waiting:
        SERVICE.approve(handle.run_id, decision)
        ledger.update(task_id, decision=None, last_event=f"owner said {decision}")


async def _mirror(task_id: str, handle: RunHandle, *, announce_final: bool) -> None:
    """Mirror one run into the ledger until it stops, then announce once."""
    approval_announced = False
    try:
        while True:
            apply_pending_control(task_id, handle)
            row = _sync_row(task_id, handle)
            if row is None:
                return
            if row["status"] == "awaiting_approval" and not approval_announced:
                await _announce_approval(row)
                approval_announced = True
            elif row["status"] != "awaiting_approval":
                approval_announced = False
            if row["status"] in ledger.FINAL_STATUSES:
                if announce_final:
                    await _announce_final(row)
                return
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the mirror must not kill the run
        logger.exception("task mirror failed")
    finally:
        # A mirror that stops while the run is live still leaves a truthful
        # row: sync once more; orphan reconciliation handles process death.
        with contextlib.suppress(Exception):
            _sync_row(task_id, handle)


async def _announce_approval(task: dict[str, Any]) -> None:
    from app.agent.notify import announce

    fresh = ledger.get(str(task["id"]))
    if fresh is None or fresh.get("approval_notified"):
        return  # the turn that received the request already asked
    ledger.update(str(task["id"]), approval_notified=1)
    tool = str(task.get("approval_tool") or "an action")
    await announce(
        "Evie needs your approval",
        f"I'm paused before {tool} on “{str(task.get('goal') or '')[:120]}”. "
        f"Say yes to continue or no to stop (task {task['id']}).",
        kind="agent_approval",
    )


async def _record_thread_completion(task: dict[str, Any]) -> None:
    """Deliver a background result to the conversation thread (B5/0.4).

    Best effort like the voice/bus announcement: without this, a text turn
    that outlived its inline wait heard progress and then silence. No memory
    extraction runs here — a status line is not a memory.
    """

    try:
        from app.db import SessionLocal
        from app.ev.assistant import bind_live_thread
        from app.memory.turns import record_conversation_turn

        status = str(task.get("status") or "")
        goal = str(task.get("goal") or "")[:120]
        summary = str(task.get("summary") or task.get("error") or "").strip()
        if status == "completed":
            text = f"Finished “{goal}”: {summary}" if goal else summary
        elif status == "needs_input":
            text = f"“{goal}” needs one detail: {summary}" if goal else summary
        elif status == "cancelled":
            text = f"Cancelled “{goal}”." if goal else "Cancelled."
        else:
            detail = summary or "no error was reported"
            text = f"“{goal}” failed: {detail}" if goal else f"That task failed: {detail}"
        if not text.strip():
            return
        async with SessionLocal() as session:
            # The chat surface reads the LIVE thread, which may differ from
            # the default thread; deliver exactly there, never orphaned.
            thread = await bind_live_thread(session)
            await record_conversation_turn(
                session,
                text=text[:2000],
                role="assistant",
                source="agent_task",
                conversation_id=thread.id,
                modality="task",
                transcript_source="agent_task",
                extra_metadata={
                    "task_id": str(task.get("id") or ""),
                    "task_status": status,
                },
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - delivery must not break the mirror
        logger.debug("thread completion record skipped", exc_info=True)


async def _record_agent_usage(task: dict[str, Any]) -> None:
    """One aggregated ledger row per agent run (closes the 0.2 agent gap).

    The agent loop has no DB session, so per-call rows are impossible here;
    the run's aggregated usage is logged once with its brain-call count, so
    agent-lane egress and cost stay visible to the ledger. Runs that made no
    brain call (fail-closed starts) leave no row. Best effort.
    """

    try:
        run_id = str(task.get("run_id") or "")
        handle = SERVICE.get(run_id) if run_id else None
        if handle is None or handle.usage_logged:
            return
        payload = handle.result or {}
        usage = payload.get("usage") or {}
        brain = payload.get("brain") or {}
        if not isinstance(usage, dict) or not usage.get("brain_calls"):
            return
        handle.usage_logged = True
        from app.config import settings as _settings
        from app.db import SessionLocal
        from app.services.model_call import DATA_CLASS_AGENT_TASK, log_brain_call

        provider = str(brain.get("provider") or "muse")
        # Only a fully-reported summary may ride as the cost receipt;
        # anything else is re-estimated and labeled honestly.
        receipt: dict[str, Any] = {
            "prompt_tokens": usage.get("prompt_tokens") or 0,
            "completion_tokens": usage.get("completion_tokens") or 0,
            "prompt_tokens_details": {
                "cached_tokens": usage.get("cached_tokens") or 0
            },
        }
        if usage.get("cost_source") == "openrouter_reported":
            receipt["cost"] = usage.get("cost_usd") or 0.0
        steps = payload.get("steps") or []
        tool_names = [
            {"name": str(step.get("tool") or "")}
            for step in steps[:40]
            if isinstance(step, dict)
        ]
        async with SessionLocal() as session:
            await log_brain_call(
                session,
                provider=provider,
                model=brain.get("model"),
                effort=(
                    str(_settings.agent_reasoning_effort)
                    if provider == "muse"
                    else None
                ),
                usage=receipt,
                latency_ms=float(payload.get("elapsed_ms") or 0),
                actor=str(task.get("source") or "agent"),
                request_id=f"agent-{task.get('id')}",
                data_class=DATA_CLASS_AGENT_TASK,
                tool_calls=tool_names,
                extra_metadata={"brain_calls": int(usage.get("brain_calls") or 0)},
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - audit must not break the mirror
        logger.debug("agent usage record skipped", exc_info=True)


async def _announce_final(task: dict[str, Any]) -> None:
    from app.agent.notify import announce

    # A turn that is waiting on this task speaks the result itself; give it a
    # beat to mark the row consumed so the owner does not hear it twice.
    await asyncio.sleep(0.75)
    fresh = ledger.get(str(task["id"]))
    if fresh is None or fresh.get("notified"):
        return  # already spoken inside the turn that started it
    status = str(fresh.get("status") or "")
    ledger_summary = str(fresh.get("summary") or fresh.get("error") or "").strip()
    if not ledger_summary:
        # An empty completion still owes the thread an honest status line,
        # never silence.
        goal = str(fresh.get("goal") or "")[:120]
        if status == "failed":
            fallback = f"“{goal}” failed with no recorded detail." if goal else "Failed with no recorded detail."
        elif status == "cancelled":
            fallback = f"Cancelled “{goal}”." if goal else "Cancelled."
        else:
            fallback = f"“{goal}” finished with no summary." if goal else "Finished with no summary."
        ledger.update(str(task["id"]), summary=fallback)
        fresh = ledger.get(str(task["id"])) or fresh
    summary = str(fresh.get("summary") or fresh.get("error") or "").strip()
    ledger.update(str(task["id"]), notified=1)
    await _record_thread_completion(fresh)
    await _record_agent_usage(fresh)
    if status == "needs_input":
        await announce("Evie has a question", summary, kind="agent")
        return
    await announce(
        "Evie finished a task" if status == "completed" else "Evie stopped a task",
        summary,
        kind="agent",
    )


def submit_task(
    goal: str,
    *,
    source: str = "chat",
    approved: bool | None = None,
    root: str | None = None,
    context: str = "",
    announce_final: bool = True,
    max_steps: int | None = None,
    max_seconds: int | None = None,
) -> dict[str, Any]:
    """Create the ledger row first, then start the run. Returns the row."""
    text = (goal or "").strip()
    if not text:
        raise ValueError("goal is required")
    row = ledger.create(goal=text, source=source, pid=os.getpid(), boot_id=BOOT_ID)
    try:
        handle = SERVICE.start(
            text,
            approved=approved,
            root=root,
            max_steps=max_steps,
            max_seconds=max_seconds,
            context=context,
        )
    except Exception as exc:  # noqa: BLE001 - an unstartable task is a reported failure
        ledger.update(
            row["id"], status="failed", error=f"could not start: {type(exc).__name__}: {exc}"
        )
        return ledger.get(row["id"]) or row
    ledger.update(row["id"], run_id=handle.run_id, status="running")
    task = asyncio.create_task(
        _mirror(row["id"], handle, announce_final=announce_final),
        name=f"ev-task-{row['id']}",
    )
    _MIRRORS.add(task)
    task.add_done_callback(_MIRRORS.discard)
    return ledger.get(row["id"]) or row


async def wait_task(task_id: str, timeout: float) -> dict[str, Any]:
    """Wait up to ``timeout`` for a final row; return the current row either way."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        row = ledger.get(task_id)
        if row is None:
            return {}
        if row["status"] in ledger.FINAL_STATUSES:
            # Inline completions never reach _announce_final; their usage is
            # logged here (the handle flag keeps it exactly-once).
            await _record_agent_usage(row)
            return row
        if time.monotonic() >= deadline:
            return row
        await asyncio.sleep(0.25)


def approve_task(task_id: str, decision: str = "approve_run") -> dict[str, Any] | None:
    row = ledger.get(task_id)
    if row is None or not row.get("run_id"):
        return row
    if decision not in {"approve", "approve_run", "deny"}:
        return row
    run_id = str(row["run_id"])
    if SERVICE.get(run_id) is not None:
        SERVICE.approve(run_id, decision)
        ledger.update(task_id, last_event=f"owner said {decision}", status="running")
    else:
        # The run lives in the other API process (ev.api vs ev.talk). The
        # owner mirror applies this decision on its next tick.
        ledger.update(task_id, decision=decision, last_event=f"owner said {decision}")
    return ledger.get(task_id)


def cancel_task(task_id: str | None = None) -> dict[str, Any] | None:
    row = ledger.get(task_id) if task_id else ledger.newest_active()
    if row is None:
        return None
    run_id = str(row.get("run_id") or "")
    if not run_id:
        # A ledger-only row (tests, imported history) has no owner process.
        ledger.update(row["id"], status="cancelled", error="cancelled by owner")
    elif SERVICE.get(run_id) is not None:
        SERVICE.cancel(run_id)
        ledger.update(row["id"], status="cancelled", error="cancelled by owner")
    else:
        ledger.update(
            row["id"], cancel_requested=1, last_event="cancel requested by owner"
        )
    return ledger.get(row["id"])


def pending_approval() -> dict[str, Any] | None:
    return ledger.pending_approval()


def mark_approval_announced(task_id: str) -> None:
    ledger.update(task_id, approval_notified=1)


def mark_consumed(task_id: str) -> None:
    """The turn that started this task is speaking the result itself."""
    ledger.update(task_id, notified=1)


def is_inline_finished(row: dict[str, Any]) -> bool:
    return bool(row) and row.get("status") in ledger.FINAL_STATUSES


def inline_result_text(row: dict[str, Any]) -> str:
    """What the owner hears when the task stopped inside the turn."""
    status = str(row.get("status") or "")
    summary = str(row.get("summary") or "").strip()
    error = str(row.get("error") or "").strip()
    if status == "completed":
        return summary or "Done."
    if status == "needs_input":
        return summary or "I need one detail before I can continue."
    if status == "cancelled":
        return summary or "Cancelled."
    if status == "interrupted":
        return f"That task was interrupted before it finished. {error}".strip()
    return f"That task failed. {error or summary or 'No error was reported.'}".strip()[:600]


def progress_text(row: dict[str, Any]) -> str:
    """Truthful in-progress line with the real last step."""
    step = _step_phrase(row)
    status = str(row.get("status") or "")
    goal = str(row.get("goal") or "")[:100]
    if status == "awaiting_approval":
        tool = str(row.get("approval_tool") or "an action")
        return (
            f"I'm paused before {tool} on “{goal}”. Say yes to continue or no to stop "
            f"(task {row.get('id')})."
        )
    vague = step in {"starting up", "planning the first step", "thinking about the next step"}
    if str(row.get("source") or "") in {"voice", "phone"}:
        # A phone call has no screen. Speak the real first step; never promise
        # a notification or say the work was sent/submitted. The verified
        # result arrives later as its own spoken turn.
        return "On it." if vague else f"On it — {step}."
    if vague:
        return f"On it — “{goal}”."
    return f"On it — {step}, for “{goal}”."


# --------------------------------------------------------------------------- #
# Reconciliation + watchdog
# --------------------------------------------------------------------------- #


def reconcile() -> int:
    """Mark rows from a dead process as interrupted. Returns rows changed."""
    return ledger.mark_orphans(BOOT_ID)


async def watch_loop() -> None:
    """Periodic truth maintenance for tasks that outlive their turn."""
    interval = max(5.0, float(getattr(settings, "agent_watch_seconds", 15.0) or 15.0))
    while True:
        try:
            reconcile()
            ledger.prune()
        except Exception:  # noqa: BLE001 - watchdog never dies
            logger.exception("agent task watchdog tick failed")
        await asyncio.sleep(interval)


def shutdown() -> None:
    """Stop live runs and leave a truthful ledger row for each."""
    for row in ledger.list_tasks(active=True, limit=100):
        if int(row.get("pid") or 0) != os.getpid():
            continue  # another live worker owns this row
        run_id = str(row.get("run_id") or "")
        if run_id and SERVICE.get(run_id) is not None:
            SERVICE.cancel(run_id)
        ledger.update(
            str(row["id"]),
            status="interrupted",
            error="the app shut down before this task finished",
        )


def bootstrap() -> asyncio.Task | None:
    """Init the ledger, reconcile orphans, and start the watchdog."""
    ledger.init()
    changed = reconcile()
    if changed:
        logger.warning("agent ledger: %d task(s) marked interrupted after restart", changed)
    try:
        return asyncio.get_running_loop().create_task(watch_loop(), name="ev-agent-watchdog")
    except RuntimeError:
        return None
