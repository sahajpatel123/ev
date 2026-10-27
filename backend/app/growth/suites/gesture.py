"""Adapter: PresenceChecks (gesture replay D1: 0 activations required)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..keys import suite_env
from ..result import finish, new_result, wilson

MACOS = Path(__file__).resolve().parents[4] / "macos"
TOOL = MACOS / ".build" / "release" / "PresenceChecks"


def run(*, budget: float | None, sample: float, out: str) -> dict:
    res = new_result("gesture", sample=sample, budget=budget)
    if not TOOL.is_file():
        p = subprocess.run(["swift", "build", "-c", "release", "--product", "PresenceChecks"],
                           cwd=str(MACOS), capture_output=True, text=True, env=suite_env(), timeout=1800)
        if not TOOL.is_file():
            res["status"] = "blocked"
            res["coverage_note"] = f"PresenceChecks build failed: {(p.stderr or '')[-200:]}"
            return finish(res, counts=(0, 0, 0, 0, 1))
    p = subprocess.run([str(TOOL)], capture_output=True, text=True, env=suite_env(), timeout=1200)
    text = p.stdout or ""
    m = re.search(r"passed=(\d+)\s+failed=(\d+)", text)
    r = re.search(r"events=(\d+).*activations=(\d+)", text)
    if not m or not r:
        res["status"] = "blocked"
        res["coverage_note"] = (f"PresenceChecks output missing counts or replay line "
                                f"(oracle drift?): {text[-200:]}")
        return finish(res, counts=(0, 0, 0, 0, 1))
    passed, failed = int(m.group(1)), int(m.group(2))
    events, activations = int(r.group(1)), int(r.group(2))
    # The binary self-counts zero-activations (replay-zero-activations check):
    # passed/failed already carry the D1 verdict. The replay line is context.
    fails = [ln for ln in text.splitlines() if ln.startswith("FAIL")]
    res["failures"] = [{"id": f, "repro": "cd macos && swift run -c release PresenceChecks", "detail": f}
                       for f in fails[:20]]
    res["cases_run"] = [f"presence-checks ({passed + failed} checks, {events} replay events)"]
    res["coverage_note"] = (f"replay events={events} activations={activations}; "
                            "D1 oracle: 0 activations (counted by the binary itself); "
                            "safety counters unobservable by gesture replay"
                            + ("" if sample >= 1.0 else "; sample ignored (single binary)"))
    n = passed + failed
    return finish(res, counts=(n, passed, failed, 0, 0),
                  categories=[{"name": "presence-checks", "n": n, "passed": passed,
                               "rate": round(passed / n, 4) if n else 0.0,
                               "wilson95": wilson(n, passed)}])
