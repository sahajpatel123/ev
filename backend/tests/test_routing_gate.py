"""Tests for the eval-gated model-routing evidence gate."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
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


def _configure_single_brain(monkeypatch) -> None:
    """MiMo configured: routing is a one-brain no-op, evidence still measured."""

    monkeypatch.setattr(settings, "chat_provider", "mimo")
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key")


async def test_routing_gate_healthy_evidence_still_noop_single_provider(
    db_session: AsyncSession, monkeypatch
) -> None:
    _configure_single_brain(monkeypatch)
    await _add_calls(db_session, ok=5, latency_ms=25.0)
    result = await run_routing_gate(session=db_session, min_calls=5, max_p95_ms=100.0)
    assert result.passed is False, result.to_dict()
    by_name = {check.name: check for check in result.checks}
    assert by_name["evidence_volume"].passed is True
    assert by_name["provider_health"].passed is True
    assert by_name["latency_budget"].passed is True
    assert by_name["routing_is_noop"].passed is False


async def test_routing_gate_fails_closed_without_evidence(
    db_session: AsyncSession, monkeypatch
) -> None:
    _configure_single_brain(monkeypatch)
    result = await run_routing_gate(session=db_session, min_calls=5)
    assert result.passed is False
    volume = next(check for check in result.checks if check.name == "evidence_volume")
    assert volume.passed is False


async def test_routing_gate_rejects_unhealthy_provider(
    db_session: AsyncSession, monkeypatch
) -> None:
    _configure_single_brain(monkeypatch)
    await _add_calls(db_session, ok=4, error=2)
    result = await run_routing_gate(session=db_session, min_calls=5, max_error_rate=0.25)
    assert result.passed is False
    health = next(check for check in result.checks if check.name == "provider_health")
    assert health.passed is False


async def test_routing_gate_rejects_latency_over_budget(
    db_session: AsyncSession, monkeypatch
) -> None:
    _configure_single_brain(monkeypatch)
    await _add_calls(db_session, ok=5, latency_ms=2000.0)
    result = await run_routing_gate(session=db_session, min_calls=5, max_p95_ms=1000.0)
    assert result.passed is False
    latency = next(check for check in result.checks if check.name == "latency_budget")
    assert latency.passed is False


async def test_routing_gate_single_provider_is_honest_noop(
    db_session: AsyncSession,
) -> None:
    """With one provider, healthy evidence must NOT produce a meaningless pass."""

    await _add_calls(db_session, ok=5, latency_ms=25.0)
    result = await run_routing_gate(session=db_session, min_calls=5, max_p95_ms=100.0)
    assert result.passed is False
    noop = next(check for check in result.checks if check.name == "routing_is_noop")
    assert noop.passed is False
    assert "no-op" in noop.detail


async def test_routing_gate_cli_path_works_without_injected_session() -> None:
    """The no-session path (used by the CLI) initializes tables and fails closed."""

    result = await run_routing_gate(min_calls=5)
    assert result.passed is False
    assert any(not check.passed for check in result.checks)
