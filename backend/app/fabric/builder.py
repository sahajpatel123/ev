"""Builder: plan -> implement -> test -> fix loop (<=6), sqlite job state, resume.

apply_plan executes the plan's steps for real through the shell lane and
records per-step state in the jobs DB, so a killed job resumes honestly
instead of claiming phantom success. Steps may delegate to an installed
coding-agent CLI ({agent: goal}) or run shell commands ({cmd}).
"""

from __future__ import annotations

import contextlib
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

MAX_FIX_ITERS = 6

# Installed coding-agent CLIs (non-interactive entry points, verified flags).
# First present binary wins; steps fall back to direct shell when none exist.
AGENT_CLIS: tuple[tuple[str, list[str]], ...] = (
    ("opencode", ["opencode", "run", "{goal}"]),
    ("claude", ["claude", "-p", "{goal}"]),
    ("codex", ["codex", "exec", "{goal}"]),
    ("gemini", ["gemini", "-p", "{goal}"]),
)

# Fix strategies: distinct, safe step mutations, tried in order.
STRATEGIES = ("retry", "verbose", "timeout_x3", "verbose_timeout")


def detect_agent_cli() -> tuple[str, list[str]] | None:
    for name, argv in AGENT_CLIS:
        if shutil.which(argv[0]):
            return name, argv
    return None


def _strategize(cmd: str, strategy: str, timeout: float) -> tuple[str, float]:
    if strategy == "retry":
        return cmd, timeout
    if strategy == "verbose":
        if "pytest" in cmd and "-v" not in cmd:
            return cmd + " -v --tb=short", timeout
        if cmd.strip().startswith("npm ") and "--verbose" not in cmd:
            return cmd + " --verbose", timeout
        if cmd.strip().startswith("make") and "V=1" not in cmd:
            return cmd + " V=1", timeout
        if cmd.strip().startswith("go ") and " -v" not in cmd:
            return cmd + " -v", timeout
        if cmd.strip().startswith("cargo ") and "--verbose" not in cmd:
            return cmd + " --verbose", timeout
        # Unknown tool: trace execution. bash -x propagates the inner exit
        # code (unlike piping through tail, which would mask failures).
        import shlex as _shlex

        return f"bash -x -c {_shlex.quote(cmd)}", timeout
    if strategy == "timeout_x3":
        return cmd, timeout * 3
    if strategy == "verbose_timeout":
        verbose, _ = _strategize(cmd, "verbose", timeout)
        return verbose, timeout * 3
    return cmd, timeout


_TEST_SUMMARY_RES = (
    re.compile(r"(\d+) passed(?:, (\d+) failed)?(?:, (\d+) (?:error|skipped))?"),
    re.compile(r"FAILED|failed", re.IGNORECASE),
)


def summarize_tests(output: str) -> dict:
    """Best-effort pytest/unittest summary (None fields when unparseable)."""
    text = output or ""
    m = _TEST_SUMMARY_RES[0].search(text)
    if m:
        return {"passed": int(m.group(1)),
                "failed": int(m.group(2) or 0),
                "parsed": True}
    if _TEST_SUMMARY_RES[1].search(text):
        return {"passed": None, "failed": None, "parsed": False,
                "note": "failure markers present, counts unparseable"}
    return {"passed": None, "failed": None, "parsed": False}


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
            with contextlib.suppress(Exception):
                conn.execute(ddl)
    return conn


def build_plan(goal: str, root=None, project_dir: str = "", steps: list | None = None,
               provenance: str = "owner_turn") -> dict:
    from .policy import UNTRUSTED_PROVENANCE

    root = Path(str(root)) if root is not None else Path(".")
    if str(provenance or "").lower() in UNTRUSTED_PROVENANCE:
        return {"ok": False, "verified": True, "evidence": {"needs_confirm": True},
                "error": "untrusted provenance needs confirm", "undo": None,
                "confirm": True}
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
    cmd = str(step.get("cmd") or "").strip()
    cwd = step.get("cwd") or str(root)
    if not cmd:
        return {"ok": False, "verified": False,
                "evidence": {"name": step.get("name", "?")},
                "error": "step has no command", "undo": None}
    # Builder steps run with explicit confirm (the plan itself was confirmed).
    return s.run(cmd, confirm=True, cwd=cwd)


