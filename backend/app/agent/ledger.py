"""Durable task ledger for agent work that outlives one turn or one process.

Why this exists: a conversational turn must never say "submitted / I'll inform
you" unless something is actually running and can be inspected later. The
ledger is the source of truth for that claim:

- every delegated goal gets a row before any runner starts;
- every runner mirrors its live state (status, last step, approvals) into it;
- a process restart marks orphaned rows ``interrupted`` instead of leaving
  them "running" forever;
- status/cancel/approval intents read it without asking a model.

Storage is a small SQLite database (stdlib, WAL, no ORM migration) so the
ledger works in the API process, the CLI, and tests with the same code.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.config import settings

ACTIVE_STATUSES = ("running", "awaiting_approval")
# ``needs_input`` is terminal for one run: the agent stopped and asked the
# owner a question. It is not a failure, and the next turn starts a new run.
FINAL_STATUSES = ("completed", "failed", "cancelled", "interrupted", "needs_input")
ALL_STATUSES = ACTIVE_STATUSES + FINAL_STATUSES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    goal TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'chat',
    status TEXT NOT NULL DEFAULT 'running',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL,
    pid INTEGER,
    boot_id TEXT,
    last_event TEXT,
    steps INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    summary TEXT,
    error TEXT,
    approval_tool TEXT,
    approval_args TEXT,
    approval_notified INTEGER NOT NULL DEFAULT 0,
    notified INTEGER NOT NULL DEFAULT 0,
    decision TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
"""


