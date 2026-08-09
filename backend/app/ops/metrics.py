"""Aggregate operational metrics: latency percentiles, cost estimates, budgets."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelCallLog
from app.ops.budgets import (
    LATENCY_BUDGETS_MS,
    MODEL_PRICES_USD_PER_1M,
    MONTHLY_COST_BUDGET_USD,
)
from app.utils.text import utcnow

METRICS_WINDOW_DAYS = 30


def estimate_cost_usd(
    *,
    provider: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Estimate the provider cost of one model call from token usage."""

    price = MODEL_PRICES_USD_PER_1M.get(provider) or MODEL_PRICES_USD_PER_1M["default"]
    return round(
        (prompt_tokens * price["input"] + completion_tokens * price["output"])
        / 1_000_000,
        6,
    )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((percentile / 100.0) * (len(ordered) - 1))))
    return round(ordered[index], 1)


def _ensure_aware(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=utcnow().tzinfo)
    return value


async def collect_metrics(
    session: AsyncSession,
    *,
    limit: int = 10_000,
) -> dict:
    """Aggregate model-call latency, error, and estimated-cost metrics.

    The report is intentionally cheap and personal-scale: it scans the last
    ``limit`` audit rows and computes percentiles/cost estimates in memory.
    """

    result = await session.execute(
        select(ModelCallLog).order_by(ModelCallLog.created_at.desc()).limit(limit)
    )
    rows = list(result.scalars().all())
    now = utcnow()
    cutoff = now - timedelta(days=METRICS_WINDOW_DAYS)

    ok_rows = [r for r in rows if r.status == "ok"]
    latencies = [r.latency_ms for r in ok_rows if r.latency_ms is not None]

    by_status = dict(Counter(r.status for r in rows))
    by_provider = dict(Counter(r.provider for r in rows))
    prompt_tokens = sum(r.prompt_tokens or 0 for r in rows)
    completion_tokens = sum(r.completion_tokens or 0 for r in rows)

    cost_by_provider: dict[str, float] = {}
    for row in rows:
        cost_by_provider[row.provider] = cost_by_provider.get(row.provider, 0.0) + (
            estimate_cost_usd(
                provider=row.provider,
                prompt_tokens=row.prompt_tokens or 0,
                completion_tokens=row.completion_tokens or 0,
            )
        )

    window_rows = [
        r
        for r in rows
        if (created := _ensure_aware(r.created_at)) is not None and created >= cutoff
    ]
    window_cost = sum(
        estimate_cost_usd(
            provider=r.provider,
            prompt_tokens=r.prompt_tokens or 0,
            completion_tokens=r.completion_tokens or 0,
        )
        for r in window_rows
    )
    total_cost = sum(cost_by_provider.values())
    p95 = _percentile(latencies, 95)

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_days": METRICS_WINDOW_DAYS,
        "latency": {
            "count": len(latencies),
            "p50_ms": _percentile(latencies, 50),
            "p95_ms": _percentile(latencies, 95),
            "max_ms": round(max(latencies), 1) if latencies else None,
            "mean_ms": round(statistics.fmean(latencies), 1) if latencies else None,
            "budgets_ms": LATENCY_BUDGETS_MS,
            "within_budget_p95": p95 is not None and p95 <= LATENCY_BUDGETS_MS["chat_first_token"],
        },
        "cost": {
            "total_usd": round(total_cost, 6),
            "last_30d_usd": round(window_cost, 6),
            "monthly_budget_usd": MONTHLY_COST_BUDGET_USD,
            "within_budget": window_cost <= MONTHLY_COST_BUDGET_USD,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "by_provider": {
                provider: round(value, 6) for provider, value in cost_by_provider.items()
            },
        },
        "calls": {
            "total": len(rows),
            "ok": len(ok_rows),
            "errors": len(rows) - len(ok_rows),
            "by_status": by_status,
            "by_provider": by_provider,
        },
    }
