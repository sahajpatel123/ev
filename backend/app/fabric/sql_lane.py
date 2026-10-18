"""SQL lane: sqlite registry, classification, guarded execution with audit."""
from __future__ import annotations

import contextlib
import re
import shlex
import sqlite3
import time
from pathlib import Path

_DANGEROUS = ("drop", "truncate", "alter", "grant", "revoke")
_DML = ("insert", "update", "delete", "merge", "upsert", "replace")
_DDL = ("create",) + _DANGEROUS
_DDL_EXTRA = ("attach", "detach", "vacuum", "reindex", "copy",
              "begin", "commit", "rollback", "savepoint", "release")
_READ_FIRST = ("select", "show", "describe", "explain", "values", "table")
_TXN_ONLY = ("begin", "commit", "rollback", "savepoint", "release")
_DANGEROUS_FUNCS = ("load_extension", "readfile", "writefile")


def split_statements(sql: str) -> list[str]:
    """Split on semicolons outside strings/comments/dollar-quotes.

    Statements keep their ORIGINAL text (comments and literals intact);
    only the split points are computed, so execution semantics never shift.
    """
    parts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql or "")
    state: str | None = None  # None | 'sq | "dq | `bt | -- | /* | $tag$
    dtag = ""
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if state is None:
            if ch == "'":
                state, buf = "'sq", buf + [ch]
            elif ch == '"':
                state, buf = '"dq', buf + [ch]
            elif ch == "`":
                state, buf = "`bt", buf + [ch]
            elif ch == "-" and nxt == "-":
                state, buf = "--", buf + [ch, nxt]
                i += 1
            elif ch == "/" and nxt == "*":
                state, buf = "/*", buf + [ch, nxt]
                i += 1
            elif ch == "$":
                m = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[i:])
                if m:
                    state, dtag = "$tag$", m.group(0)
                    buf.append(dtag)
                    i += len(dtag) - 1
                else:
                    buf.append(ch)
            elif ch == ";":
                text = "".join(buf).strip()
                if text:
                    parts.append(text)
                buf = []
            else:
                buf.append(ch)
        elif state == "'sq":
            buf.append(ch)
            if ch == "'" and nxt == "'":
                buf.append(nxt)
                i += 1
            elif ch == "'":
                state = None
        elif state == '"dq':
            buf.append(ch)
            if ch == '"' and nxt == '"':
                buf.append(nxt)
                i += 1
            elif ch == '"':
                state = None
        elif state == "`bt":
            buf.append(ch)
            if ch == "`":
                state = None
        elif state == "--":
            buf.append(ch)
            if ch == "\n":
                state = None
        elif state == "/*":
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 1
                state = None
        elif state == "$tag$":
            if sql.startswith(dtag, i):
                buf.append(dtag)
                i += len(dtag) - 1
                state = None
            else:
                buf.append(ch)
        i += 1
    text = "".join(buf).strip()
    if text:
        parts.append(text)
    return parts


def strip_strings_comments(sql: str) -> str:
    """Structural skeleton for classification: literals/comments blanked."""
    out: list[str] = []
    i, n = 0, len(sql or "")
    state: str | None = None
    dtag = ""
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if state is None:
            if ch in "'\"`":
                state = {"'": "'sq", '"': '"dq', "`": "`bt"}[ch]
                out.append(" ")
            elif ch == "-" and nxt == "-":
                state = "--"
                out.append("  ")
                i += 1
            elif ch == "/" and nxt == "*":
                state = "/*"
                out.append("  ")
                i += 1
            elif ch == "$":
                m = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[i:])
                if m:
                    state, dtag = "$tag$", m.group(0)
                    out.append(" " * len(dtag))
                    i += len(dtag) - 1
                else:
                    out.append(ch)
            else:
                out.append(ch)
        elif state in ("'sq", '"dq', "`bt"):
            if state == "'sq":
                closer = "'"
            elif state == '"dq':
                closer = '"'
            else:
                closer = "`"
            out.append(" ")
            if ch == closer and nxt == closer:
                out.append(" ")
                i += 1
            elif ch == closer:
                state = None
        elif state == "--":
            out.append(" " if ch != "\n" else "\n")
            if ch == "\n":
                state = None
        elif state == "/*":
            out.append(" ")
            if ch == "*" and nxt == "/":
                out.append(" ")
                i += 1
                state = None
        elif state == "$tag$":
            if sql.startswith(dtag, i):
                out.append(" " * len(dtag))
                i += len(dtag) - 1
                state = None
            else:
                out.append(" ")
        i += 1
    return "".join(out)


