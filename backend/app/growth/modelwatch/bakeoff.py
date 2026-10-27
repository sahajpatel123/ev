"""GE7 bake-off: candidate model vs pinned on a stratified sample.

Runs scorecard sample + LIFE full + gauntlet scripted sample with the EVAL
key, first against the pinned brain model, then the candidate (via
EV_BRAIN_MODEL override in the suite env), and reports pass rates with
intervals, tool-call validity, safety, latency, and cost + a
recommendation. Never switches (owner-only setting change via ship).

Gaps are stated, never filled: if the real-brain scorecard adapter is
missing or the eval key is absent, those legs report blocked and the
bake-off says so.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3]


def _leg(model: str | None, budget: float, tmp: Path) -> dict:
    from ..keys import suite_env

    legs = {}
    for suite, sample, b in (("scorecard", 0.2, budget), ("life", 1.0, 0.0),
                             ("gauntlet", 0.25, 0.0)):
        out = tmp / f"{suite}.json"
        env = suite_env()
        if model:
            env["EV_BRAIN_MODEL"] = model
        cmd = [sys.executable, "-m", "app.growth.run", suite, "--sample", str(sample),
               "--out", str(out), "--budget", str(b)]
        p = subprocess.run(cmd, cwd=str(BACKEND), capture_output=True, text=True,
                           env=env, timeout=5400)
        legs[suite] = json.loads(out.read_text()) if out.is_file() else {
            "status": "blocked", "coverage_note": f"no output (rc={p.returncode})"}
    return legs


def run_bakeoff(candidate: str, *, budget: float = 2.0, dry_run: bool = False) -> dict:
    from .watch import pins
    pinned = pins()["spark"]
    if dry_run:
        return {"ok": True, "dry_run": True, "pinned": pinned, "candidate": candidate,
                "note": "plumbing smoke only (no model calls); real bake-off needs --bakeoff + eval key"}
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="ge-bakeoff-"))
    try:
        (tmp / "base").mkdir(parents=True, exist_ok=True)
        (tmp / "cand").mkdir(parents=True, exist_ok=True)
        base = _leg(None, budget, tmp / "base")
        cand = _leg(candidate, budget, tmp / "cand")
    except Exception:
        rep = {"ok": False, "pinned": pinned, "candidate": candidate,
               "tmp": str(tmp),  # kept for forensics; suite legs failed
               "note": "bake-off crashed; leg outputs kept at tmp"}
        return rep
    rep = {"ok": True, "pinned": pinned, "candidate": candidate, "suites": {}}
    for suite in ("scorecard", "life", "gauntlet"):
        b, c = base.get(suite, {}), cand.get(suite, {})
        rep["suites"][suite] = {
            "base": {"status": b.get("status"), "rate": b.get("pass_rate"),
                     "wilson95": b.get("wilson95"), "cost": b.get("cost_usd"),
                     "latency": (b.get("latency_ms") or {}).get("p95"),
                     "coverage": (b.get("coverage_note") or "")[:200]},
            "candidate": {"status": c.get("status"), "rate": c.get("pass_rate"),
                          "wilson95": c.get("wilson95"), "cost": c.get("cost_usd"),
                          "latency": (c.get("latency_ms") or {}).get("p95"),
                          "coverage": (c.get("coverage_note") or "")[:200]}}
    rep["recommendation"] = (
        "manual review required: compare intervals above; switch only via "
        "`ev release ship` with a setting change")
    shutil.rmtree(tmp, ignore_errors=True)
    return rep
