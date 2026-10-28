"""Our own SQLite file in WAL mode. Queue + state + logs, no migrations.

One connection per thread per path, guarded by a lock. Synchronous=NORMAL
keeps single-row commits inside the 1 ms observe budget while WAL keeps
readers unblocked; durability across clean restarts holds.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.persona_mind.paths import mind_db_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS queue (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  turn_id TEXT UNIQUE NOT NULL,
  owner_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, seq);
CREATE TABLE IF NOT EXISTS state (
  owner_id TEXT PRIMARY KEY,
  data TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rejections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  item TEXT NOT NULL,
  reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reflections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  turn_ids TEXT NOT NULL,
  summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS owner_stats (
  owner_id TEXT PRIMARY KEY,
  samples TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config (
  version INTEGER PRIMARY KEY AUTOINCREMENT,
  data TEXT NOT NULL,
  diff TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

_local = threading.local()
_guard = threading.Lock()


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db = str(path or mind_db_path())
    cache = getattr(_local, "conns", None)
    if cache is None:
        cache = {}
        _local.conns = cache
    conn = cache.get(db)
    if conn is None:
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db, timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.executescript(_SCHEMA)
        cache[db] = conn
    return conn


def reset_connections() -> None:
    """Close cached connections (tests switching DB files)."""
    cache = getattr(_local, "conns", None)
    if cache:
        for conn in cache.values():
            with suppress(Exception):
                conn.close()
        cache.clear()


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def enqueue_turn(record: Any, *, path: Path | None = None) -> None:
    from app.persona_mind.types import TurnRecord

    assert isinstance(record, TurnRecord)
    payload = json.dumps(
        {
            "owner_id": record.owner_id,
            "thread_id": record.thread_id,
            "turn_id": record.turn_id,
            "ts": record.ts.isoformat(),
            "owner_msg": record.owner_msg,
            "evie_reply": record.evie_reply,
            "channel": record.channel,
            "audience": record.audience,
            "tone": dict(record.tone or {}),
            "reaction_to_prev": record.reaction_to_prev,
            "receipts": [list(r) for r in (record.receipts or ())],
            "now_line": record.now_line,
        },
        ensure_ascii=False,
    )
    with _guard:
        # One autocommit statement: atomic on its own, and the minimum
        # round-trips for the 1 ms observe budget.
        _connect(path).execute(
            "INSERT OR IGNORE INTO queue"
            " (turn_id, owner_id, thread_id, ts, payload) VALUES (?,?,?,?,?)",
            (record.turn_id, record.owner_id, record.thread_id,
             record.ts.isoformat(), payload),
        )


def queued_turns(limit: int = 100, *, path: Path | None = None) -> list[dict[str, Any]]:
    with _guard:
        conn = _connect(path)
        rows = conn.execute(
            "SELECT payload FROM queue WHERE status='queued' ORDER BY seq LIMIT ?",
            (max(1, limit),),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def recent_done_turns(
    owner_id: str, limit: int = 5, *, path: Path | None = None
) -> list[dict[str, Any]]:
    """Most recent processed turns, oldest first (reflector context)."""
    with _guard:
        conn = _connect(path)
        rows = conn.execute(
            "SELECT payload FROM queue WHERE status='done' AND owner_id=?"
            " ORDER BY seq DESC LIMIT ?",
            (owner_id, max(1, limit)),
        ).fetchall()
    return [json.loads(row[0]) for row in reversed(rows)]


def queued_count(*, path: Path | None = None) -> int:
    with _guard:
        conn = _connect(path)
        row = conn.execute(
            "SELECT COUNT(*) FROM queue WHERE status='queued'"
        ).fetchone()
    return int(row[0])


def mark_done(turn_ids: list[str], *, path: Path | None = None) -> None:
    if not turn_ids:
        return
    with _guard:
        conn = _connect(path)
        conn.execute("BEGIN IMMEDIATE;")
        try:
            conn.executemany(
                "UPDATE queue SET status='done' WHERE turn_id=?",
                [(tid,) for tid in turn_ids],
            )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise


def turn_exists(turn_id: str, *, path: Path | None = None) -> bool:
    with _guard:
        conn = _connect(path)
        row = conn.execute(
            "SELECT 1 FROM queue WHERE turn_id=? LIMIT 1", (turn_id,)
        ).fetchone()
    return row is not None


def turn_text(turn_id: str, *, path: Path | None = None) -> tuple[str, str] | None:
    """(owner_msg, evie_reply) for a known turn_id, else None."""
    with _guard:
        conn = _connect(path)
        row = conn.execute(
            "SELECT payload FROM queue WHERE turn_id=? LIMIT 1", (turn_id,)
        ).fetchone()
    if row is None:
        return None
    payload = json.loads(row[0])
    return str(payload.get("owner_msg") or ""), str(payload.get("evie_reply") or "")


def get_state(owner_id: str, *, path: Path | None = None) -> dict[str, Any] | None:
    with _guard:
        conn = _connect(path)
        row = conn.execute(
            "SELECT data, version, updated_at FROM state WHERE owner_id=?",
            (owner_id,),
        ).fetchone()
    if row is None:
        return None
    return {"data": json.loads(row[0]), "version": int(row[1]), "updated_at": row[2]}


def put_state(owner_id: str, data: dict[str, Any], *, path: Path | None = None) -> int:
    """Upsert state, bump version, return the new version."""
    body = json.dumps(data, ensure_ascii=False)
    with _guard:
        conn = _connect(path)
        conn.execute("BEGIN IMMEDIATE;")
        try:
            row = conn.execute(
                "SELECT version FROM state WHERE owner_id=?", (owner_id,)
            ).fetchone()
            version = int(row[0]) + 1 if row else 1
            conn.execute(
                "INSERT INTO state (owner_id, data, version, updated_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(owner_id) DO UPDATE SET data=excluded.data,"
                " version=excluded.version, updated_at=excluded.updated_at",
                (owner_id, body, version, _utcnow()),
            )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
    return version


def state_version(owner_id: str, *, path: Path | None = None) -> int:
    with _guard:
        conn = _connect(path)
        row = conn.execute(
            "SELECT version FROM state WHERE owner_id=?", (owner_id,)
        ).fetchone()
    return int(row[0]) if row else 0


def meta_get(key: str, default: str = "", *, path: Path | None = None) -> str:
    with _guard:
        conn = _connect(path)
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def meta_set(key: str, value: str, *, path: Path | None = None) -> None:
    with _guard:
        conn = _connect(path)
        conn.execute("BEGIN IMMEDIATE;")
        try:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise


def reflection_count(*, path: Path | None = None) -> int:
    try:
        return int(meta_get("reflection_count", "0", path=path))
    except ValueError:
        return 0


def bump_reflection_count(*, path: Path | None = None) -> int:
    count = reflection_count(path=path) + 1
    meta_set("reflection_count", str(count), path=path)
    return count


def log_rejection(kind: str, item: str, reason: str, *, path: Path | None = None) -> None:
    with _guard:
        conn = _connect(path)
        conn.execute("BEGIN IMMEDIATE;")
        try:
            conn.execute(
                "INSERT INTO rejections (ts, kind, item, reason) VALUES (?,?,?,?)",
                (_utcnow(), kind, item[:500], reason[:500]),
            )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise


def rejection_stats(*, path: Path | None = None) -> dict[str, int]:
    with _guard:
        conn = _connect(path)
        rows = conn.execute(
            "SELECT kind || ':' || reason, COUNT(*) FROM rejections GROUP BY 1"
        ).fetchall()
    return {row[0]: int(row[1]) for row in rows}


def get_owner_samples(owner_id: str, *, path: Path | None = None) -> list[dict[str, Any]]:
    with _guard:
        conn = _connect(path)
        row = conn.execute(
            "SELECT samples FROM owner_stats WHERE owner_id=?", (owner_id,)
        ).fetchone()
    if row is None:
        return []
    try:
        samples = json.loads(row[0])
    except ValueError:
        return []
    return list(samples) if isinstance(samples, list) else []


def put_owner_samples(
    owner_id: str, samples: list[dict[str, Any]], *, path: Path | None = None
) -> None:
    body = json.dumps(list(samples)[-200:])
    with _guard:
        conn = _connect(path)
        conn.execute(
            "INSERT INTO owner_stats (owner_id, samples, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(owner_id) DO UPDATE SET samples=excluded.samples,"
            " updated_at=excluded.updated_at",
            (owner_id, body, _utcnow()),
        )


def recent_turns(owner_id: str, limit: int = 5, *, path: Path | None = None) -> list[dict[str, Any]]:
    """Most recent turns regardless of status, oldest first."""
    with _guard:
        conn = _connect(path)
        rows = conn.execute(
            "SELECT payload FROM queue WHERE owner_id=? ORDER BY seq DESC LIMIT ?",
            (owner_id, max(1, limit)),
        ).fetchall()
    return [json.loads(row[0]) for row in reversed(rows)]


def put_config(data: dict[str, Any], diff: str, *, path: Path | None = None) -> int:
    with _guard:
        conn = _connect(path)
        cursor = conn.execute(
            "INSERT INTO config (data, diff, created_at) VALUES (?,?,?)",
            (json.dumps(data, ensure_ascii=False), diff[:500], _utcnow()),
        )
        return int(cursor.lastrowid or 0)


def get_config(version: int, *, path: Path | None = None) -> dict[str, Any] | None:
    with _guard:
        conn = _connect(path)
        row = conn.execute(
            "SELECT data, diff, created_at FROM config WHERE version=?", (version,)
        ).fetchone()
    if row is None:
        return None
    return {"data": json.loads(row[0]), "diff": row[1], "created_at": row[2]}


def list_configs(*, path: Path | None = None) -> list[dict[str, Any]]:
    with _guard:
        conn = _connect(path)
        rows = conn.execute(
            "SELECT version, diff, created_at FROM config ORDER BY version"
        ).fetchall()
    return [
        {"version": int(row[0]), "diff": row[1], "created_at": row[2]} for row in rows
    ]


def log_reflection(turn_ids: list[str], summary: dict[str, Any], *, path: Path | None = None) -> None:
    with _guard:
        conn = _connect(path)
        conn.execute("BEGIN IMMEDIATE;")
        try:
            conn.execute(
                "INSERT INTO reflections (ts, turn_ids, summary) VALUES (?,?,?)",
                (_utcnow(), json.dumps(turn_ids), json.dumps(summary, default=str)),
            )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
