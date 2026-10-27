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
  world TEXT NOT NULL DEFAULT '', cluster_id TEXT NOT NULL DEFAULT '',
  prov TEXT NOT NULL DEFAULT 'unstamped');
CREATE INDEX IF NOT EXISTS idx_failures_ts ON failures(ts);
CREATE INDEX IF NOT EXISTS idx_failures_source ON failures(source);
CREATE INDEX IF NOT EXISTS idx_failures_prov ON failures(prov);
"""


def _db():
    from ..store import _connect

    conn = _connect()
    conn.executescript(SCHEMA)
    cols = [c[1] for c in conn.execute("PRAGMA table_info(failures)")]
    if "prov" not in cols:  # v1.1 migration: pre-stamp rows stay 'unstamped'
        conn.execute("ALTER TABLE failures ADD COLUMN prov TEXT NOT NULL DEFAULT 'unstamped'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_failures_prov ON failures(prov)")
        conn.commit()
    return conn


def count_by_prov() -> dict[str, int]:
    conn = _db()
    try:
        return {r[0]: r[1] for r in
                conn.execute("SELECT prov, COUNT(*) FROM failures GROUP BY prov")}
    finally:
        conn.close()


def redact(text: str, limit: int = 500) -> str:
    try:
        from app.security.boundary import strip_for_model
        return str(strip_for_model(text or "")[:limit])
    except Exception:
        return (text or "")[:limit]


def record(source: str, *, utterance: str = "", trace_ref: str = "",
           policy: str = "", receipts: str = "", world: str = "",
           prov: str = "unstamped") -> int:
    from ..result import utcnow

    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO failures(ts, source, utterance, trace_ref, policy, receipts, world, prov)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (utcnow(), source, redact(utterance), redact(trace_ref, 200),
             redact(policy, 300), redact(receipts, 1000), redact(world, 500),
             prov[:24] if isinstance(prov, str) else "unstamped"))
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def list_recent(n: int = 100, *, prov: str | None = "owner") -> list[dict]:
    """Recent failures; default owner-provenanced only (v1.1).

    Pass prov=None for the unfiltered set (quarantine audits only).
    """
    import sqlite3

    conn = _db()
    try:
        conn.row_factory = sqlite3.Row
        if prov is None:
            rows = conn.execute(
                "SELECT * FROM failures ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM failures WHERE prov = ? ORDER BY id DESC LIMIT ?",
                (prov, n)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_by_source(since_days: int = 7, *, prov: str | None = "owner") -> dict[str, int]:
    from datetime import UTC, datetime, timedelta


    cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
    conn = _db()
    try:
        if prov is None:
            rows = conn.execute(
                "SELECT source, COUNT(*) c FROM failures WHERE ts >= ? GROUP BY source",
                (cutoff,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT source, COUNT(*) c FROM failures WHERE ts >= ? AND prov = ? GROUP BY source",
                (cutoff, prov)).fetchall()
        return {s: c for s, c in rows}
    finally:
        conn.close()
