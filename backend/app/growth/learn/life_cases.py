"""GE4 LIFE cases: approved eval cases that run forever.

v1 runner scope (honest): lane-level files.* and sql-read cases executed
in-process on scratch state (same isolation as the journeys adapter).
Approved cases outside that scope stay approved-but-unrunnable and are
reported as skips with the exact reason — never faked, never dropped.

Case schema: {id, title, lane, op, args, expect: {ok, verified}, setup_files}
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS life_cases(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, candidate_id INTEGER NOT NULL,
  title TEXT NOT NULL, lane TEXT NOT NULL, op TEXT NOT NULL,
  args TEXT NOT NULL DEFAULT '{}', expect TEXT NOT NULL DEFAULT '{}');
"""

RUNNABLE = {("files", "create"), ("files", "read"), ("files", "list"),
            ("files", "delete"), ("files", "undo"), ("sql", "read")}


def _db():
    from ..store import _connect

    conn = _connect()
    conn.executescript(SCHEMA)
    return conn


def add(candidate_id: int, title: str, lane: str, op: str, args: dict, expect: dict) -> int:
    from ..result import utcnow

    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO life_cases(ts, candidate_id, title, lane, op, args, expect)"
            " VALUES(?,?,?,?,?,?,?)",
            (utcnow(), candidate_id, title, lane, op, json.dumps(args), json.dumps(expect)))
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def all_cases() -> list[dict]:
    conn = _db()
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM life_cases ORDER BY id").fetchall()]
    finally:
        conn.close()


def run_case(case: dict) -> tuple[str, str]:
    """Run one case on scratch state. Returns (verdict, detail) with verdict
    in {pass, fail, skip}. Never touches live state."""
    lane, op = case.get("lane", ""), case.get("op", "")
    if (lane, op) not in RUNNABLE:
        return "skip", f"runner v1 covers lane-level files/sql only (case is {lane}.{op})"
    try:
        args = json.loads(case.get("args") or "{}")
        expect = json.loads(case.get("expect") or "{}")
    except ValueError:
        return "fail", "case args/expect not valid JSON"
    tmp = Path(tempfile.mkdtemp(prefix="ge-life-"))
    try:
        if lane == "files":
            from app.fabric import files as F
            root = tmp / "fs"
            root.mkdir()
            for name, body in (args.get("setup_files") or {}).items():
                (root / name).write_text(str(body))
            fn = {"create": F.create, "read": F.read_file, "list": F.list_files,
                  "delete": F.delete, "undo": F.undo}.get(op)
            if fn is None:
                return "skip", f"no files.{op} entry"
            call = dict(args.get("call") or {})
            r = fn(*call.get("args", []), root=root, **call.get("kwargs", {}))
        else:  # sql read
            from app.fabric import sql_lane as SQL
            db = str(tmp / "scratch.db")
            sqlite3.connect(db).execute("CREATE TABLE t(a TEXT)").connection.commit()
            for stmt in args.get("setup_sql") or []:
                sqlite3.connect(db).execute(stmt).connection.commit()
            r = SQL.execute(str(args.get("sql") or "SELECT * FROM t"), db_path=db, root=tmp)
        if not isinstance(r, dict):
            return "fail", f"lane returned non-dict {type(r).__name__}"
        for key in ("ok", "verified"):
            if key in expect and bool(r.get(key)) != bool(expect[key]):
                return "fail", f"expected {key}={expect[key]}, got {r.get(key)}"
        return "pass", ""
    except Exception as e:  # noqa: BLE001 - case failure is data
        return "fail", f"{type(e).__name__}: {e}"[:200]
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def run_all() -> dict:
    cases = all_cases()
    results = []
    for c in cases:
        verdict, detail = run_case(c)
        results.append({"id": c["id"], "title": c["title"], "verdict": verdict,
                        "detail": detail})
    return {"cases": results,
            "n": len(results),
            "passed": sum(1 for r in results if r["verdict"] == "pass"),
            "failed": sum(1 for r in results if r["verdict"] == "fail"),
            "skipped": sum(1 for r in results if r["verdict"] == "skip")}
