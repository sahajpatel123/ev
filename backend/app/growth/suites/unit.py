"""Adapter: full pytest suite (backend/tests), stride-sampled, junit-parsed."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from ..keys import suite_env
from ..result import finish, new_result, percentile, stride

BACKEND = Path(__file__).resolve().parents[3]


def _collect() -> list[str]:
    p = subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q",
                        "--no-header", "-p", "no:cacheprovider"],
                       cwd=str(BACKEND), capture_output=True, text=True, env=suite_env(), timeout=600)
    ids = [ln.strip() for ln in p.stdout.splitlines()
           if ln.strip() and "::" in ln and not ln.startswith(("=", "tests collected", "no tests")) ]
    # pytest collect-only prints node ids one per line; warnings go to stderr
    return ids or [ln.strip() for ln in p.stdout.splitlines() if "::" in ln]


def parse_junit(path: str) -> dict:
    tree = ET.parse(path)
    cases: list[dict] = []
    for tc in tree.iter("testcase"):
        name = f"{tc.get('classname', '')}::{tc.get('name', '')}"
        failed = tc.find("failure") is not None or tc.find("error") is not None
        skipped = tc.find("skipped") is not None
        detail = ""
        el = tc.find("failure")
        if el is None:
            el = tc.find("error")
        if el is not None:
            detail = (el.get("message") or "")[:200]
        try:
            dur = float(tc.get("time", "0") or 0) * 1000.0
        except ValueError:
            dur = 0.0
        cases.append({"id": name, "failed": failed, "skipped": skipped,
                      "detail": detail, "ms": dur,
                      "file": (tc.get("classname") or "").split(".")[0]})
    return {"cases": cases}


def run(*, budget: float | None, sample: float, out: str) -> dict:
    res = new_result("unit", sample=sample, budget=budget)
    ids = _collect()
    chosen = stride(ids, sample)
    res["coverage_note"] = (f"S={sample}, {len(chosen)}/{len(ids)} tests by stride"
                            if sample < 1.0 else f"all {len(ids)} tests")
    if not chosen:
        res["status"] = "blocked"
        res["coverage_note"] = "collection found no tests"
        return finish(res, counts=(0, 0, 0, 0, 0))
    with tempfile.TemporaryDirectory(prefix="ge-unit-") as tmp:
        junit = str(Path(tmp) / "r.xml")
        subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider",
                        f"--junitxml={junit}", *chosen],
                       cwd=str(BACKEND), capture_output=True, text=True, env=suite_env(), timeout=10800)
        parsed = parse_junit(junit)
    cases = parsed["cases"]
    by_file: dict[str, dict] = {}
    failures = []
    lats = []
    for c in cases:
        b = by_file.setdefault(c["file"], {"n": 0, "passed": 0})
        b["n"] += 1
        lats.append(c["ms"])
        if c["failed"]:
            failures.append({"id": c["id"], "repro": f"pytest {c['id']}", "detail": c["detail"]})
        elif not c["skipped"]:
            b["passed"] += 1
    from ..result import wilson

    cats = [{"name": k, "n": v["n"], "passed": v["passed"],
             "rate": round(v["passed"] / v["n"], 4) if v["n"] else 0.0,
             "wilson95": wilson(v["n"], v["passed"])} for k, v in sorted(by_file.items())]
    n = len(cases)
    failed = len(failures)
    skipped = sum(1 for c in cases if c["skipped"])
    res["failures"] = failures
    res["cases_run"] = [c["id"] for c in cases]
    res["latency_ms"] = {"n": len(lats), "p50": percentile(lats, 0.5), "p95": percentile(lats, 0.95)}
    res["coverage_note"] += "; safety counters unobservable by unit tests"
    return finish(res, counts=(n, n - failed - skipped, failed, skipped, 0),
                  categories=cats)
