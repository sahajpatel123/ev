"""Adapter: eval gates (app.scripts.eval_gates), parsed from last-run.json."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from ..keys import suite_env
from ..result import finish, new_result, stride, wilson

BACKEND = Path(__file__).resolve().parents[3]
REPRO = "cd backend && python -m app.scripts.eval_gates --report eval/last-run.json"


def parse_gates(gates: list[dict], sample: float) -> dict:
    """Pure parse of the gates list (unit-testable)."""
    cats = []
    failures = []
    cases_run = []
    skip_reasons = []
    n = passed = failed = skipped = 0
    for g in gates:
        name = g.get("name", "?")
        checks = g.get("checks", [])
        if g.get("skipped"):
            skipped += len(checks)
            skip_reasons.append(f"{name}: {g.get('skip_reason', '')}")
            cats.append({"name": name, "n": 0, "passed": 0, "rate": 0.0,
                         "wilson95": wilson(0, 0)})
            continue
        chosen = set(stride([c.get("name", "?") for c in checks], sample))
        gp = 0
        for c in checks:
            if c.get("name") not in chosen:
                continue
            cid = f"{name}/{c.get('name')}"
            cases_run.append(cid)
            if c.get("passed"):
                gp += 1
            else:
                failures.append({"id": cid, "repro": REPRO,
                                 "detail": str(c.get("detail") or "")[:200]})
        n += len(chosen)
        passed += gp
        failed += len(chosen) - gp
        cats.append({"name": name, "n": len(chosen), "passed": gp,
                     "rate": round(gp / len(chosen), 4) if chosen else 0.0,
                     "wilson95": wilson(len(chosen), gp)})
    return {"n": n, "passed": passed, "failed": failed, "skipped": skipped,
            "categories": cats, "failures": failures, "cases_run": cases_run,
            "skip_reasons": skip_reasons}


def run(*, budget: float | None, sample: float, out: str) -> dict:
    res = new_result("gates", sample=sample, budget=budget)
    report = BACKEND / "eval" / "last-run.json"
    before = report.read_bytes() if report.is_file() else None
    started = time.time()
    p = subprocess.run([sys.executable, "-m", "app.scripts.eval_gates",
                        "--report", str(report)],
                       cwd=str(BACKEND), capture_output=True, text=True, env=suite_env(), timeout=3600)
    try:
        fresh = report.is_file() and report.stat().st_mtime >= started - 1
        try:
            data = json.loads(report.read_text()) if fresh else None
        except ValueError:
            data = None
    finally:
        if before is None:
            report.unlink(missing_ok=True)
        else:
            report.write_bytes(before)
    if not fresh or data is None:
        res["status"] = "blocked"
        res["coverage_note"] = (f"no fresh report (rc={p.returncode}); "
                                "refusing to read old results")
        return finish(res, counts=(0, 0, 0, 0, 1))
    parsed = parse_gates(data.get("gates", []), sample)
    res["failures"] = parsed["failures"]
    res["cases_run"] = parsed["cases_run"]
    note = f"S={sample} over checks; gate runner rc={p.returncode}; safety counters unobservable by gates"
    if parsed["skip_reasons"]:
        note += "; skipped gates: " + "; ".join(parsed["skip_reasons"])
    res["coverage_note"] = note
    if sample < 1.0:
        res["status"] = "partial"
    return finish(res, counts=(parsed["n"], parsed["passed"], parsed["failed"],
                               parsed["skipped"], 0), categories=parsed["categories"])
