"""GE4 candidates: drafted eval cases (world setup + PENDING-REVIEW oracle).

Drafts are templates, never tests: the oracle stays PENDING-REVIEW until
the owner approves it in `ev review`. Approved cases join the LIFE suite
permanently (see life_cases.py).
"""

from __future__ import annotations

import json

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, cluster_id TEXT NOT NULL,
  title TEXT NOT NULL, setup TEXT NOT NULL DEFAULT '{}',
  oracle TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending');
CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
"""


def _db():
    from ..store import _connect

    conn = _connect()
    conn.executescript(SCHEMA)
    return conn


def draft(cluster_id: str, title: str, setup: dict, oracle: dict) -> int:
    from ..result import utcnow

    oracle = {"verdict": "PENDING-REVIEW", **oracle}
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO candidates(ts, cluster_id, title, setup, oracle) VALUES(?,?,?,?,?)",
            (utcnow(), cluster_id, title, json.dumps(setup), json.dumps(oracle)))
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def draft_from_cluster(cluster_id: str, members: list[dict]) -> int:
    first = members[0]
    setup = {"kind": "replay-failure",
             "source": first.get("source", ""),
             "receipts": str(first.get("receipts") or "")[:300],
             "utterance": str(first.get("utterance") or "")[:300],
             "members": len(members)}
    oracle = {"expect": "owner decides: what SHOULD happen",
              "hint": f"failure was: {first.get('source')}; policy was: {first.get('policy')}"}
    return draft(cluster_id, f"{first.get('source')}: {str(first.get('receipts') or '')[:80]}",
                 setup, oracle)


def pending() -> list[dict]:
    import sqlite3

    conn = _db()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM candidates WHERE status='pending' ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_status(cid: int, status: str) -> None:
    assert status in ("pending", "approved", "rejected", "quarantined")
    conn = _db()
    try:
        conn.execute("UPDATE candidates SET status=? WHERE id=?", (status, cid))
        conn.commit()
    finally:
        conn.close()


def quarantine_unprovenanced() -> int:
    """Quarantine pending candidates built from unprovenanced mining input.

    v1.1: pre-stamp candidates came from eval/drill byproduct, not real
    owner turns. Approved cases stay (owner action stands as test intent);
    their 'from real life' story is void — see the v1.1 prep log.
    Returns the number quarantined.
    """
    conn = _db()
    try:
        cur = conn.execute(
            "UPDATE candidates SET status='quarantined' WHERE status='pending'")
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()