def _run_agent_step(step: dict, root: Path, timeout: float = 600.0) -> dict:
    """Delegate one step to an installed coding-agent CLI (tmux session).

    Falls back to direct shell when no agent CLI exists. The agent goal runs
    in a tmux session so long generations survive and stream honestly.
    """
    from .shell_sessions import ShellSessions

    goal = str(step.get("agent") or step.get("goal") or "")
    if not goal:
        return {"ok": False, "verified": True, "evidence": {},
                "error": "agent step needs a goal", "undo": None}
    found = detect_agent_cli()
    if found is None:
        return {"ok": False, "verified": True,
                "evidence": {"fallback": "internal"},
                "error": "no coding-agent CLI installed", "undo": None}
    name, argv = found
    cmd = " ".join(f'"{part}"' if " " in part and "{goal}" not in part else part
                   for part in argv).replace("{goal}", goal)
    s = ShellSessions(root=root)
    started = s.start(cmd, cwd=str(step.get("cwd") or root), confirm=True)
    if not started.get("ok"):
        return started
    sid = started["session_id"]
    deadline = time.time() + timeout
    tail = ""
    while time.time() < deadline:
        code = s.exit_code(sid)
        tail = s.tail(sid, 60).get("output", "")
        if code.get("exit_code") is not None:
            break
        time.sleep(5.0)
    else:
        s.cancel(sid)
        return {"ok": False, "verified": True, "evidence": {"agent": name},
                "error": "agent step timed out", "undo": None}
    final = s.exit_code(sid).get("exit_code")
    s.cancel(sid)
    ok = final == 0
    return {"ok": ok, "verified": True,
            "evidence": {"agent": name, "exit_code": final, "out": tail[-4000:]},
            "error": None if ok else f"agent exited {final}", "undo": None}


def _run_step_with_fixes(step: dict, root: Path,
                         max_iters: int = MAX_FIX_ITERS) -> dict:
    """Run a step, rotating distinct fix strategies on failure (<=6 rounds)."""
    if not str(step.get("cmd") or "").strip() and not step.get("agent"):
        return {"ok": False, "verified": False,
                "evidence": {"name": step.get("name", "?"), "attempts": []},
                "error": "step has no command", "undo": None}
    strategies = list(STRATEGIES)[: max(1, min(int(max_iters), MAX_FIX_ITERS))]
    attempts: list[dict] = []
    base_timeout = float(step.get("timeout", 120.0))
    for strategy in strategies:
        if step.get("agent"):
            res = _run_agent_step(step, root)
        else:
            cmd, timeout = _strategize(str(step.get("cmd", "")), strategy,
                                       base_timeout)
            attempt_step = dict(step)
            attempt_step["cmd"] = cmd
            attempt_step["timeout"] = timeout
            from .shell_sessions import ShellSessions
            s = ShellSessions(root=root)
            res = s.run(cmd, timeout=min(timeout, 300),
                        confirm=True, cwd=str(step.get("cwd") or root))
        attempts.append({"strategy": strategy, "ok": bool(res.get("ok")),
                         "error": res.get("error")})
        if res.get("ok"):
            res["evidence"] = {**res.get("evidence", {}), "attempts": attempts,
                               "strategy": strategy}
            return res
    last = attempts[-1] if attempts else {}
    return {"ok": False, "verified": True,
            "evidence": {"attempts": attempts, "name": step.get("name", "?")},
            "error": f"step failed after {len(attempts)} strategies: "
                     f"{last.get('error') or 'unknown'}", "undo": None}


