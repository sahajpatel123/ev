"""Kernel bridge: let a conversational turn hand a goal to the durable task lane.

The bridge no longer owns a runner or a watcher. It submits through
:mod:`app.agent.runner`, which writes the ledger row before starting the run,
mirrors every step, announces parked approvals and finished work, and makes the
task inspectable later. The kernel's only job here is to turn the current row
into one truthful spoken line.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent import runner
from app.config import settings

logger = logging.getLogger("ev.agent.bridge")


def _receipt(row: dict[str, Any], *, spoken: str | None = None) -> dict[str, Any]:
    status = str(row.get("status") or "")
    return {
        "ok": status in {"running", "awaiting_approval", "completed"},
        "spoken": (spoken or runner.progress_text(row))[:700],
        "run_id": row.get("run_id"),
        "task_id": row.get("id"),
        "status": status,
        "steps": int(row.get("steps") or 0),
        "evidence": {
            "task_id": row.get("id"),
            "status": status,
            "last_event": row.get("last_event"),
            "summary": row.get("summary"),
            "error": row.get("error"),
        },
    }


async def start_goal_run(
    goal: str,
    *,
    actor: str = "master",
    live_session_id: str | None = None,
    wait_seconds: float | None = None,
) -> dict[str, Any]:
    """Submit one durable agent task for a kernel turn and report its real state."""
    del actor  # authority already resolved by the kernel; the run is the owner's
    text = (goal or "").strip()
    if not text:
        return {"ok": False, "error": "BAD_ARGS", "spoken": "I need a goal to work on."}
    if not bool(getattr(settings, "agent_enabled", True)):
        return {
            "ok": False,
            "error": "DISABLED",
            "spoken": "The agent runtime is turned off.",
        }
    from app.gateway.turn_guard import current_guard

    guard = current_guard()
    if guard is not None:
        # 0.3 root-cause fix: a brain that re-issues the same agent goal in
        # one turn reuses the first run's receipt instead of stacking a
        # second 24-step run beside it.
        prior = guard.check_agent_run(text)
        if prior is not None:
            from app.cognitive.telemetry import inc as _tele_inc

            _tele_inc("duplicate_agent_runs")
            return dict(prior)
    def _out(receipt: dict[str, Any]) -> dict[str, Any]:
        if guard is not None:
            guard.record_agent_run(text, receipt)
        return receipt

    source = "voice" if live_session_id else "chat"
    row = runner.submit_task(text, source=source, approved=False, announce_final=True)
    if row.get("status") in {"failed", "interrupted"}:
        from app.agent.intake import _brain_dark_local_fallback

        fallback = await _brain_dark_local_fallback(
            text, row, source=source, approved=False
        )
        runner.mark_consumed(str(row["id"]))
        if fallback is not None:
            return _out({
                "ok": True,
                "spoken": str(fallback.get("spoken") or "Done.")[:700],
                "run_id": None,
                "task_id": row.get("id"),
                "status": "completed",
                "steps": len(fallback.get("receipts") or []),
                "evidence": {
                    "task_id": row.get("id"),
                    "status": "completed",
                    "brain_fallback": fallback.get("brain_fallback"),
                    "receipts": fallback.get("receipts") or [],
                },
            })
        return _out(_receipt(row, spoken=runner.inline_result_text(row)))
    budget = (
        float(wait_seconds)
        if wait_seconds is not None
        else (1.5 if live_session_id else float(getattr(settings, "cognitive_work_timeout_seconds", 90.0) or 90.0))
    )
    row = await runner.wait_task(str(row["id"]), max(0.5, budget))
    if runner.is_inline_finished(row):
        if str(row.get("status") or "") == "failed":
            from app.agent.intake import _brain_dark_local_fallback

            fallback = await _brain_dark_local_fallback(
                text, row, source=source, approved=False
            )
            if fallback is not None:
                runner.mark_consumed(str(row["id"]))
                return _out({
                    "ok": True,
                    "spoken": str(fallback.get("spoken") or "Done.")[:700],
                    "run_id": None,
                    "task_id": row.get("id"),
                    "status": "completed",
                    "steps": len(fallback.get("receipts") or []),
                    "evidence": {
                        "task_id": row.get("id"),
                        "status": "completed",
                        "brain_fallback": fallback.get("brain_fallback"),
                        "receipts": fallback.get("receipts") or [],
                    },
                })
        runner.mark_consumed(str(row["id"]))
        return _out(_receipt(row, spoken=runner.inline_result_text(row)))
    if row.get("status") == "awaiting_approval":
        runner.mark_approval_announced(str(row["id"]))
    return _out(_receipt(row))