def _first_word(cleaned: str) -> str:
    parts = cleaned.strip().split(None, 1)
    return parts[0].lower().rstrip(";") if parts else ""


def classify_statement(sql: str) -> str:
    """One statement: read | dml | ddl | deny. Unknown fails toward ddl."""
    cleaned = strip_strings_comments(sql)
    low = cleaned.lower()
    for func in _DANGEROUS_FUNCS:
        if re.search(rf"\b{func}\s*\(", low):
            return "deny"
    first = _first_word(cleaned)
    if not first:
        return "read"
    if first in _READ_FIRST:
        return "read"
    if first == "with":
        # Data-modifying CTEs (postgres) hide DML behind WITH.
        if re.search(r"\b(insert|update|delete|merge)\b", low):
            return "dml"
        return "read"
    if first == "pragma":
        rest = cleaned.strip().split(None, 1)
        body = rest[1] if len(rest) > 1 else ""
        if "writable_schema" in body.lower():
            return "deny"
        if "=" in body:
            return "ddl"
        return "read"
    if first in _DML:
        return "dml"
    if first == "copy" and re.search(r"\bprogram\b", low):
        return "deny"
    if first in _DDL or first in _DDL_EXTRA:
        return "ddl"
    return "ddl"


def classify(sql: str) -> str:
    """Multi-statement: most-dangerous wins (deny > ddl > dml > read)."""
    rank = {"read": 0, "dml": 1, "ddl": 2, "deny": 3}
    worst, level = "read", 0
    for part in split_statements(sql):
        if not part.strip():
            continue
        c = classify_statement(part)
        if rank[c] > level:
            worst, level = c, rank[c]
    return worst

def _audit(root: Path, entry: dict) -> None:
    try:
        import json

        from .shell_sessions import redact

        log = root / "sql_audit.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        entry["ts"] = time.time()
        if isinstance(entry.get("sql"), str):
            entry["sql"] = redact(entry["sql"])[:2000]
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

SCRATCH_NAME = ".ev-scratch.db"


def default_db_path(root) -> str:
    rp = Path(str(root))
    rp.mkdir(parents=True, exist_ok=True)
    return str(rp / SCRATCH_NAME)


def execute(sql: str, *, db_path: str | None = None, root=".", prod: bool = False,
            confirm: bool = False, preview: bool = False,
            provenance: str = "owner_turn") -> dict:
    from .policy import UNTRUSTED_PROVENANCE

    kind = classify(sql)
    if kind == "deny":
        return _deny("denied by policy: dangerous SQL construct")
    if prod:
        return _deny("prod DB DENY: writes to production are not allowed")
    if not confirm and not preview and str(provenance or "").lower() in UNTRUSTED_PROVENANCE:
        return {"ok": False, "verified": False,
                "evidence": {"needs_confirm": True, "kind": kind},
                "error": "untrusted provenance needs confirm", "undo": None,
                "confirm": True}
    rp = Path(str(root))
    target = db_path if db_path not in (None, "") else default_db_path(rp)
    if kind == "read":
        try:
            rows = query_scratch(target, sql, root=rp)
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
            conn = sqlite3.connect(target)
            try:
                m = re.search(r"(?i)\b(?:update|delete\s+from|insert\s+into)\s+([\"\w.]+)", strip_strings_comments(sql))
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
                    # statement-by-statement over lexer splits (semicolons in
                    # strings must not fracture statements); executescript()
                    # would COMMIT implicitly. Leading/trailing transaction
                    # verbs are dropped: the preview already wraps in one.
                    changes_before = conn.total_changes
                    stmts = [s for s in split_statements(sql)
                             if _first_word(strip_strings_comments(s)) not in _TXN_ONLY]
                    for stmt in stmts:
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
        if target != ":memory:" and Path(target).exists():
            import shutil
            snap = str(rp / "ddl_snapshot.db")
            rp.mkdir(parents=True, exist_ok=True)
            shutil.copy(target, snap)
    conn = sqlite3.connect(target)
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
    undo: dict | None
    if kind == "ddl" and snap:
        undo = {"snapshot": snap,
                "restore_cmd": f"cp {shlex.quote(snap)} {shlex.quote(target)}"}
    else:
        undo = ({"rolled_back_possible": False} if kind == "dml" else None)
    _audit(rp, {"op": kind, "sql": sql, "confirmed": True, "affected_rows": affected})
    return {"ok": True, "verified": True, "evidence": {"kind": kind, "affected_rows": affected},
            "error": None, "undo": undo}


