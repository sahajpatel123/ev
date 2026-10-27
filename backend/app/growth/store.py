"""Append-only metrics store: ~/.ev/growth/metrics.sqlite (override: growth_store_dir)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS results(
  id INTEGER PRIMARY KEY,
  suite TEXT NOT NULL, version INTEGER NOT NULL,
  "commit" TEXT NOT NULL, date TEXT NOT NULL,
  started TEXT NOT NULL, finished TEXT NOT NULL,
  n INTEGER, passed INTEGER, failed INTEGER, skipped INTEGER, errors INTEGER,
  cost_usd REAL, status TEXT, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_results_suite_date ON results(suite, date);
CREATE TABLE IF NOT EXISTS releases(tag TEXT PRIMARY KEY, "commit" TEXT, date TEXT, notes TEXT);
"""


def store_dir() -> Path:
    try:
        from app.config import settings

        raw = str(getattr(settings, "growth_store_dir", "") or "")
        if raw:
            return Path(raw).expanduser()
    except Exception:
        pass
    return Path.home() / ".ev" / "growth"


def db_path() -> Path:
    d = store_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "metrics.sqlite"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()))
    conn.executescript(SCHEMA)
    return conn


def record(res: dict) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            'INSERT INTO results(suite, version, "commit", date, started, finished,'
            " n, passed, failed, skipped, errors, cost_usd, status, json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (res.get("suite"), res.get("version", 1), res.get("commit"),
             (res.get("started") or "")[:10], res.get("started"), res.get("finished"),
             res.get("n"), res.get("passed"), res.get("failed"), res.get("skipped"),
             res.get("errors"), res.get("cost_usd"), res.get("status"),
             json.dumps(res)))
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def recent(suite: str, n: int = 5) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT json FROM results WHERE suite=? ORDER BY id DESC LIMIT ?",
            (suite, n)).fetchall()
        return [json.loads(r[0]) for r in rows]
    finally:
        conn.close()


def tag_release(tag: str, commit: str, notes: str = "") -> None:
    from .result import utcnow

    conn = _connect()
    try:
        conn.execute('INSERT OR REPLACE INTO releases(tag, "commit", date, notes)'
                     " VALUES(?,?,?,?)", (tag, commit, utcnow()[:10], notes))
        conn.commit()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="store")
    ap.add_argument("--suite", required=True)
    ap.add_argument("--last", type=int, default=5)
    a = ap.parse_args(argv)
    for r in recent(a.suite, a.last):
        print(f"{r.get('started','?')[:16]} {r.get('commit','?')} {r.get('status','?')} "
              f"n={r.get('n')} pass={r.get('passed')} fail={r.get('failed')} "
              f"ci={r.get('wilson95')} cost={r.get('cost_usd')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
