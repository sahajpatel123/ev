"""Adapter: full pytest suite (backend/tests), stride-sampled, junit-parsed."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from ..keys import suite_env
from ..result import finish, new_result, percentile, stride, wilson

BACKEND = Path(__file__).resolve().parents[3]

_SCRUBS = [
    (re.compile(r"(EV_[A-Z_]*(?:KEY|SECRET|TOKEN|PASSWORD)[A-Z_]*\s*[=:]\s*['\"]?)[^'\"\s]+"),
     r"\1<redacted>"),
    (re.compile(r"sk-(?:or-v1-)?[A-Za-z0-9_-]{8,}"), "<redacted-key>"),
]


def scrub(text: str) -> str:
    """Remove key material a failing test may have dumped into junit output."""
    for rx, repl in _SCRUBS:
        text = rx.sub(repl, text)
    return text


def _collect() -> list[str]:
    p = subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q",
                        "--no-header", "-p", "no:cacheprovider"],
                       cwd=str(BACKEND), capture_output=True, text=True, env=suite_env(), timeout=600)
    ids = [ln.strip() for ln in p.stdout.splitlines()
           if ln.strip() and "::" in ln and not ln.startswith(("=", "tests collected", "no tests"))]
    # pytest collect-only prints node ids one per line; warnings go to stderr
    return ids or [ln.strip() for ln in p.stdout.splitlines() if "::" in ln]


def _node_index(chosen: list[str]) -> dict[tuple[str, str], str]:
    """Map (junit classname, name) back to the pytest node id that ran."""
    index = {}
    for node in chosen:
        parts = node.split("::")
        dotted = parts[0].replace("/", ".").removesuffix(".py")
        classname = dotted + "." + ".".join(parts[1:-1]) if len(parts) > 2 else dotted
        index[(classname, parts[-1])] = node
    return index


def parse_junit(path: str, index: dict[tuple[str, str], str] | None = None) -> dict:
    tree = ET.parse(path)
    cases: list[dict] = []
    for tc in tree.iter("testcase"):
        classname = tc.get("classname") or ""
        name = tc.get("name") or ""
        node = (index or {}).get((classname, name), f"{classname}::{name}")
        failed = tc.find("failure") is not None
        errored = tc.find("error") is not None
        skipped = tc.find("skipped") is not None
        detail = ""
        el = tc.find("failure")
        if el is None:
            el = tc.find("error")
        if el is not None:
            detail = scrub((el.get("message") or "")[:300])
        try:
            dur = float(tc.get("time", "0") or 0) * 1000.0
        except ValueError:
            dur = 0.0
        cases.append({"id": node, "failed": failed, "errored": errored and not failed,
                      "skipped": skipped, "detail": detail, "ms": dur,
                      "file": node.split("::")[0]})
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
    index = _node_index(chosen)
    with tempfile.TemporaryDirectory(prefix="ge-unit-") as tmp:
        junit = str(Path(tmp) / "r.xml")
        subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider",
                        f"--junitxml={junit}", *chosen],
                       cwd=str(BACKEND), capture_output=True, text=True, env=suite_env(), timeout=10800)
        parsed = parse_junit(junit, index)
    cases = parsed["cases"]
    by_file: dict[str, dict] = {}
    failures = []
    lats = []
    for c in cases:
        b = by_file.setdefault(c["file"], {"n": 0, "passed": 0})
        b["n"] += 1
        lats.append(c["ms"])
        if c["failed"] or c["errored"]:
            failures.append({"id": c["id"], "repro": f"cd backend && pytest {c['id']}",
                             "detail": c["detail"]})
        elif not c["skipped"]:
            b["passed"] += 1
    cats = [{"name": k, "n": v["n"], "passed": v["passed"],
             "rate": round(v["passed"] / v["n"], 4) if v["n"] else 0.0,
             "wilson95": wilson(v["n"], v["passed"])} for k, v in sorted(by_file.items())]
    n = len(cases)
    failed = sum(1 for c in cases if c["failed"])
    errors = sum(1 for c in cases if c["errored"])
    skipped = sum(1 for c in cases if c["skipped"])
    res["failures"] = failures
    res["cases_run"] = [c["id"] for c in cases]
    res["latency_ms"] = {"n": len(lats), "p50": percentile(lats, 0.5), "p95": percentile(lats, 0.95)}
    res["coverage_note"] += "; safety counters unobservable by unit tests"
    if sample < 1.0:
        res["status"] = "partial"
    return finish(res, counts=(n, n - failed - errors - skipped, failed, skipped, errors),
                  categories=cats)
