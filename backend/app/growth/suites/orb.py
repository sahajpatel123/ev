"""Adapter: orb headless bench (E2 thresholds: static 0 frames, CPU<=2%, GPU<=1.5ms)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..keys import suite_env
from ..result import finish, new_result, wilson

MACOS = Path(__file__).resolve().parents[4] / "macos"
ORB = MACOS / "Packages" / "EvieOrb"
TOOL = ORB / ".build" / "release" / "EvieOrbPreviewTool"

CPU_BUDGET = 2.0
GPU_BUDGET = 1.5


def verdict_row(r: dict) -> tuple[bool, str]:
    """Pure per-row oracle (unit-testable)."""
    state = str(r.get("state"))
    static = state in ("idle", "offline")
    frames = int(r.get("framesRendered", -1))
    cpu = float(r.get("processCPUPct", 999))
    gpu = float(r.get("frameGPUSteadyMaxMs", -1))
    spikes = int(r.get("gpuSpikesOverBudget", 0))
    if static and frames != 0:
        return False, f"static state rendered {frames} frames"
    if cpu > CPU_BUDGET:
        return False, f"cpu {cpu:.2f}% > {CPU_BUDGET}%"
    if gpu >= 0 and (gpu > GPU_BUDGET or spikes > 0):
        return False, f"gpu steady max {gpu:.3f}ms spikes={spikes} (budget {GPU_BUDGET}ms)"
    return True, ""


def run(*, budget: float | None, sample: float, out: str) -> dict:
    res = new_result("orb", sample=sample, budget=budget)
    if not TOOL.is_file():
        p = subprocess.run(["swift", "build", "-c", "release", "--product", "EvieOrbPreviewTool"],
                           cwd=str(ORB), capture_output=True, text=True, env=suite_env(), timeout=1800)
        if not TOOL.is_file():
            res["status"] = "blocked"
            res["coverage_note"] = f"bench tool build failed: {(p.stderr or '')[-200:]}"
            return finish(res, counts=(0, 0, 0, 0, 1))
    outdir = Path(out).expanduser().parent / "orb-bench"
    p = subprocess.run([str(TOOL), "bench", "--seconds", "10", "--fps", "60",
                        "--sizes", "120", "--outdir", str(outdir)],
                       capture_output=True, text=True, env=suite_env(), timeout=1800)
    rj = outdir / "results.json"
    if not rj.is_file():
        res["status"] = "blocked"
        res["coverage_note"] = f"bench produced no results.json: {(p.stderr or '')[-200:]}"
        return finish(res, counts=(0, 0, 0, 0, 1))
    runs = json.loads(rj.read_text())
    if sample < 1.0:
        from ..result import stride

        keep = set(stride([f"{r.get('state')}/{r.get('runKind')}" for r in runs], sample))
        runs = [r for r in runs if f"{r.get('state')}/{r.get('runKind')}" in keep]
        res["status"] = "partial"
    failures = []
    gpus = []
    for r in runs:
        state = str(r.get("state"))
        kind = str(r.get("runKind", "?"))
        cid = f"{state}/{kind}/120pt"
        gpu = float(r.get("frameGPUSteadyMaxMs", -1))
        if gpu >= 0:
            gpus.append(gpu)
        ok, why = verdict_row(r)
        res["cases_run"].append(cid)
        if not ok:
            failures.append({"id": cid,
                             "repro": ("cd macos/Packages/EvieOrb && swift run -c release EvieOrbPreviewTool "
                                       "bench --seconds 10 --fps 60 --sizes 120"),
                             "detail": why})
    from ..result import percentile

    res["failures"] = failures
    res["latency_ms"] = {"n": len(gpus), "p50": percentile(gpus, 0.5), "p95": percentile(gpus, 0.95)}
    res["coverage_note"] = (f"120pt/60fps/10s per state x runKind; steady-state GPU oracle "
                            f"(cpu<={CPU_BUDGET}% gpu<={GPU_BUDGET}ms, spikes=0); "
                            f"safety counters unobservable by the bench")
    cats = [{"name": "states", "n": len(runs), "passed": len(runs) - len(failures),
             "rate": 0.0, "wilson95": [0.0, 1.0]}]
    if runs:
        cats[0]["rate"] = round((len(runs) - len(failures)) / len(runs), 4)
        cats[0]["wilson95"] = wilson(len(runs), len(runs) - len(failures))
    return finish(res, counts=(len(runs), len(runs) - len(failures), len(failures), 0, 0),
                  categories=cats)
