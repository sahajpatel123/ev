"""SQL lane: sqlite registry, classification, guarded execution with audit."""
from __future__ import annotations
import re, sqlite3, time
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
    # DML preview or require confirm
    if preview and not confirm:
        return {"ok": True, "verified": False, "evidence": {"preview": True, "kind": kind},
                "error": None, "undo": None}
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
        conn.executescript(sql)
        conn.commit()
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return {"ok": False, "verified": False, "evidence": {"rolled_back": True},
                "error": str(e), "undo": None}
    finally:
        conn.close()
    _audit(rp, {"op": kind, "sql": sql, "confirmed": True})
    undo = {"snapshot": snap} if kind == "ddl" and snap else ({"rolled_back_possible": False} if kind == "dml" else None)
    return {"ok": True, "verified": True, "evidence": {"kind": kind}, "error": None, "undo": undo}
