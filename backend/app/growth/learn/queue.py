"""GE4 failure queue: redacted failure records from real life.

Signals (spec): failed/unverified receipts, undo within 10 min of an
action, owner corrections, same-request-rephrased within 2 min,
cancelled/abandoned turns, timeouts, capability gaps, explicit `ev bad`
or HUD/phone thumbs-down, denied approvals.

Every record is redacted at intake (boundary rules: secrets and
never-store classes never land here). Schema: ts, source, utterance,
trace_ref, policy, receipts summary, world summary, cluster_id (nightly).
"""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS failures(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL,
  utterance TEXT NOT NULL DEFAULT '', trace_ref TEXT NOT NULL DEFAULT '',
  policy TEXT NOT NULL DEFAULT '', receipts TEXT NOT NULL DEFAULT '',
  world TEXT NOT NULL DEFAULT '', cluster_id TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_failures_ts ON failures(ts);
CREATE INDEX IF NOT EXISTS idx_failures_source ON failures(source);
"""


def _db():
    from ..store import _connect

    conn = _connect()
    conn.executescript(SCHEMA)
    return conn


def redact(text: str, limit: int = 500) -> str:
    try:
        from app.security.boundary import strip_for_model
        return str(strip_for_model(text or "")[:limit])
    except Exception:
        return (text or "")[:limit]


def record(source: str, *, utterance: str = "", trace_ref: str = "",
           policy: str = "", receipts: str = "", world: str = "") -> int:
    from ..result import utcnow

    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO failures(ts, source, utterance, trace_ref, policy, receipts, world)"
            " VALUES(?,?,?,?,?,?,?)",
            (utcnow(), source, redact(utterance), redact(trace_ref, 200),
             redact(policy, 300), redact(receipts, 1000), redact(world, 500)))
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def list_recent(n: int = 100) -> list[dict]:
    import sqlite3

    conn = _db()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM failures ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_by_source(since_days: int = 7) -> dict[str, int]:
    from datetime import UTC, datetime, timedelta


    cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT source, COUNT(*) c FROM failures WHERE ts >= ? GROUP BY source",
            (cutoff,)).fetchall()
        return {s: c for s, c in rows}
    finally:
        conn.close()
