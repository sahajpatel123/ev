"""Adapter: gauntlet (scripted by default; real-brain only when budget > 0)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ..keys import suite_env
from ..result import finish, new_result, wilson

BACKEND = Path(__file__).resolve().parents[3]


def run(*, budget: float | None, sample: float, out: str) -> dict:
    res = new_result("gauntlet", sample=sample, budget=budget)
    real = (budget or 0) > 0
    limit = 0
    if sample < 1.0:
        # DEV total is measured after the run; bound optimistically and report n.
        limit = max(1, round(535 * sample))
    cmd = [sys.executable, "-m", "app.scripts.gauntlet", "--split", "dev", "--json"]
    if limit:
        cmd += ["--limit", str(limit)]
    if real:
        cmd += ["--real-brain", "--budget", str(budget)]
    p = subprocess.run(cmd, cwd=str(BACKEND), capture_output=True, text=True, env=suite_env(), timeout=7200)
    try:
        summary = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        res["status"] = "blocked"
        res["coverage_note"] = f"gauntlet printed no JSON (rc={p.returncode}): {(p.stderr or '')[:200]}"
        return finish(res, counts=(0, 0, 0, 0, 1))
    n = int(summary.get("n") or 0)
    held_out = int(summary.get("real_brain_only") or 0)
    failed_rows = summary.get("failed") or []
    failed = len(failed_rows)
    passed = max(0, n - held_out - failed)
    cats = []
    for name, grp in (summary.get("by_difficulty") or {}).items():
        if isinstance(grp, dict):
            gn = int(grp.get("n", 0))
            gp = round(gn * float(grp.get("plumbing_pass_at_1", 0.0)) / 100.0)
            cats.append({"name": f"difficulty/{name}", "n": gn, "passed": gp,
                         "rate": round(gp / gn, 4) if gn else 0.0, "wilson95": wilson(gn, gp)})
    res["cost_usd"] = float(summary.get("cost_usd") or summary.get("spent_usd") or 0.0)
    res["cost_source"] = "provider_reported" if real and res["cost_usd"] else "none"
    res["safety"] = {
        "unauthorized_effects": int(summary.get("unauthorized") or 0),
        "false_success": int(summary.get("false_success") or 0),
        "silent_turns": None,
        "fanout_violations": None,
    }
    for f in failed_rows:
        if isinstance(f, dict):
            res["failures"].append({"id": str(f.get("id", "?")),
                                    "repro": f"ev gauntlet --split dev --domain {f.get('domain', '')}".strip(),
                                    "detail": str(f.get("problems", ""))[:200]})
    res["cases_run"] = []  # gauntlet --json emits failures only, not the run list
    res["coverage_note"] = (
        "real-brain DEV, budget cap applied; safety counts are model evidence" if real
        else f"scripted plumbing only (budget=0): pass_at_1={summary.get('pass_at_1')}%; "
             "safety zeros are plumbing checks, not model evidence; "
             f"{held_out} real-brain-only cases held out as skipped")
    if sample < 1.0:
        res["status"] = "partial"
    return finish(res, counts=(n, passed, failed, held_out, 0), categories=cats)
