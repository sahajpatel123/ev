"""Adapter: orb headless bench (E2 thresholds: static 0 frames, CPU<=2%, GPU<=1.5ms)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..result import finish, new_result, wilson

MACOS = Path(__file__).resolve().parents[5] / "macos"
ORB = MACOS / "Packages" / "EvieOrb"
TOOL = ORB / ".build" / "release" / "EvieOrbPreviewTool"

CPU_BUDGET = 2.0
GPU_BUDGET = 1.5


def run(*, budget: float | None, sample: float, out: str) -> dict:
    res = new_result("orb", sample=sample, budget=budget)
    if not TOOL.is_file():
        p = subprocess.run(["swift", "build", "-c", "release", "--product", "EvieOrbPreviewTool"],
                           cwd=str(ORB), capture_output=True, text=True, timeout=1800)
        if not TOOL.is_file():
            res["status"] = "blocked"
            res["coverage_note"] = f"bench tool build failed: {(p.stderr or '')[-200:]}"
            return finish(res, counts=(0, 0, 0, 0, 1))
    outdir = Path(out).expanduser().parent / "orb-bench"
    p = subprocess.run([str(TOOL), "bench", "--seconds", "10", "--fps", "60",
                        "--sizes", "120", "--outdir", str(outdir)],
                       capture_output=True, text=True, timeout=1800)
    rj = outdir / "results.json"
    if not rj.is_file():
        res["status"] = "blocked"
        res["coverage_note"] = f"bench produced no results.json: {(p.stderr or '')[-200:]}"
        return finish(res, counts=(0, 0, 0, 0, 1))
    runs = json.loads(rj.read_text())
    if sample < 1.0:
        from ..result import stride

        keep = set(stride([f"{r.get('state')}" for r in runs], sample))
        runs = [r for r in runs if r.get("state") in keep]
        res["status"] = "partial"
    failures = []
    gpus = []
    for r in runs:
        state = str(r.get("state"))
        static = state in ("idle", "offline")
        frames = int(r.get("framesRendered", -1))
        cpu = float(r.get("processCPUPct", 999))
        gpu = float(r.get("frameGPUMaxMs", -1))
        ok = True
        why = ""
        if static and frames != 0:
            ok, why = False, f"static state rendered {frames} frames"
        if cpu > CPU_BUDGET:
            ok, why = False, f"cpu {cpu:.2f}% > {CPU_BUDGET}%"
        if gpu >= 0:
            gpus.append(gpu)
            if gpu > GPU_BUDGET:
                ok, why = False, f"gpu max {gpu:.3f}ms > {GPU_BUDGET}ms"
        res["cases_run"].append(f"{state}/120pt")
        if not ok:
            failures.append({"id": state, "repro": "macos/scripts/orb-benchmark (10s matrix)",
                             "detail": why})
    from ..result import percentile

    res["failures"] = failures
    res["latency_ms"] = {"n": len(gpus), "p50": percentile(gpus, 0.5), "p95": percentile(gpus, 0.95)}
    res["safety"] = {"unauthorized_effects": 0, "false_success": 0,
                     "silent_turns": None, "fanout_violations": None}
    res["coverage_note"] = f"120pt/60fps/10s per state; thresholds cpu<={CPU_BUDGET}% gpu<={GPU_BUDGET}ms"
    cats = [{"name": "states", "n": len(runs), "passed": len(runs) - len(failures),
             "rate": 0.0, "wilson95": [0.0, 1.0]}]
    if runs:
        cats[0]["rate"] = round((len(runs) - len(failures)) / len(runs), 4)
        cats[0]["wilson95"] = wilson(len(runs), len(runs) - len(failures))
    return finish(res, counts=(len(runs), len(runs) - len(failures), len(failures), 0, 0),
                  categories=cats)
