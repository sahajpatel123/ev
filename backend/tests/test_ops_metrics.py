"""Tests for aggregate ops metrics: latency percentiles and cost estimates."""

from __future__ import annotations

from uuid import uuid4

from app.models import ModelCallLog
from app.ops.metrics import estimate_cost_usd


def test_estimate_cost_usd_uses_provider_pricing() -> None:
    cost = estimate_cost_usd(
        provider="deepseek",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert cost == round(0.27 + 1.10, 6)


def test_estimate_cost_usd_defaults_unknown_provider() -> None:
    cost = estimate_cost_usd(provider="unknown", prompt_tokens=1_000_000, completion_tokens=0)
    assert cost == round(1.00, 6)


async def test_ops_metrics_aggregates_calls_latency_and_cost(
    client,
    db_session,
) -> None:
    rows = [
        ModelCallLog(
            request_id=str(uuid4()),
            actor="master",
            provider="deepseek",
            model="deepseek-v4-flash-0731",
            status="ok",
            latency_ms=120,
            prompt_tokens=1000,
            completion_tokens=200,
            tool_calls=[],
            envelope={},
        ),
        ModelCallLog(
            request_id=str(uuid4()),
            actor="master",
            provider="deepseek",
            model="deepseek-v4-flash-0731",
            status="ok",
            latency_ms=300,
            prompt_tokens=500,
            completion_tokens=100,
            tool_calls=[],
            envelope={},
        ),
        ModelCallLog(
            request_id=str(uuid4()),
            actor="master",
            provider="mock",
            model="mock",
            status="error",
            latency_ms=9000,
            prompt_tokens=0,
            completion_tokens=0,
            tool_calls=[],
            envelope={},
            error="provider timeout",
        ),
    ]
    for row in rows:
        db_session.add(row)
    await db_session.commit()

    resp = await client.get("/v1/ops/metrics")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["calls"]["total"] == 3
    assert body["calls"]["ok"] == 2
    assert body["calls"]["errors"] == 1
    assert body["calls"]["by_status"] == {"ok": 2, "error": 1}
    assert body["calls"]["by_provider"] == {"deepseek": 2, "mock": 1}

    assert body["latency"]["count"] == 2
    assert body["latency"]["p50_ms"] == 120.0
    assert body["latency"]["p95_ms"] == 300.0
    assert body["latency"]["max_ms"] == 300.0
    assert body["latency"]["within_budget_p95"] is True

    assert body["cost"]["prompt_tokens"] == 1500
    assert body["cost"]["completion_tokens"] == 300
    assert body["cost"]["by_provider"]["deepseek"] == round(
        (1500 * 0.27 + 300 * 1.10) / 1_000_000, 6
    )
    assert body["cost"]["within_budget"] is True
    assert body["cost"]["monthly_budget_usd"] == 40.0
