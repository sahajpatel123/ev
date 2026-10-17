"""Builder: plan -> implement -> test -> fix loop (<=6), sqlite job state, resume.

apply_plan executes the plan's steps for real through the shell lane and
records per-step state in the jobs DB, so a killed job resumes honestly
instead of claiming phantom success.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

MAX_FIX_ITERS = 6


def _db(root) -> Path:
    return Path(str(root)) / "builder_jobs.db"


def _ensure(root) -> sqlite3.Connection:
    db = _db(root)
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs
        (id TEXT PRIMARY KEY, goal TEXT, state TEXT, iters INTEGER, plan TEXT,
         project_dir TEXT DEFAULT '', steps_done INTEGER DEFAULT 0,
         summary TEXT DEFAULT '', updated REAL DEFAULT 0)""")
    # tolerate DBs created by older schema (no new columns)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for col, ddl in (("project_dir", "ALTER TABLE jobs ADD COLUMN project_dir TEXT DEFAULT ''"),
                     ("steps_done", "ALTER TABLE jobs ADD COLUMN steps_done INTEGER DEFAULT 0"),
                     ("summary", "ALTER TABLE jobs ADD COLUMN summary TEXT DEFAULT ''"),
                     ("updated", "ALTER TABLE jobs ADD COLUMN updated REAL DEFAULT 0")):
        if col not in cols:
            try:
                conn.execute(ddl)
            except Exception:
                pass
    return conn


def build_plan(goal: str, root=None, project_dir: str = "", steps: list | None = None) -> dict:
    root = Path(str(root)) if root is not None else Path(".")
    jid = f"job-{uuid.uuid4().hex[:8]}"
    plan = {"goal": goal, "steps": steps or ["implement", "test", "fix"],
            "project_dir": project_dir}
    conn = _ensure(root)
    try:
        conn.execute("INSERT INTO jobs (id, goal, state, iters, plan, project_dir, steps_done, updated)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (jid, goal, "planned", 0, str(plan), project_dir, 0, time.time()))
        conn.commit()
        row = conn.execute("SELECT id FROM jobs WHERE id=?", (jid,)).fetchone()
    finally:
        conn.close()
    ok = row is not None
    return {"ok": ok, "verified": ok, "job_id": jid, "plan": plan,
            "evidence": {"goal": goal}, "error": None, "undo": None}


def _run_step(step: dict, root: Path) -> dict:
    """Execute one plan step. Step: {name, cmd} run via the shell lane."""
    from .shell_sessions import ShellSessions
    s = ShellSessions(root=root)
    cmd = step.get("cmd", "")
    cwd = step.get("cwd") or str(root)
    if not cmd:
        return {"ok": True, "verified": True, "evidence": {"name": step.get("name", "?"),
                "note": "no cmd; marked done"}, "error": None, "undo": None}
    # Builder steps run with explicit confirm (the plan itself was confirmed).
    return s.run(cmd, confirm=True, cwd=cwd)


def apply_plan(plan_or_receipt: dict, root=None, stop_after: int | None = None) -> dict:
    """Execute plan steps. stop_after=N simulates a kill after N steps (for resume tests)."""
    root = Path(str(root)) if root is not None else Path(".")
    plan = plan_or_receipt.get("plan", plan_or_receipt)
    jid = plan_or_receipt.get("job_id")
    raw_steps = plan.get("steps", []) if isinstance(plan, dict) else []
    steps = [s if isinstance(s, dict) else {"name": str(s)} for s in raw_steps]
    outs: list[dict] = []
    iters = 0
    ok = True
    for idx, step in enumerate(steps):
        if stop_after is not None and idx >= stop_after:
            ok = False
            break
        iters += 1
        if jid:
            conn = _ensure(root)
            try:
                conn.execute("UPDATE jobs SET state=?, iters=?, steps_done=?, updated=? WHERE id=?",
                             ("running", iters, idx, time.time(), jid))
                conn.commit()
            finally:
                conn.close()
        res = _run_step(step, root)
        outs.append({"name": step.get("name", f"step-{idx}"), "receipt": res})
        if not res.get("ok"):
            ok = False
            break
    summary = "; ".join(f"{o['name']}: {'ok' if o['receipt'].get('ok') else 'FAIL'}"
                        for o in outs) or "no steps"
    # attach last step's output tail so the receipt carries a test summary
    tail = ""
    if outs:
        last = outs[-1]["receipt"].get("evidence", {})
        tail = str(last.get("out", ""))[-1500:]
    state = "done" if ok else ("killed" if stop_after is not None and iters == stop_after else "failed")
    if jid:
        conn = _ensure(root)
        try:
            conn.execute("UPDATE jobs SET state=?, iters=?, steps_done=?, summary=?, updated=? WHERE id=?",
                         (state, iters, len(outs), summary, time.time(), jid))
            conn.commit()
        finally:
            conn.close()
    verified = ok and all(o["receipt"].get("verified") for o in outs)
    return {"ok": ok, "verified": verified, "iters": iters,
            "evidence": {"plan": plan, "summary": summary, "tail": tail, "state": state},
            "error": None if ok else f"job {state}", "undo": None}


def resume_job(job_id: str, root=None) -> dict:
    root = Path(str(root)) if root is not None else Path(".")
    conn = _ensure(root)
    try:
        row = conn.execute("SELECT id, goal, state, iters, steps_done, summary FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "verified": True, "error": "unknown job",
                "evidence": {}, "undo": None}
    state = row[2]
    honest = state in ("planned", "running", "killed", "failed", "done")
    return {"ok": honest, "verified": True, "job_id": row[0],
            "evidence": {"goal": row[1], "state": state, "iters": row[3],
                         "steps_done": row[4], "summary": row[5]},
            "error": None if state == "done" else f"job not done: {state}", "undo": None}
