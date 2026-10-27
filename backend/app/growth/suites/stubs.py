"""Adapters for suites that do not exist yet: honest not_built stubs."""

from __future__ import annotations

from ..result import finish, new_result

REASONS = {
    "latency": "no committed latency probe; needs provider key + target instance",
    "soak": "committed runner lands in GE8 (weekly 2h soak); 15-min S4 evidence in RELEASE_REPORT",
    "life": "LIFE suite lands in GE4 (grows from approved failure candidates)",
}


def _stub(suite: str, *, budget: float | None, sample: float, out: str) -> dict:
    res = new_result(suite, status="not_built", sample=sample, budget=budget)
    res["coverage_note"] = REASONS[suite]
    return finish(res, counts=(0, 0, 0, 0, 0))


def run_latency(*, budget: float | None, sample: float, out: str) -> dict:
    return _stub("latency", budget=budget, sample=sample, out=out)


def run_soak(*, budget: float | None, sample: float, out: str) -> dict:
    return _stub("soak", budget=budget, sample=sample, out=out)


def run_life(*, budget: float | None, sample: float, out: str) -> dict:
    return _stub("life", budget=budget, sample=sample, out=out)
