"""Adapter: LIFE suite (approved real-failure regression cases, forever)."""

from __future__ import annotations

from ..learn import life_cases as L
from ..result import finish, new_result, wilson


def run(*, budget: float | None, sample: float, out: str) -> dict:
    res = new_result("life", sample=sample, budget=budget)
    rep = L.run_all()
    cases = rep["cases"]
    if sample < 1.0:
        from ..result import stride
        keep = set(stride([str(c["id"]) for c in cases], sample))
        cases = [c for c in cases if str(c["id"]) in keep]
        res["status"] = "partial"
    failures = [{"id": f"life-{c['id']}: {c['title']}"[:120],
                 "repro": "ev growth run life --out /tmp/life.json  # case " + str(c["id"]),
                 "detail": c["detail"][:200]} for c in cases if c["verdict"] == "fail"]
    skipped = [c for c in cases if c["verdict"] == "skip"]
    passed = [c for c in cases if c["verdict"] == "pass"]
    res["failures"] = failures
    res["cases_run"] = [f"life-{c['id']}" for c in cases if c["verdict"] != "skip"]
    res["coverage_note"] = (f"{len(cases)} approved LIFE cases; "
                            + ("; ".join(f"life-{c['id']}: {c['detail']}" for c in skipped[:5])
                               if skipped else "all runnable in v1")
                            + "; safety counters unobservable by LIFE cases")
    n = len(cases)
    cats = [{"name": "life", "n": n, "passed": len(passed),
             "rate": round(len(passed) / n, 4) if n else 0.0,
             "wilson95": wilson(n, len(passed))}]
    if not cases:
        res["coverage_note"] = "no approved LIFE cases yet (queue grows via nightly + ev review)"
    return finish(res, counts=(n, len(passed), len(failures), len(skipped), 0),
                  categories=cats)