def _job_state(root: Path, jid: str | None) -> dict | None:
    if not jid:
        return None
    conn = _ensure(root)
    try:
        row = conn.execute("SELECT id, goal, state, iters, steps_done, summary, updated "
                           "FROM jobs WHERE id=?", (jid,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"id": row[0], "goal": row[1], "state": row[2], "iters": row[3],
            "steps_done": row[4], "summary": row[5], "updated": row[6]}


STALE_RUNNING_S = 900


def apply_plan(plan_or_receipt: dict, root=None, stop_after: int | None = None,
               fix_rounds: int = MAX_FIX_ITERS) -> dict:
    """Execute plan steps with the fix loop. stop_after=N simulates a kill
    after N steps (for resume tests)."""
    root = Path(str(root)) if root is not None else Path(".")
    plan = plan_or_receipt.get("plan", plan_or_receipt)
    jid = plan_or_receipt.get("job_id")
    if jid:
        existing = _job_state(root, jid)
        if existing is not None and existing["state"] == "running":
            age = time.time() - float(existing.get("updated") or 0)
            if age < STALE_RUNNING_S:
                return {"ok": False, "verified": True,
                        "evidence": {"job_id": jid},
                        "error": "job already running", "undo": None}
            repair_running(root, jid)
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
        res = _run_step_with_fixes(step, root, max_iters=fix_rounds)
        outs.append({"name": step.get("name", f"step-{idx}"), "receipt": res})
        if not res.get("ok"):
            ok = False
            break
    summary = "; ".join(f"{o['name']}: {'ok' if o['receipt'].get('ok') else 'FAIL'}"
                        for o in outs) or "no steps"
    # attach output tails so the receipt carries a test summary aggregated
    # over every step (a trailing silent lint must not hide test counts).
    tails = []
    for o in outs:
        last = o["receipt"].get("evidence", {})
        text = str(last.get("out", ""))
        if text.strip():
            tails.append(f"[{o['name']}]\n{text[-1500:]}")
    tail = "\n".join(tails)[-3000:]
    test_summary = summarize_tests(tail)
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
    step_detail = [{"name": o["name"], "ok": bool(o["receipt"].get("ok")),
                    "attempts": (o["receipt"].get("evidence") or {}).get("attempts", []),
                    "error": o["receipt"].get("error")} for o in outs]
    return {"ok": ok, "verified": verified, "iters": iters,
            "evidence": {"plan": plan, "summary": summary, "tail": tail, "state": state,
                         "test_summary": test_summary, "steps": step_detail},
            "error": None if ok else f"job {state}", "undo": None}


def repair_running(root, job_id: str) -> dict:
    """Park a stale-running job as interrupted (it can honestly resume)."""
    root = Path(str(root))
    conn = _ensure(root)
    try:
        row = conn.execute("SELECT state, updated FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        if row is None:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": "unknown job", "undo": None}
        state, updated = row[0], float(row[1] or 0)
        if state != "running" or time.time() - updated < STALE_RUNNING_S:
            return {"ok": True, "verified": True,
                    "evidence": {"state": state, "repaired": False},
                    "error": None, "undo": None}
        conn.execute("UPDATE jobs SET state=?, updated=? WHERE id=?",
                     ("interrupted", time.time(), job_id))
        conn.commit()
        back = conn.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        repaired = bool(back and back[0] == "interrupted")
        return {"ok": repaired, "verified": repaired,
                "evidence": {"state": back[0] if back else None, "repaired": repaired},
                "error": None if repaired else "repair did not read back",
                "undo": None}
    finally:
        conn.close()


def resume_job(job_id: str, root=None) -> dict:
    root = Path(str(root)) if root is not None else Path(".")
    repair_running(root, job_id)
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
    honest = state in ("planned", "running", "killed", "failed", "done", "interrupted")
    return {"ok": honest, "verified": True, "job_id": row[0],
            "evidence": {"goal": row[1], "state": state, "iters": row[3],
                         "steps_done": row[4], "summary": row[5]},
            "error": None if state == "done" else f"job not done: {state}", "undo": None}
