"""Adapter: gauntlet (scripted plumbing by default; real-brain when budget > 0)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ..keys import suite_env
from ..result import finish, new_result, wilson

BACKEND = Path(__file__).resolve().parents[3]


def parse_scripted(summary: dict, sample: float) -> dict:
    """Pure parse of the scripted --json summary (unit-testable)."""
    n = int(summary.get("n") or 0)
    held_out = int(summary.get("real_brain_only") or 0)
    failed_rows = [f for f in (summary.get("failed") or []) if isinstance(f, dict)]
    failed = len(failed_rows)
    passed = max(0, n - held_out - failed)
    cats = []
    for name, grp in (summary.get("by_difficulty") or {}).items():
        if isinstance(grp, dict):
            gn = int(grp.get("n", 0))
            gp = round(gn * float(grp.get("plumbing_pass_at_1", 0.0)) / 100.0)
            cats.append({"name": f"difficulty/{name}", "n": gn, "passed": gp,
                         "rate": round(gp / gn, 4) if gn else 0.0, "wilson95": wilson(gn, gp)})
    failures = [{"id": str(f.get("id", "?")),
                 "repro": f"python -m app.scripts.gauntlet --split dev --json  # case {f.get('id', '?')}",
                 "detail": str(f.get("problems", ""))[:200]} for f in failed_rows]
    return {"n": n, "passed": passed, "failed": failed, "skipped": held_out,
            "categories": cats, "failures": failures,
            "cases_run": [str(i) for i in (summary.get("ran_ids") or [])],
            "unauthorized": int(summary.get("unauthorized") or 0),
            "false_success": int(summary.get("false_success") or 0),
            "pass_at_1": summary.get("pass_at_1")}


def parse_real(slim: dict) -> dict:
    """Pure parse of the real-brain slim --json (unit-testable).

    Returns {"ready": False, "detail": ...} when the run never started.
    """
    if not slim.get("ready"):
        return {"ready": False,
                "detail": str(slim.get("detail") or "real-brain run not ready (no key? provider unset?)")}
    ran = int(slim.get("stage1_ran") or 0)
    passed = int(slim.get("stage1_passed") or 0)
    eligible = int(slim.get("stage1_eligible") or 0)
    cats = []
    for name, grp in (slim.get("stage1_by_difficulty") or {}).items():
        if isinstance(grp, dict):
            gn = int(grp.get("n", 0))
            gp = int(grp.get("passed", 0))
            cats.append({"name": f"difficulty/{name}", "n": gn, "passed": gp,
                         "rate": round(gp / gn, 4) if gn else 0.0, "wilson95": wilson(gn, gp)})
    failures = []
    for f in (slim.get("stage1_failures") or []):
        if isinstance(f, dict):
            failures.append({"id": str(f.get("id", "?")),
                             "repro": "python -m app.scripts.gauntlet --real-brain --json  # full re-run",
                             "detail": str(f.get("problems", ""))[:200]})
    return {"ready": True, "n": ran, "passed": passed, "failed": max(0, ran - passed),
            "skipped": max(0, eligible - ran), "categories": cats, "failures": failures,
            "cases_run": [str(i) for i in (slim.get("stage1_ran_ids") or [])],
            "unauthorized": int(slim.get("stage1_unauthorized") or 0),
            "false_success": int(slim.get("stage1_false_success") or 0),
            "cost_usd": float(slim.get("spent_usd") or 0.0),
            "cost_source": str(slim.get("cost_source") or "none"),
            "pass_rate": slim.get("stage1_pass_rate")}


def run(*, budget: float | None, sample: float, out: str) -> dict:
    res = new_result("gauntlet", sample=sample, budget=budget)
    real = (budget or 0) > 0
    if real and sample < 1.0:
        res["status"] = "blocked"
        res["coverage_note"] = ("real-brain gauntlet has no subset mode; "
                                "run sample=1.0 or use scripted (budget=0)")
        return finish(res, counts=(0, 0, 0, 0, 1))
    cmd = [sys.executable, "-m", "app.scripts.gauntlet", "--split", "dev", "--json"]
    if not real and sample < 1.0:
        stride = max(2, round(1.0 / sample))
        cmd += ["--stride", str(stride), "--stride-index", "0"]
    if real:
        cmd += ["--real-brain", "--budget", str(budget)]
    p = subprocess.run(cmd, cwd=str(BACKEND), capture_output=True, text=True,
                       env=suite_env(), timeout=7200)
    try:
        summary = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        res["status"] = "blocked"
        res["coverage_note"] = f"gauntlet printed no JSON (rc={p.returncode}): {(p.stderr or '')[:200]}"
        return finish(res, counts=(0, 0, 0, 0, 1))
    if real:
        parsed = parse_real(summary)
        if not parsed["ready"]:
            res["status"] = "blocked"
            res["coverage_note"] = f"real-brain not ready: {parsed['detail']}"
            return finish(res, counts=(0, 0, 0, 0, 1))
        res["cost_usd"] = parsed["cost_usd"]
        res["cost_source"] = parsed["cost_source"]
        safety_note = "safety counts are model evidence"
    else:
        parsed = parse_scripted(summary, sample)
        res["cost_usd"] = 0.0
        res["cost_source"] = "none"
        safety_note = ("safety zeros are plumbing checks, not model evidence; "
                       f"{parsed['skipped']} real-brain-only cases held out as skipped")
    res["safety"] = {
        "unauthorized_effects": parsed["unauthorized"],
        "false_success": parsed["false_success"],
        "silent_turns": None,
        "fanout_violations": None,
    }
    res["failures"] = parsed["failures"]
    res["cases_run"] = parsed["cases_run"]
    res["coverage_note"] = (
        f"{'real-brain DEV' if real else 'scripted plumbing only (budget=0)'}; {safety_note}")
    if sample < 1.0:
        res["status"] = "partial"
    return finish(res, counts=(parsed["n"], parsed["passed"], parsed["failed"],
                               parsed["skipped"], 0), categories=parsed["categories"])
