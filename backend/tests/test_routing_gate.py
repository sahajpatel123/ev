"""Tests for the eval-gated model-routing evidence gate."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelCallLog
from app.scripts.routing_gate import run_routing_gate


async def _add_calls(
    session: AsyncSession,
    *,
    ok: int = 0,
    error: int = 0,
    latency_ms: float = 10.0,
) -> None:
    for i in range(ok):
        session.add(
            ModelCallLog(
                request_id=f"gate-ok-{i}",
                actor="gate",
                provider="mock",
                model="mock-model",
                status="ok",
                latency_ms=latency_ms,
                prompt_tokens=10,
                completion_tokens=5,
                envelope={},
            )
        )
    for i in range(error):
        session.add(
            ModelCallLog(
                request_id=f"gate-err-{i}",
                actor="gate",
                provider="mock",
                model="mock-model",
                status="error",
                latency_ms=latency_ms * 20,
                prompt_tokens=10,
                completion_tokens=5,
                envelope={},
            )
        )
    await session.flush()
    await session.commit()


async def test_routing_gate_passes_with_healthy_evidence(db_session: AsyncSession) -> None:
    await _add_calls(db_session, ok=5, latency_ms=25.0)
    result = await run_routing_gate(session=db_session, min_calls=5, max_p95_ms=100.0)
    assert result.passed is True, result.to_dict()
    assert all(check.passed for check in result.checks)


async def test_routing_gate_fails_closed_without_evidence(db_session: AsyncSession) -> None:
    result = await run_routing_gate(session=db_session, min_calls=5)
    assert result.passed is False
    assert result.checks[0].name == "evidence_volume"
    assert result.checks[0].passed is False


async def test_routing_gate_rejects_unhealthy_provider(db_session: AsyncSession) -> None:
    await _add_calls(db_session, ok=4, error=2)
    result = await run_routing_gate(session=db_session, min_calls=5, max_error_rate=0.25)
    assert result.passed is False
    health = next(check for check in result.checks if check.name == "provider_health")
    assert health.passed is False


async def test_routing_gate_rejects_latency_over_budget(db_session: AsyncSession) -> None:
    await _add_calls(db_session, ok=5, latency_ms=2000.0)
    result = await run_routing_gate(session=db_session, min_calls=5, max_p95_ms=1000.0)
    assert result.passed is False
    latency = next(check for check in result.checks if check.name == "latency_budget")
    assert latency.passed is False
