"""Tests for the ops evaluation gate harness (`make eval`)."""

from __future__ import annotations

import json

from app.main import app
from app.scripts.eval_gates import (
    Check,
    GateResult,
    build_report,
    run_api_contract_gate,
    run_filter_gate,
    run_observability_gate,
    run_roadmap_gate,
    run_voice_gate,
)


def _spec() -> dict:
    return app.openapi()


def test_api_contract_gate_passes() -> None:
    result = run_api_contract_gate(_spec())
    assert result.passed, result.to_dict()


def test_filter_gate_passes() -> None:
    result = run_filter_gate()
    assert result.passed, result.to_dict()


def test_voice_gate_passes() -> None:
    result = run_voice_gate(_spec())
    assert result.passed, result.to_dict()


def test_observability_gate_passes() -> None:
    result = run_observability_gate(_spec())
    assert result.passed, result.to_dict()


async def test_latency_gate_passes(client) -> None:
    from app.scripts.eval_gates import run_latency_gate

    result = await run_latency_gate()
    assert result.passed, result.to_dict()


def test_roadmap_gate_passes() -> None:
    result = run_roadmap_gate(_spec())
    assert result.passed, result.to_dict()


async def test_retrieval_gate_passes(db_session) -> None:
    from app.scripts.eval_gates import run_retrieval_gate

    result = await run_retrieval_gate(db_session)
    assert result.passed, result.to_dict()


def test_report_is_json_serializable() -> None:
    gates = [
        GateResult(
            name="api_contract",
            passed=True,
            checks=[Check(name="x", passed=True)],
        )
    ]
    report = build_report(gates)
    payload = json.dumps(report)
    assert '"schema_version": "ev.ops.gates.v1"' in payload
    assert report["summary"]["passed"] == 1
    assert report["summary"]["total"] == 1
