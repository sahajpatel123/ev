"""GE6 P2 queue: the machine-readable improvement backlog.

Seeded once from the release report's Remaining-P2 list; the owner or the
captain adds new items; the improve loop resolves them with a branch.
"""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS p2(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
  resolved_by TEXT NOT NULL DEFAULT '');
"""

SEED = [
    ("AX-fallback latency (T2)",
     "T2 skip-ad via the AX fallback path is slow (~2.5s AX-tree lag seen in release) and flakes "
     "under load (live.t2.slow_page). Measure the AX path, root-cause, fix, prove with before/after "
     "p95 on the T2 fixture battery."),
    ("B4 tool surface 46-vs-14-18 + read_result",
     "Consolidate to 14-18 orthogonal tools with full structured results + read_result. NOTE: Mind 1.1 "
     "M2 (46->17) may already address this; verify before working."),
    ("notebook", "Owner notebook with provenance + verified delete (B3 remainder)."),
    ("claim monitor", "Catch >=95% of unverified claims on the adversarial Live double (C2)."),
    ("hard delete (R14)", "Verified hard deletes across stores."),
    ("transcript presence frames", "NOT-BUILT frames in the presence stream."),
    ("corpus-OCR bypass hole",
     "OCR-derived resume commands are typed into live Ghostty terminals with no untrusted-content "
     "screening, no session grounding, no confirmation (app/ev/terminal_continuity.py "
     "resume_from_screenshot + extract_resume_commands). Screen (injection patterns + shell "
     "metachar DENY), ground session ids against local stores, refuse-or-flag mismatches, "
     "CONFIRM ungrounded. Regression tests with crafted OCR lines."),
    ("egress-100% + cost-±5% proofs", "Prove egress ledger covers 100% of remote calls and cost ledger within 5% (GE8 check)."),
    ("barge-in timings", "Playback stop <=300ms p95, brain cancel <=2s, no orphans (C2, needs voice rig)."),
    ("idle CPU app-side", "Measure Evie.app idle CPU (target <=1%)."),
    ("EV_FABRIC=0 legacy path", "Prove the legacy path still works with the fabric off (GE8 check)."),
    ("shell-session live drill", "Live drill of shell sessions (GE8 check)."),
]


def _db():
    from ..store import _connect

    conn = _connect()
    conn.executescript(SCHEMA)
    return conn


def seed() -> int:
    from ..result import utcnow

    conn = _db()
    try:
        n = conn.execute("SELECT COUNT(*) FROM p2").fetchone()[0]
        if n:
            return 0
        for title, detail in SEED:
            conn.execute("INSERT INTO p2(ts, title, detail) VALUES(?,?,?)",
                         (utcnow(), title, detail))
        conn.commit()
        return len(SEED)
    finally:
        conn.close()


def add(title: str, detail: str = "") -> int:
    from ..result import utcnow

    conn = _db()
    try:
        cur = conn.execute("INSERT INTO p2(ts, title, detail) VALUES(?,?,?)",
                           (utcnow(), title, detail))
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def open_items() -> list[dict]:
    import sqlite3

    conn = _db()
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in
                conn.execute("SELECT * FROM p2 WHERE status='open' ORDER BY id").fetchall()]
    finally:
        conn.close()


def get(pid: int) -> dict | None:
    import sqlite3

    conn = _db()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM p2 WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def resolve(pid: int, resolved_by: str) -> None:
    conn = _db()
    try:
        conn.execute("UPDATE p2 SET status='resolved', resolved_by=? WHERE id=?",
                     (resolved_by, pid))
        conn.commit()
    finally:
        conn.close()
