"""GE3 stage 3 RC: the release-candidate battery + baseline comparison.

Runs every $0 suite plus budgeted benchmark smokes against a frozen
worktree of the submission branch, then compares each result to the
stored baseline for the base commit:

- Statistically significant regression (non-overlapping Wilson 95%
  intervals, RC below base) BLOCKS.
- ANY measured safety counter > 0 BLOCKS.
- Benchmark suites that need the eval key and lack it are recorded as
  gaps: verdict ``pass-with-gaps`` (ship shows the gaps to the owner).
  Nothing is ever counted as passing without running.

Every suite runs via subprocess with the WORKTREE's code (never the
captain's tree): ``python -m app.growth.run`` with cwd=worktree/backend.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ..store import recent
from .gate import ensure_worktree

REPO = Path(__file__).resolve().parents[4]

# (suite, budget, sample). Budgets are caps; the runner clamps to room.
BATTERY: list[tuple[str, float | None, float]] = [
    ("unit", 0.0, 1.0),
    ("gates", 0.0, 1.0),
    ("gauntlet", 0.0, 0.25),
    ("scorecard", 1.0, 0.2),
    ("cug", 0.0, 1.0),
    ("journeys", 0.0, 1.0),
    ("orb", 0.0, 1.0),
    ("gesture", 0.0, 1.0),
]


def intervals_overlap(a: list[float], b: list[float]) -> bool:
    """Pure Wilson-overlap check (unit-testable)."""
    try:
        (a0, a1), (b0, b1) = (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))
    except (IndexError, TypeError, ValueError):
        return True  # unparseable intervals never convict
    return max(a0, b0) <= min(a1, b1)


def is_regression(base: dict, rc: dict) -> tuple[bool, str]:
    """True when RC is statistically significantly below base."""
    if not base or base.get("n", 0) == 0 or rc.get("n", 0) == 0:
        return False, "no baseline or empty run; cannot compare"
    if rc.get("status") in ("blocked", "not_built"):
        return False, f"rc {rc['status']}; recorded as gap, not compared"
    if base.get("status") in ("blocked", "not_built"):
        return False, "baseline unusable; this run becomes the baseline"
    if rc["pass_rate"] >= base["pass_rate"]:
        return False, "rc at or above baseline"
    if intervals_overlap(base.get("wilson95") or [0, 1], rc.get("wilson95") or [0, 1]):
        return False, "below baseline but intervals overlap (noise, not regression)"
    return True, (f"REGRESSION: base {base['pass_rate']} {base.get('wilson95')} vs "
                  f"rc {rc['pass_rate']} {rc.get('wilson95')}")


def safety_violations(res: dict) -> list[str]:
    out = []
    for k, v in (res.get("safety") or {}).items():
        if v is not None and v > 0:
            out.append(f"{k}={v}")
    return out


def baseline_for(suite: str, base_commit: str) -> dict | None:
    for r in recent(suite, 50):
        if r.get("commit") == base_commit:
            return r
    return None


def run_suite(worktree_backend: Path, suite: str, budget: float | None,
              sample: float, out: Path, timeout: int = 10800) -> dict:
    cmd = [sys.executable, "-m", "app.growth.run", suite,
           "--sample", str(sample), "--out", str(out)]
    if budget is not None:
        cmd += ["--budget", str(budget)]
    p = subprocess.run(cmd, cwd=str(worktree_backend), capture_output=True,
                       text=True, timeout=timeout)
    if not out.is_file():
        return {"suite": suite, "status": "blocked", "n": 0,
                "coverage_note": f"harness produced no output (rc={p.returncode}): {(p.stderr or '')[-300:]}"}
    try:
        return json.loads(out.read_text())
    except ValueError:
        return {"suite": suite, "status": "blocked", "n": 0,
                "coverage_note": "harness output unparseable"}


def run_rc(branch: str, base: str, out_dir: Path) -> dict:
    """Full RC battery. Returns the RC report (also written to out_dir)."""
    wt = ensure_worktree(branch)
    head = subprocess.run(["git", "rev-parse", branch], cwd=str(REPO),
                          capture_output=True, text=True, check=True).stdout.strip()
    base_sha = subprocess.run(["git", "rev-parse", base], cwd=str(REPO),
                              capture_output=True, text=True, check=True).stdout.strip()
    out_dir.mkdir(parents=True, exist_ok=True)
    suites = {}
    for suite, budget, sample in BATTERY:
        out = out_dir / f"{suite}.json"
        res = run_suite(wt / "backend", suite, budget, sample, out)
        base_res = baseline_for(suite, base_sha)
        reg, why = is_regression(base_res or {}, res)
        suites[suite] = {
            "status": res.get("status"), "n": res.get("n", 0),
            "passed": res.get("passed", 0), "failed": res.get("failed", 0),
            "pass_rate": res.get("pass_rate", 0.0),
            "wilson95": res.get("wilson95", [0.0, 1.0]),
            "safety": res.get("safety", {}),
            "violations": safety_violations(res),
            "regression": reg, "compare_note": why,
            "baseline_commit": base_sha if base_res else None,
            "coverage_note": res.get("coverage_note", ""),
        }
    blockers = [f"{s}: regression ({v['compare_note']})" for s, v in suites.items() if v["regression"]]
    blockers += [f"{s}: safety {','.join(v['violations'])}" for s, v in suites.items() if v["violations"]]
    gaps = [f"{s}: {v['status']} ({v['coverage_note'][:150]})" for s, v in suites.items()
            if v["status"] in ("blocked", "not_built")]
    verdict = "blocked" if blockers else ("pass-with-gaps" if gaps else "pass")
    report = {"stage": "rc", "branch": branch, "head": head, "base": base,
              "base_commit": base_sha, "verdict": verdict, "suites": suites,
              "blockers": blockers, "gaps": gaps}
    (out_dir / "rc-report.json").write_text(json.dumps(report, indent=1))
    return report
