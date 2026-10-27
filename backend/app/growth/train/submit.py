"""Release train: SUBMIT registry (GE3 stage 1)."""

from __future__ import annotations

import subprocess

SUBMIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, branch TEXT NOT NULL,
  base TEXT NOT NULL, scope TEXT NOT NULL, evidence TEXT NOT NULL,
  status TEXT NOT NULL, report TEXT NOT NULL DEFAULT '');
"""


def _repo() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parents[4])


def _db():
    from ..store import _connect

    conn = _connect()
    conn.executescript(SUBMIT_SCHEMA)
    return conn


def branch_diff_files(branch: str, base: str = "main") -> list[str]:
    p = subprocess.run(["git", "diff", "--name-only", f"{base}...{branch}"],
                       cwd=_repo(), capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise ValueError(f"cannot diff {base}...{branch}: {p.stderr.strip()[:200]}")
    return [ln for ln in p.stdout.splitlines() if ln.strip()]


def submit(branch: str, scope: str, evidence: str, base: str = "main") -> dict:
    from ..result import utcnow

    subprocess.run(["git", "rev-parse", "--verify", branch], cwd=_repo(),
                   capture_output=True, timeout=30, check=True)
    files = branch_diff_files(branch, base)
    if not files:
        raise ValueError(f"{branch} has no diff vs {base}")
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO submissions(ts, branch, base, scope, evidence, status)"
            " VALUES(?,?,?,?,?,?)",
            (utcnow(), branch, base, scope, evidence, "submitted"))
        conn.commit()
        return {"id": int(cur.lastrowid or 0), "branch": branch,
                "files": len(files), "status": "submitted"}
    finally:
        conn.close()


def get(sub_id: int) -> dict | None:
    import sqlite3 as _s

    conn = _db()
    try:
        conn.row_factory = _s.Row
        row = conn.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_status(sub_id: int, status: str, report: str = "") -> None:
    conn = _db()
    try:
        conn.execute("UPDATE submissions SET status=?, report=? WHERE id=?",
                     (status, report, sub_id))
        conn.commit()
    finally:
        conn.close()


def scan_mailbox() -> list[dict]:
    """SUBMIT offers from peers in the captain's inbox (message protocol)."""
    from pathlib import Path

    out = []
    inbox = Path.home() / ".ev" / "mailbox" / "rocky-sculptor" / "inbox"
    if not inbox.is_dir():
        return out
    for f in sorted(inbox.iterdir()):
        try:
            text = f.read_text()
        except OSError:
            continue
        head = text[:600]
        if '"SUBMIT"' in head or "type: SUBMIT" in head or "SUBMIT:" in head:
            out.append({"file": f.name, "head": head})
    return out