# --- throwaway Postgres (docker exec psql; no driver dependency) ---

PG_IMAGE = "postgres:16-alpine"
PG_USER = "evie"
PG_DB = "scratch"
_PG_PASSWORD = "ev-scratch-only"
_CONTAINER_RE = re.compile(r"^ev-[a-z0-9][a-z0-9-]*$")


def _docker(*args: str, timeout: int = 120, input_text: str | None = None):
    import subprocess

    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout,
                          input=input_text if input_text is not None else None)


def pg_ensure(container: str = "ev-sql-scratch", timeout_s: int = 180) -> dict:
    """Idempotently create/start/wait a throwaway Postgres container."""
    import shutil

    if shutil.which("docker") is None:
        return {"ok": False, "verified": True, "evidence": {},
                "error": "docker not installed", "undo": None}
    if not _CONTAINER_RE.match(container or ""):
        return {"ok": False, "verified": True, "evidence": {},
                "error": "container name refused (ev-* scratch only)", "undo": None}
    try:
        running = _docker("inspect", "-f", "{{.State.Running}}", container,
                          timeout=30)
        if running.returncode == 0 and running.stdout.strip() == "true":
            return {"ok": True, "verified": True,
                    "evidence": {"container": container, "reused": True},
                    "error": None, "undo": None}
        exists = _docker("inspect", container, timeout=30).returncode == 0
        if exists:
            started = _docker("start", container, timeout=60)
            if started.returncode != 0:
                return {"ok": False, "verified": True, "evidence": {},
                        "error": f"docker start failed: {started.stderr[:200]}",
                        "undo": None}
        else:
            created = _docker("run", "-d", "--rm", "--name", container,
                              "-e", f"POSTGRES_PASSWORD={_PG_PASSWORD}",
                              "-e", f"POSTGRES_USER={PG_USER}",
                              "-e", f"POSTGRES_DB={PG_DB}", PG_IMAGE,
                              timeout=timeout_s)
            if created.returncode != 0:
                return {"ok": False, "verified": True, "evidence": {},
                        "error": f"docker run failed: {created.stderr[:300]}",
                        "undo": None}
        deadline = time.time() + 120
        while time.time() < deadline:
            probe = _docker("exec", container, "pg_isready", "-U", PG_USER,
                            timeout=15)
            if probe.returncode == 0:
                return {"ok": True, "verified": True,
                        "evidence": {"container": container,
                                     "reused": exists},
                        "error": None, "undo": None}
            time.sleep(2.0)
        return {"ok": False, "verified": True, "evidence": {},
                "error": "postgres never became ready", "undo": None}
    except Exception as e:
        return {"ok": False, "verified": True, "evidence": {},
                "error": f"docker failed: {e}", "undo": None}


def _pg_run(container: str, script: str, *, tuples: bool = False,
            timeout: int = 120):
    args = ["exec", "-i", container, "psql", "-U", PG_USER, "-d", PG_DB,
            "-v", "ON_ERROR_STOP=1"]
    if tuples:
        args += ["-tAX", "-F", "\t", "-P", "null=NULL"]
    return _docker(*args, timeout=timeout, input_text=script)


def _pg_tuples(out: str) -> list:
    rows = []
    for line in (out or "").splitlines():
        if line.strip() == "":
            continue
        rows.append(tuple(None if cell == "NULL" else cell
                          for cell in line.split("\t")))
    return rows


