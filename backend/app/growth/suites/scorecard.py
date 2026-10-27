"""Adapter: scorecard (Mind 1.1 brain scorecard over the v2 corpus).

Runs ``gauntlet.v2.score`` (EVIE + RAW-HIGH) in a subprocess under the
growth suite env, then maps the scores record onto the standard result.
Headline n/passed/safety describe the EVIE config (the system under test);
RAW-HIGH rides as comparison categories plus harness-penalty entries.
Oracles were written before the first run (R9); judge-graded portions are
noted per case in ``gauntlet/v2/cases/*.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from ..keys import suite_env
from ..result import finish, new_result, percentile, stride, wilson

BACKEND = Path(__file__).resolve().parents[3]
SCORES = BACKEND.parent / "docs" / "brain-probe" / "scores"


def case_ids() -> list[str]:
    from gauntlet.v2.corpus import load_corpus

    return sorted(c["id"] for c in load_corpus())


def to_standard_result(record: dict, *, sample: float, budget: float | None) -> dict:
    from gauntlet.v2 import stats as v2stats

    res = new_result("scorecard", sample=sample, budget=budget)
    rows = record.get("rows") or []
    evie = [r for r in rows if r.get("config") == "evie"]
    raw = [r for r in rows if r.get("config") != "evie"]
    n = len(evie)
    passed = sum(1 for r in evie if r.get("passed"))
    errors = sum(1 for r in evie
                 for p in (r.get("problems") or []) if v2stats._bucket(str(p)) == "harness_error")
    errors = min(errors, n)
    failed = n - passed
    categories: list[dict] = []
    for domain in sorted({r.get("domain", "?") for r in rows}):
        for name, subset in (("evie", evie), ("raw-high", raw)):
            part = [r for r in subset if r.get("domain") == domain]
            if not part:
                continue
            k = sum(1 for r in part if r.get("passed"))
            categories.append({"name": f"{name}/{domain}", "n": len(part), "passed": k,
                               "rate": round(k / len(part), 4),
                               "wilson95": wilson(len(part), k)})
    if raw:
        k = sum(1 for r in raw if r.get("passed"))
        categories.append({"name": "raw-high", "n": len(raw), "passed": k,
                           "rate": round(k / len(raw), 4), "wilson95": wilson(len(raw), k)})
    res = finish(res, counts=(n, passed, failed, 0, errors), categories=categories)
    res["cost_usd"] = float(record.get("spent_usd") or 0.0)
    res["cost_source"] = "provider_reported" if res["cost_usd"] > 0 else "none"
    latencies = [float(r.get("latency_ms") or 0) for r in evie]
    res["latency_ms"] = {"n": len(latencies), "p50": percentile(latencies, 0.5),
                         "p95": percentile(latencies, 0.95)}
    res["safety"] = {
        "unauthorized_effects": sum(1 for r in evie if r.get("unauthorized")),
        "false_success": sum(1 for r in evie if r.get("false_success")),
        "silent_turns": sum(1 for r in evie if r.get("silent")),
        "fanout_violations": None,
    }
    for row in [r for r in evie if not r.get("passed")][:50]:
        res["failures"].append({
            "id": str(row.get("id")),
            "repro": f"ev brain score --ids {row.get('id')} --config evie",
            "detail": "; ".join(row.get("problems") or [])[:200]})
    for row in [r for r in raw if not r.get("passed")][:20]:
        res["failures"].append({
            "id": f"raw-high:{row.get('id')}",
            "repro": f"ev brain score --ids {row.get('id')} --config raw-high",
            "detail": "; ".join(row.get("problems") or [])[:200]})
    res["cases_run"] = [f"{r.get('config')}:{r.get('id')}" for r in rows]
    coverage = record.get("coverage") or {}
    stopped = bool(record.get("stopped_by_budget"))
    res["status"] = "partial" if (sample < 1.0 or stopped) else "ok"
    res["coverage_note"] = (
        f"S={sample} stride over the v2 corpus; ran {coverage.get('ran', len(rows))} "
        f"of {coverage.get('planned', len(rows))} planned rows"
        + ("; stopped by budget" if stopped else "")
        + "; fanout per-case export lands with M2 turn-guard accounting; "
          "headlines describe EVIE, RAW-HIGH rides as comparison categories"
    )
    res["penalties"] = record.get("penalties") or {}
    res["detail_scores"] = f"docs/brain-probe/scores/{record.get('run', '?')}.json"
    return res


def run(*, budget: float | None, sample: float, out: str) -> dict:
    ids = stride(case_ids(), sample)
    if not ids:
        res = new_result("scorecard", status="blocked", sample=sample, budget=budget)
        res["coverage_note"] = "v2 corpus is empty"
        return finish(res, counts=(0, 0, 0, 0, 0))
    run_name = f"scorecard-{time.strftime('%Y%m%d-%H%M%S')}"
    cmd = [sys.executable, "-m", "gauntlet.v2.score",
           "--config", "evie", "--config", "raw-high",
           "--runs", "1", "--jobs", "4",
           "--budget", str(budget if budget is not None else 15),
           "--ids", *ids, "--run", run_name]
    proc = subprocess.run(cmd, cwd=str(BACKEND), capture_output=True, text=True,
                          env=suite_env(), timeout=14400)
    scores_file = SCORES / f"{run_name}.json"
    if not scores_file.is_file():
        res = new_result("scorecard", status="blocked", sample=sample, budget=budget)
        tail = (proc.stderr or proc.stdout or "")[-400:]
        res["coverage_note"] = f"score engine wrote no record (rc={proc.returncode}): {tail}"
        return finish(res, counts=(0, 0, 0, 0, 1))
    record = json.loads(scores_file.read_text(encoding="utf-8"))
    return to_standard_result(record, sample=sample, budget=budget)
