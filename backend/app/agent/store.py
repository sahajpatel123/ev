"""Run traces: one JSON file per agent run, written atomically.

The trace is the audit surface for the agent runtime: goal, brain choice,
every step with policy/approval/receipt, budgets, and the final summary.
Nothing secret is written — tool arguments drop ``content`` bodies (their
sha256 lives in the receipt) and shell stdout is already redacted by the
shell lane before it reaches a receipt.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.config import settings


def trace_dir() -> Path:
    configured = str(getattr(settings, "agent_trace_dir", "") or "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".ev" / "agent"
    path = base / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_id(run_id: str) -> str:
    return "".join(ch for ch in str(run_id) if ch.isalnum() or ch in "-_")[:80] or "run"


def write_run(result: dict[str, Any]) -> Path | None:
    """Persist one finished run. Returns the path, or None on a write failure."""
    payload = dict(result)
    payload.setdefault("persisted_at", time.time())
    path = trace_dir() / f"{_safe_id(str(payload.get('run_id') or 'run'))}.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, default=str, ensure_ascii=False, indent=2))
        tmp.replace(path)
        return path
    except OSError:
        return None


def load_run(run_id: str) -> dict[str, Any] | None:
    path = trace_dir() / f"{_safe_id(run_id)}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    """Newest first, summary fields only (no step payloads).

    Only stable summary keys are read: `usage` changed shape in 0.2
    (last-call dict -> aggregated summary) and old traces still parse.
    """
    rows: list[dict[str, Any]] = []
    try:
        files = sorted(trace_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    for path in files[: max(1, min(limit, 500))]:
        if path.name.endswith(".tmp"):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        rows.append({
            "run_id": data.get("run_id"),
            "goal": data.get("goal"),
            "status": data.get("status"),
            "ok": data.get("ok"),
            "summary": str(data.get("summary") or "")[:300],
            "brain": data.get("brain"),
            "started_at": data.get("started_at"),
            "elapsed_ms": data.get("elapsed_ms"),
            "tool_calls": data.get("tool_calls"),
        })
    return rows