def _pg_dump_hash(container: str) -> str | None:
    try:
        dumped = _docker("exec", container, "pg_dump", "-U", PG_USER, "-d", PG_DB,
                         "--no-owner", "--no-privileges", timeout=120)
        if dumped.returncode != 0:
            return None
        import hashlib

        # pg_dump embeds a fresh random \restrict token per run: strip it
        # (session noise, not data) so equal databases hash equal.
        body = "\n".join(
            line for line in dumped.stdout.splitlines()
            if not line.startswith("\\restrict ") and not line.startswith("\\unrestrict "))
        return hashlib.sha256(body.encode()).hexdigest()
    except Exception:
        return None


def pg_execute(sql: str, *, container: str = "ev-sql-scratch", root=".",
               confirm: bool = False, preview: bool = False) -> dict:
    """Throwaway-Postgres mirror of execute(). Container names are fenced."""
    kind = classify(sql)
    if kind == "deny":
        return _deny("denied by policy: dangerous SQL construct")
    rp = Path(str(root))
    ready = pg_ensure(container)
    if not ready.get("ok"):
        return ready
    if kind == "read":
        try:
            proc = _pg_run(container, sql, tuples=True)
        except Exception as e:
            return _deny(str(e))
        if proc.returncode != 0:
            return _deny((proc.stderr or "").strip()[:500] or "query failed")
        rows = _pg_tuples(proc.stdout)
        _audit(rp, {"op": "pg-read", "sql": sql, "mode": "AUTO"})
        return {"ok": True, "verified": True,
                "evidence": {"rows": rows, "mode": "AUTO", "backend": "postgres"},
                "error": None, "undo": None}
    if preview and not confirm:
        before = _pg_dump_hash(container)
        body = "; ".join(s for s in split_statements(sql)
                         if _first_word(strip_strings_comments(s)) not in _TXN_ONLY)
        script = f"BEGIN;\n{body};\nROLLBACK;"
        try:
            proc = _pg_run(container, script)
        except Exception as e:
            return _deny(f"preview failed: {e}")
        after = _pg_dump_hash(container)
        rolled_back = (proc.returncode == 0 and before is not None
                       and after is not None and before == after)
        affected = 0
        for line in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines():
            m = re.match(r"^(?:INSERT \d+ |UPDATE |DELETE )(\d+)\s*$", line.strip())
            if m:
                affected += int(m.group(1))
        _audit(rp, {"op": f"pg-{kind}-preview", "sql": sql, "rolled_back": rolled_back})
        return {"ok": True, "verified": rolled_back,
                "evidence": {"preview": True, "kind": kind, "backend": "postgres",
                             "affected_rows": affected, "rolled_back": rolled_back},
                "error": None, "undo": None}
    if not confirm:
        return {"ok": False, "verified": False, "evidence": {"needs_confirm": True, "kind": kind},
                "error": "confirm required", "undo": None}
    snap = None
    if kind == "ddl":
        try:
            dumped = _docker("exec", container, "pg_dump", "-U", PG_USER, "-d", PG_DB,
                             "--no-owner", "--no-privileges", timeout=180)
            if dumped.returncode == 0:
                rp.mkdir(parents=True, exist_ok=True)
                snap = str(rp / "ddl_snapshot_pg.sql")
                Path(snap).write_text(dumped.stdout)
        except Exception:
            snap = None
    try:
        proc = _pg_run(container, sql)
    except Exception as e:
        return _deny(str(e))
    if proc.returncode != 0:
        return {"ok": False, "verified": False, "evidence": {"rolled_back": True},
                "error": (proc.stderr or "").strip()[:500] or "commit failed",
                "undo": None}
    affected = 0
    for line in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines():
        m = re.match(r"^(?:INSERT \d+ |UPDATE |DELETE )(\d+)\s*$", line.strip())
        if m:
            affected += int(m.group(1))
    _audit(rp, {"op": f"pg-{kind}", "sql": sql, "confirmed": True,
                "affected_rows": affected})
    undo = ({"snapshot": snap,
             "restore_cmd": f"docker exec -i {container} psql -U {PG_USER} "
                            f"-d {PG_DB} < {shlex.quote(snap)}"}
            if kind == "ddl" and snap
            else ({"rolled_back_possible": False} if kind == "dml" else None))
    return {"ok": True, "verified": True,
            "evidence": {"kind": kind, "backend": "postgres", "affected_rows": affected},
            "error": None, "undo": undo}
