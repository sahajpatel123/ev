"""Routing evidence gate: eval-gated fast/deep model routing readiness.

Routing stays disabled until this gate passes on real model-call evidence
(volume, latency, error rate per provider/model) from the ``model_calls`` audit
trail — the same evidence exposed by ``GET /v1/gateway/stats``. This is the
evaluation hook that the plan (D-13, WORK_BREAKDOWN 4.2) requires before any
routing policy may be enabled.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.services.model_call import model_call_stats


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class RoutingGateResult:
    name: str = "model_routing"
    passed: bool = False
    checks: list[Check] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


async def run_routing_gate(
    *,
    session: AsyncSession | None = None,
    window_hours: int = 24,
    min_calls: int = 5,
    max_error_rate: float = 0.25,
    max_p95_ms: float = 60_000.0,
) -> RoutingGateResult:
    """Gate routing on measured evidence; fail closed when evidence is missing."""

    started = time.perf_counter()
    if session is not None:
        stats = await model_call_stats(session, window_hours=window_hours)
    else:
        async with SessionLocal() as own_session:
            stats = await model_call_stats(own_session, window_hours=window_hours)

    totals = stats["totals"]
    calls = totals["calls"]
    bad = totals["errors"] + totals["blocked"]
    bad_rate = bad / calls if calls else 0.0
    checks = [
        Check(
            "evidence_volume",
            calls >= min_calls,
            f"{calls} model calls in {window_hours}h (need >= {min_calls})",
        ),
        Check(
            "provider_health",
            calls > 0 and bad_rate <= max_error_rate,
            f"error+blocked rate {bad_rate:.2%} (max {max_error_rate:.0%})",
        ),
        Check(
            "latency_budget",
            calls > 0 and totals["p95_latency_ms"] <= max_p95_ms,
            f"p95 latency {totals['p95_latency_ms']:.0f}ms (max {max_p95_ms:.0f}ms)",
        ),
    ]
    return RoutingGateResult(
        passed=all(check.passed for check in checks),
        checks=checks,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Model-routing evidence gate.")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    result = asyncio.run(run_routing_gate(window_hours=args.window_hours))
    payload = json.dumps(result.to_dict(), indent=2)
    print(payload)
    if args.report:
        args.report.write_text(payload + "\n")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
