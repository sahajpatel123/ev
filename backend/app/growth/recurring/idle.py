"""rec-idle-app / rec-idle-backend: idle CPU sampling with honesty notes.

Samples CPU over 60s (6 x 10s). Reports mean/max + machine load; if the
machine is busy (1-min load above half the cores) the result is still recorded
but flagged MACHINE BUSY so nobody mistakes a busy-box sample for an idle one.
Targets: app <= 1%, backend combined <= 3% (G5).

Methodology note: `ps %cpu` is windowed on macOS but lifetime-average on
Linux — on Linux these numbers are approximate (stated in coverage_note).
"""

from __future__ import annotations

import os
import subprocess
import time

from ..result import wilson


def _pids(pattern: str) -> list[int]:
    p = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=15)
    return [int(x) for x in p.stdout.split() if x.isdigit()]


def _cpu_pct(pid: int) -> float | None:
    p = subprocess.run(["ps", "-o", "%cpu=", "-p", str(pid)], capture_output=True,
                       text=True, timeout=15)
    try:
        return float(p.stdout.strip())
    except ValueError:
        return None


def _sample(patterns: list[str], seconds: float = 60.0, step: float = 10.0) -> dict:
    samples: list[float] = []
    pids: set[int] = set()
    n = max(1, int(seconds / step))
    for _ in range(n):
        found = [pid for pat in patterns for pid in _pids(pat)]
        pids.update(found)
        total = 0.0
        for pid in found:
            v = _cpu_pct(pid)
            if v is not None:
                total += v
        samples.append(total)
        time.sleep(step)
    try:
        load1 = os.getloadavg()[0]
        cores = os.cpu_count() or 1
    except OSError:
        load1, cores = 0.0, 1
    return {"samples": samples, "pids": sorted(pids), "load1": load1, "cores": cores,
            "mean": sum(samples) / len(samples) if samples else 0.0,
            "max": max(samples) if samples else 0.0}


def _verdict(suite: str, target: float, data: dict) -> dict:
    from ..result import finish as F
    from ..result import new_result as N
    res = N(suite)
    busy = data["load1"] > data["cores"] / 2
    res["cases_run"] = [f"idle-cpu-{suite}"]
    load_note = f"load1={data['load1']:.1f}/{data['cores']} cores"
    if not data["pids"]:
        res["status"] = "partial"  # SKIP is never "ok"
        res["coverage_note"] = (f"no matching processes; nothing sampled; {load_note} — "
                                "SKIP, not a pass (Evie.app not launched)" if "app" in suite else
                                f"no matching processes; {load_note}")
        return F(res, counts=(1, 0, 0, 1, 0),
                 categories=[{"name": "idle", "n": 1, "passed": 0,
                              "rate": 0.0, "wilson95": wilson(1, 0)}])
    ok = data["mean"] <= target
    if not ok:
        res["failures"] = [{"id": f"idle-cpu-{suite}", "repro": f"ev growth recurring {suite[4:]}",
                            "detail": f"mean {data['mean']:.2f}% > {target}% (max {data['max']:.2f}%)"}]
    import sys
    res["coverage_note"] = (f"6x10s samples over 60s; pids={data['pids']}; {load_note}"
                            + ("; MACHINE BUSY — sample confounded, re-run quiet before convicting"
                               if busy else "; machine quiet")
                            + ("; ps-%cpu lifetime-average on Linux — approximate"
                               if not sys.platform.startswith("darwin") else ""))
    passed = 1 if ok else 0
    return F(res, counts=(1, passed, 1 - passed, 0, 0),
             categories=[{"name": "idle", "n": 1, "passed": passed,
                          "rate": float(passed), "wilson95": wilson(1, passed)}])


def run_app() -> dict:
    return _verdict("rec-idle-app", 1.0, _sample(["Evie.app/Contents/MacOS", "Evie\\.app"]))


def run_backend() -> dict:
    return _verdict("rec-idle-backend", 3.0,
                    _sample(["uvicorn app.main", "ev\\.api", "ev\\.talk", "ev\\.worker"]))