def db_path() -> Path:
    raw = str(getattr(settings, "agent_state_db", "") or "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".ev" / "agent"
    path = base if base.suffix == ".db" else base / "tasks.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextlib.contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """One short-lived connection, always closed.

    ``with sqlite3.connect(...)`` commits but does NOT close; the connection
    lives until GC and sqlite's internal reference cycles kept hundreds of
    FDs open in the live API (Errno 24 killed every later write). This
    context manager commits on success, rolls back on error, and closes in
    every case.
    """
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    with _db() as conn:
        conn.executescript(_SCHEMA)
        # Additive columns for ledgers created by an earlier build.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        for column, ddl in (
            ("approval_notified", "INTEGER NOT NULL DEFAULT 0"),
            ("notified", "INTEGER NOT NULL DEFAULT 0"),
            ("decision", "TEXT"),
            ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {ddl}")


def reset_for_tests() -> None:
    """Delete all rows. Tests only: parked approvals must not leak across files."""
    init()
    with _db() as conn:
        conn.execute("DELETE FROM tasks")
        conn.commit()


def new_id() -> str:
    return f"task-{uuid.uuid4().hex[:12]}"


def create(
    *,
    goal: str,
    source: str = "chat",
    run_id: str | None = None,
    pid: int | None = None,
    boot_id: str | None = None,
    status: str = "running",
) -> dict[str, Any]:
    init()
    now = time.time()
    task_id = new_id()
    with _db() as conn:
        conn.execute(
            "INSERT INTO tasks (id, run_id, goal, source, status, created_at, updated_at,"
            " pid, boot_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, run_id, goal[:8000], source[:32], status, now, now, pid, boot_id),
        )
    return get(task_id) or {}


def update(task_id: str, **fields: Any) -> None:
    allowed = {
        "run_id", "status", "last_event", "steps", "tool_calls", "summary", "error",
        "approval_tool", "approval_args", "approval_notified", "finished_at",
        "notified", "pid", "boot_id", "decision", "cancel_requested",
    }
    data = {key: value for key, value in fields.items() if key in allowed}
    if not data:
        return
    if isinstance(data.get("approval_args"), (dict, list)):
        data["approval_args"] = json.dumps(data["approval_args"], default=str)
    if "status" in data and data["status"] not in ALL_STATUSES:
        raise ValueError(f"unknown task status {data['status']!r}")
    data["updated_at"] = time.time()
    if data.get("status") in FINAL_STATUSES and "finished_at" not in data:
        data["finished_at"] = time.time()
    columns = ", ".join(f"{key} = ?" for key in data)
    with _db() as conn:
        conn.execute(
            f"UPDATE tasks SET {columns} WHERE id = ?",
            (*data.values(), task_id),
        )


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    raw = data.get("approval_args")
    if raw:
        try:
            data["approval_args"] = json.loads(raw)
        except ValueError:
            data["approval_args"] = {}
    else:
        data["approval_args"] = {}
    return data


def get(task_id: str) -> dict[str, Any] | None:
    init()
    with _db() as conn:
        return _row(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def status_of(task_id: str | None) -> str | None:
    """Best-effort task status for turn responses (None when unknown)."""

    if not task_id:
        return None
    try:
        row = get(str(task_id))
    except Exception:  # noqa: BLE001 - a status read never breaks a turn
        return None
    if row is None:
        return None
    return str(row.get("status") or "") or None


def by_run_id(run_id: str) -> dict[str, Any] | None:
    init()
    with _db() as conn:
        return _row(
            conn.execute("SELECT * FROM tasks WHERE run_id = ?", (run_id,)).fetchone()
        )


def list_tasks(*, active: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    init()
    limit = max(1, min(int(limit), 200))
    with _db() as conn:
        if active:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status IN (?, ?) ORDER BY created_at DESC LIMIT ?",
                (*ACTIVE_STATUSES, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return [item for item in (_row(row) for row in rows) if item]


def active_count() -> int:
    init()
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN (?, ?)", ACTIVE_STATUSES
        ).fetchone()
    return int(row[0] if row else 0)


def task_since(stamp: float) -> bool:
    """True when any task row was created at or after ``stamp`` (epoch seconds)."""
    init()
    with _db() as conn:
        row = conn.execute(
            "SELECT 1 FROM tasks WHERE created_at >= ? LIMIT 1", (float(stamp),)
        ).fetchone()
    return row is not None


def pending_approval() -> dict[str, Any] | None:
    """Newest task waiting for an owner decision, or None."""
    init()
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE status = 'awaiting_approval'"
            " ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return _row(row)


def newest_active() -> dict[str, Any] | None:
    init()
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE status IN (?, ?) ORDER BY created_at DESC LIMIT 1",
            ACTIVE_STATUSES,
        ).fetchone()
    return _row(row)


def pid_alive(pid: int | None) -> bool:
    """True when the recorded runner PID still exists on this host."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def mark_orphans(boot_id: str) -> int:
    """Fail rows whose runner process is gone; never a live other worker.

    Multi-process deployments (API + workers) each have their own boot id, so
    a boot-id mismatch alone is not proof of death. The PID check is.
    """
    init()
    now = time.time()
    changed = 0
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, pid FROM tasks WHERE status IN (?, ?)"
            " AND (boot_id IS NULL OR boot_id != ?)",
            (*ACTIVE_STATUSES, boot_id),
        ).fetchall()
        for row in rows:
            if pid_alive(row["pid"]):
                continue
            conn.execute(
                "UPDATE tasks SET status = 'interrupted', updated_at = ?, finished_at = ?,"
                " error = COALESCE(error, 'the process running this task stopped')"
                " WHERE id = ?",
                (now, now, row["id"]),
            )
            changed += 1
    return changed


def prune(days: int = 14, keep: int = 200) -> int:
    """Drop old finished rows; active rows are never touched."""
    init()
    cutoff = time.time() - max(1, days) * 86400
    with _db() as conn:
        cursor = conn.execute(
            "DELETE FROM tasks WHERE finished_at IS NOT NULL AND finished_at < ?"
            " AND id NOT IN (SELECT id FROM tasks ORDER BY created_at DESC LIMIT ?)",
            (cutoff, max(0, keep)),
        )
        return int(cursor.rowcount or 0)


def status_summary(limit: int = 5) -> dict[str, Any]:
    """Credential-free snapshot for the owner and for health surfaces."""
    tasks = list_tasks(limit=limit)
    active = [task for task in tasks if task["status"] in ACTIVE_STATUSES]
    recent = [task for task in tasks if task["status"] in FINAL_STATUSES]
    return {
        "active": active,
        "recent": recent,
        "active_count": active_count(),
        "path": str(db_path()),
    }
