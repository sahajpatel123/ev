"""Growth engine: standard eval results (docs/contracts/EVAL_SUITE.md)."""

from __future__ import annotations

import math
from datetime import UTC, datetime


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def wilson(n: int, passed: int, z: float = 1.96) -> list[float]:
    """95% Wilson score interval. n=0 -> [0, 1] (unknown, not zero)."""
    if n <= 0:
        return [0.0, 1.0]
    p = min(1.0, max(0.0, passed / n))
    denom = 1.0 + z * z / n
    center = p + z * z / (2.0 * n)
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return [round(max(0.0, (center - half) / denom), 4),
            round(min(1.0, (center + half) / denom), 4)]


def git_commit(repo: str | None = None) -> str:
    import subprocess
    from pathlib import Path

    root = Path(repo or Path(__file__).resolve().parents[3])
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "--short=9", "HEAD"],
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def stride(ids: list[str], sample: float) -> list[str]:
    """Deterministic stride over sorted ids. sample=1 -> all."""
    ids = sorted(ids)
    if sample >= 1.0 or not ids:
        return ids
    k = max(1, round(1.0 / sample))
    return ids[::k]


def new_result(suite: str, *, status: str = "ok", sample: float = 1.0,
               budget: float | None = None) -> dict:
    return {
        "suite": suite, "version": 1, "status": status,
        "commit": git_commit(), "started": utcnow(), "finished": "",
        "n": 0, "passed": 0, "failed": 0, "skipped": 0, "errors": 0,
        "pass_rate": 0.0, "wilson95": [0.0, 1.0], "categories": [],
        "cost_usd": 0.0, "cost_source": "none",
        "latency_ms": {"n": 0, "p50": None, "p95": None},
        "safety": {"unauthorized_effects": None, "false_success": None,
                   "silent_turns": None, "fanout_violations": None},
        "failures": [], "cases_run": [], "coverage_note": "",
        "sample": sample, "budget_usd": budget,
    }


def finish(res: dict, *, counts: tuple[int, int, int, int, int],
           categories: list[dict] | None = None) -> dict:
    n, passed, failed, skipped, errors = counts
    decided = n - skipped  # skips are disclosed, never scored
    res.update({"n": n, "passed": passed, "failed": failed,
                "skipped": skipped, "errors": errors,
                "pass_rate": round(passed / decided, 4) if decided else 0.0,
                "wilson95": wilson(decided, passed),
                "categories": categories or [], "finished": utcnow()})
    return res


def percentile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def write_json(path: str, obj: dict) -> str:
    import json
    from pathlib import Path

    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.rename(p)
    return str(p)
