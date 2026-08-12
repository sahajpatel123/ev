"""Tests for aggregate ops metrics: latency percentiles and cost estimates."""

from __future__ import annotations

from uuid import uuid4

from app.models import ModelCallLog
from app.ops.metrics import estimate_cost_usd, record_restore_drill


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


async def test_ops_center_includes_budget_metrics(client, db_session) -> None:
    from uuid import uuid4

    db_session.add(
        ModelCallLog(
            request_id=str(uuid4()),
            actor="master",
            provider="deepseek",
            model="deepseek-v4-flash-0731",
            status="ok",
            latency_ms=150,
            prompt_tokens=1000,
            completion_tokens=200,
            tool_calls=[],
            envelope={},
        )
    )
    await db_session.commit()

    resp = await client.get("/v1/ops/center")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    metrics = body.get("metrics") or {}
    assert metrics.get("latency", {}).get("p95_ms") == 150.0
    assert metrics.get("cost", {}).get("within_budget") is True
    assert metrics.get("cost", {}).get("monthly_budget_usd") == 40.0

    hud = await client.get("/v1/hud/ops")
    assert hud.status_code == 200, hud.text
    meta = hud.json().get("meta", {})
    assert meta.get("latency_p95_ms") == 150.0
    assert meta.get("cost_within_budget") is True


async def test_ops_metrics_reports_per_model_latency(client, db_session) -> None:
    for model, latency in (("deepseek-v4-flash-0731", 100.0), ("deepseek-v4-flash-0731", 300.0), ("mock", 1000.0)):
        db_session.add(
            ModelCallLog(
                request_id=str(uuid4()),
                actor="master",
                provider=model.split("-")[0],
                model=model,
                status="ok",
                latency_ms=latency,
                prompt_tokens=10,
                completion_tokens=10,
                tool_calls=[],
                envelope={},
            )
        )
    await db_session.commit()

    body = (await client.get("/v1/ops/metrics")).json()
    by_model = body["calls"]["latency_by_model"]
    assert by_model["deepseek-v4-flash-0731"]["p95_ms"] == 300.0
    assert by_model["deepseek-v4-flash-0731"]["count"] == 2
    assert by_model["mock"]["p95_ms"] == 1000.0


async def test_ops_metrics_reports_system_and_arbiter(client) -> None:
    body = (await client.get("/v1/ops/metrics")).json()
    system = body["system"]
    assert "free_ram_mb" in system
    assert isinstance(system["free_disk_gb"], float)
    assert "swap" in system
    arbiter = body["arbiter"]
    assert arbiter["available"] is True
    assert isinstance(arbiter["resident_mb"], dict)
    assert isinstance(arbiter["evictions_observed"], int)
    assert isinstance(arbiter["refusals_last_50"], list)


async def test_restore_drill_age_alert_past_35_days(client) -> None:
    from datetime import timedelta
    from pathlib import Path

    from app.config import settings
    from app.utils.text import utcnow

    marker = Path(settings.storage_root) / "ops" / "restore-drill.json"
    old = (utcnow() - timedelta(days=40)).isoformat(timespec="seconds")
    marker.write_text(f'{{"last_success_at": "{old}"}}', encoding="utf-8")

    body = (await client.get("/v1/ops/metrics")).json()
    assert body["restore_drill"]["stale"] is True
    assert body["restore_drill"]["alert"]

    record_restore_drill()
    body = (await client.get("/v1/ops/metrics")).json()
    assert body["restore_drill"]["stale"] is False
    assert body["restore_drill"]["age_days"] == 0.0
    assert not any("restore drill" in warning.lower() for warning in body["warnings"])

    marker.write_text(f'{{"last_success_at": "{old}"}}', encoding="utf-8")
    body = (await client.get("/v1/ops/metrics")).json()
    assert body["restore_drill"]["stale"] is True
    assert body["restore_drill"]["age_days"] > 35
    assert any("restore drill" in warning.lower() for warning in body["warnings"])
