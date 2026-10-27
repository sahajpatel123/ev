"""Adapter: gauntlet (scripted by default; real-brain only when budget > 0)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
    p = subprocess.run(cmd, cwd=str(BACKEND), capture_output=True, text=True, timeout=7200)
    try:
        summary = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        res["status"] = "blocked"
        res["coverage_note"] = f"gauntlet printed no JSON (rc={p.returncode}): {(p.stderr or '')[:200]}"
        return finish(res, counts=(0, 0, 0, 0, 1))
    n = int(summary.get("n") or summary.get("total") or 0)
    passed = int(summary.get("passed") or 0)
    failed = int(summary.get("failed") or max(0, n - passed))
    cats = []
    for name, grp in (summary.get("by_domain") or summary.get("categories") or {}).items():
        if isinstance(grp, dict):
            gn, gp = int(grp.get("n", 0)), int(grp.get("passed", 0))
            cats.append({"name": str(name), "n": gn, "passed": gp,
                         "rate": round(gp / gn, 4) if gn else 0.0, "wilson95": wilson(gn, gp)})
    res["cost_usd"] = float(summary.get("cost_usd") or summary.get("spent_usd") or 0.0)
    res["cost_source"] = "provider_reported" if real and res["cost_usd"] else "none"
    saf = summary.get("safety") or {}
    res["safety"] = {
        "unauthorized_effects": saf.get("unauthorized_effects", 0 if real else None),
        "false_success": saf.get("false_success", summary.get("false_success", 0 if real else None)),
        "silent_turns": saf.get("silent_turns"),
        "fanout_violations": saf.get("fanout_violations"),
    }
    for f in summary.get("failures") or []:
        if isinstance(f, dict):
            res["failures"].append({"id": str(f.get("id", "?")),
                                    "repro": str(f.get("repro", "ev gauntlet --split dev")),
                                    "detail": str(f.get("detail", ""))[:200]})
    res["cases_run"] = [str(c) for c in (summary.get("cases_run") or [])]
    res["coverage_note"] = ("real-brain DEV, budget cap applied" if real
                            else "scripted brain only (budget=0); safety counters null")
    if sample < 1.0:
        res["status"] = "partial"
    return finish(res, counts=(n, passed, failed, 0, 0), categories=cats)
