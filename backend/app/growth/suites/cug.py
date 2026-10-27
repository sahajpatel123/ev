"""Adapter: CUG sim+corpus+live+live_term (live lanes need GUI/display).

Runs the committed runner, reads only JSON files this run freshly wrote,
then restores the pre-run bytes of docs/cug/ so the tree stays clean.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import time
from pathlib import Path

from ..keys import suite_env
from ..result import finish, new_result, percentile, wilson

BACKEND = Path(__file__).resolve().parents[3]
CUGDIR = BACKEND.parent / "docs" / "cug"
LANES = ["sim", "corpus", "live", "live_term"]


def _run_lane(mode: str) -> tuple[int, str]:
    p = subprocess.run([sys.executable, "-m", "hands_cug.runner", mode],
                       cwd=str(BACKEND), capture_output=True, text=True,
                       env=suite_env(), timeout=5400)
    return p.returncode, (p.stderr or "")[-200:]


def snapshot() -> dict[str, bytes | None]:
    """Snapshot pre-run bytes (never `git checkout`: that would destroy the
    owner's uncommitted edits under docs/cug/)."""
    return {lane: (p.read_bytes() if (p := CUGDIR / f"{lane}.json").is_file() else None)
            for lane in LANES}


def restore(before: dict[str, bytes | None]) -> None:
    for lane in LANES:
        p = CUGDIR / f"{lane}.json"
        if before[lane] is None:
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
        else:
            p.write_bytes(before[lane])


def run(*, budget: float | None, sample: float, out: str) -> dict:
    res = new_result("cug", sample=sample, budget=budget)
    before = snapshot()
    lane_notes = []
    scored: list[dict] = []
    try:
        for lane in LANES:
            started = time.time()
            rc, err = _run_lane(lane)
            p = CUGDIR / f"{lane}.json"
            fresh = p.is_file() and p.stat().st_mtime >= started - 1
            if rc == 0 and fresh:
                with contextlib.suppress(ValueError):
                    scored.extend(json.loads(p.read_text()))
                lane_notes.append(f"{lane}: fresh")
            else:
                lane_notes.append(
                    f"{lane}: NO FRESH OUTPUT (rc={rc}, "
                    f"{'missing' if not p.is_file() else 'stale'}); lane excluded{'; ' + err if err else ''}")
    finally:
        restore(before)
    if not scored:
        res["status"] = "blocked"
        res["coverage_note"] = "no lane produced fresh output: " + "; ".join(lane_notes)
        return finish(res, counts=(0, 0, 0, 0, 1))
    if sample < 1.0:
        from ..result import stride

        keep = set(stride([s.get("id", "") for s in scored], sample))
        scored = [s for s in scored if s.get("id") in keep]
        res["status"] = "partial"
    elif any("NO FRESH" in n for n in lane_notes):
        res["status"] = "partial"
    by_mode: dict[str, dict] = {}
    failures = []
    lats = []
    fs = sv = 0
    for s in scored:
        m = by_mode.setdefault(str(s.get("mode", "?")), {"n": 0, "passed": 0})
        m["n"] += 1
        if s.get("passed"):
            m["passed"] += 1
        else:
            failures.append({"id": str(s.get("id")),
                             "repro": f"python -m hands_cug.runner {s.get('mode')} {s.get('id')}",
                             "detail": str(s.get("note") or s.get("say") or "")[:200]})
        if s.get("false_success"):
            fs += 1
        if s.get("scope_violation"):
            sv += 1
        extra = s.get("extra") or {}
        if isinstance(extra, dict) and extra.get("latency_ms") is not None:
            with contextlib.suppress(TypeError, ValueError):
                lats.append(float(extra["latency_ms"]))
    cats = [{"name": k, "n": v["n"], "passed": v["passed"],
             "rate": round(v["passed"] / v["n"], 4) if v["n"] else 0.0,
             "wilson95": wilson(v["n"], v["passed"])} for k, v in sorted(by_mode.items())]
    res["failures"] = failures
    res["cases_run"] = [str(s.get("id")) for s in scored]
    res["latency_ms"] = {"n": len(lats), "p50": percentile(lats, 0.5), "p95": percentile(lats, 0.95)}
    res["safety"] = {"unauthorized_effects": sv, "false_success": fs,
                     "silent_turns": None, "fanout_violations": None}
    res["coverage_note"] = ("lanes: " + "; ".join(lane_notes) +
                            "; safety: scope violations counted, silent/fanout unobservable by CUG")
    n = len(scored)
    return finish(res, counts=(n, n - len(failures), len(failures), 0, 0), categories=cats)
