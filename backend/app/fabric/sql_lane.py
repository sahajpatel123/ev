"""SQL lane: sqlite registry, classification, guarded execution with audit."""
from __future__ import annotations

import contextlib
import re
import sqlite3
import time
from pathlib import Path

_DANGEROUS = ("drop", "truncate", "alter", "grant", "revoke")
_DML = ("insert", "update", "delete", "merge", "upsert", "replace")
_DDL = ("create",) + _DANGEROUS

def classify_statement(sql: str) -> str:
    first = (sql.strip().split(None, 1) + [""])[0].lower().rstrip(";")
    if first in ("select", "show", "describe", "explain", "with", "values", "pragma"):
        return "read"
    if first in _DML:
        return "dml"
    return "ddl"

def classify(sql: str) -> str:
    """Multi-statement: most-dangerous wins (ddl > dml > read)."""
    parts = [p for p in sql.split(";") if p.strip()]
    rank = {"read": 0, "dml": 1, "ddl": 2}
    worst, level = "read", 0
    for p in parts:
        c = classify_statement(p)
        if rank[c] > level:
            worst, level = c, rank[c]
    return worst

def _audit(root: Path, entry: dict) -> None:
    try:
        import json
        log = root / "sql_audit.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        entry["ts"] = time.time()
        with open(log, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

def _deny(msg: str) -> dict:
    return {"ok": False, "verified": False, "evidence": {}, "error": msg, "undo": None}

def query_scratch(db_path: str, sql: str, *, root=None) -> list:
    conn = sqlite3.connect(db_path)
    try:
        rows = list(conn.execute(sql))
    finally:
        conn.close()
    if root is not None:
        _audit(Path(str(root)), {"op": "read", "sql": sql, "mode": "AUTO"})
    return rows

def execute(sql: str, *, db_path: str = ":memory:", root=".", prod: bool = False,
            confirm: bool = False, preview: bool = False) -> dict:
    kind = classify(sql)
    if prod:
        return _deny("prod DB DENY: writes to production are not allowed")
    rp = Path(str(root))
    if kind == "read":
        try:
            rows = query_scratch(db_path, sql, root=rp)
            return {"ok": True, "verified": True, "evidence": {"rows": rows, "mode": "AUTO"},
                    "error": None, "undo": None}
        except Exception as e:
            return _deny(str(e))
    # DML preview: execute inside a transaction, count + sample, then roll back.
    if preview and not confirm:
        if kind == "read":
            return {"ok": True, "verified": False,
                    "evidence": {"preview": True, "kind": kind, "note": "reads need no preview"},
                    "error": None, "undo": None}
        try:
            conn = sqlite3.connect(db_path)
            try:
                m = re.search(r"(?i)\b(?:update|delete\s+from|insert\s+into)\s+([\"\w.]+)", sql)
                table = m.group(1) if m else None
                before_rows = None
                if table:
                    try:
                        before_rows = [tuple(r) for r in
                                       conn.execute(f"SELECT * FROM {table}").fetchall()]
                    except Exception:
                        before_rows = None
                conn.execute("BEGIN")
                try:
                    # statement-by-statement: executescript() would COMMIT implicitly.
                    changes_before = conn.total_changes
                    for stmt in [s for s in sql.split(";") if s.strip()]:
                        conn.execute(stmt)
                    affected = conn.total_changes - changes_before
                    sample = []
                    if table:
                        try:
                            sample = [tuple(r) for r in
                                      conn.execute(f"SELECT * FROM {table} LIMIT 3").fetchall()]
                        except Exception:
                            sample = []
                finally:
                    conn.rollback()
                # R7: verify rollback by re-reading the data, not counters.
                rolled_back = True
                if before_rows is not None and table:
                    try:
                        after_rows = [tuple(r) for r in
                                      conn.execute(f"SELECT * FROM {table}").fetchall()]
                        rolled_back = (after_rows == before_rows)
                    except Exception:
                        rolled_back = False
            finally:
                conn.close()
            _audit(rp, {"op": f"{kind}-preview", "sql": sql, "rolled_back": rolled_back})
            return {"ok": True, "verified": rolled_back,
                    "evidence": {"preview": True, "kind": kind, "affected_rows": affected,
                                 "sample": sample, "rolled_back": rolled_back},
                    "error": None, "undo": None}
        except Exception as e:
            return _deny(f"preview failed: {e}")
    if not confirm:
        return {"ok": False, "verified": False, "evidence": {"needs_confirm": True, "kind": kind},
                "error": "confirm required", "undo": None}
    if kind == "ddl":
        # snapshot
        snap = None
        if db_path != ":memory:" and Path(db_path).exists():
            import shutil
            snap = str(rp / "ddl_snapshot.db")
            rp.mkdir(parents=True, exist_ok=True)
            shutil.copy(db_path, snap)
    conn = sqlite3.connect(db_path)
    try:
        before = conn.total_changes
        conn.executescript(sql)
        conn.commit()
        affected = conn.total_changes - before
    except Exception as e:
        with contextlib.suppress(Exception):
            conn.rollback()
        return {"ok": False, "verified": False, "evidence": {"rolled_back": True},
                "error": str(e), "undo": None}
    finally:
        conn.close()
    _audit(rp, {"op": kind, "sql": sql, "confirmed": True, "affected_rows": affected})
    undo = {"snapshot": snap} if kind == "ddl" and snap else ({"rolled_back_possible": False} if kind == "dml" else None)
    return {"ok": True, "verified": True, "evidence": {"kind": kind, "affected_rows": affected},
            "error": None, "undo": undo}
