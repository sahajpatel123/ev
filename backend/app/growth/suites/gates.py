"""Adapter: eval gates (app.scripts.eval_gates), parsed from last-run.json."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ..keys import suite_env
from ..result import finish, new_result, stride, wilson

BACKEND = Path(__file__).resolve().parents[3]


def run(*, budget: float | None, sample: float, out: str) -> dict:
    res = new_result("gates", sample=sample, budget=budget)
    report = BACKEND / "eval" / "last-run.json"
    p = subprocess.run([sys.executable, "-m", "app.scripts.eval_gates",
                        "--report", str(report)],
                       cwd=str(BACKEND), capture_output=True, text=True, env=suite_env(), timeout=3600)
    if not report.is_file():
        res["status"] = "blocked"
        res["coverage_note"] = f"eval gates produced no report (rc={p.returncode})"
        return finish(res, counts=(0, 0, 0, 0, 1))
    data = json.loads(report.read_text())
    gates = data.get("gates", [])
    cats = []
    failures = []
    cases_run = []
    n = passed = skipped = 0
    for g in gates:
        checks = g.get("checks", [])
        chosen = stride([c.get("name", "?") for c in checks], sample)
        gp = sum(1 for c in checks if c.get("name") in chosen and c.get("passed"))
        gs = sum(1 for c in checks if c.get("name") in chosen and c.get("skipped"))
        n += len(chosen)
        passed += gp
        skipped += gs
        cats.append({"name": g.get("name", "?"), "n": len(chosen), "passed": gp,
                     "rate": round(gp / len(chosen), 4) if chosen else 0.0,
                     "wilson95": wilson(len(chosen), gp)})
        for c in checks:
            if c.get("name") in chosen:
                cases_run.append(f"{g.get('name')}/{c.get('name')}")
                if not c.get("passed") and not c.get("skipped"):
                    failures.append({"id": cases_run[-1],
                                     "repro": "cd backend && python -m app.scripts.eval_gates --report eval/last-run.json",
                                     "detail": str(c.get("detail") or "")[:200]})
    res["failures"] = failures
    res["cases_run"] = cases_run
    res["coverage_note"] = (f"S={sample} over checks; gate runner rc={p.returncode}; "
                            "safety counters unobservable by gates")
    if sample < 1.0:
        res["status"] = "partial"
    return finish(res, counts=(n, passed, n - passed - skipped, skipped, 0),
                  categories=cats)
