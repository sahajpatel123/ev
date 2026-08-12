"""Tests for the ops evaluation gate harness (`make eval`)."""

from __future__ import annotations

import json

import pytest

from app.main import app
from app.scripts.eval_gates import (
    Check,
    GateResult,
    build_report,
    run_api_contract_gate,
    run_asr_quality_gate,
    run_ci_parity_gate,
    run_deployment_gate,
    run_face_recognition_gate,
    run_filter_gate,
    run_grounding_gate,
    run_observability_gate,
    run_regression_gate,
    run_retrieval_quality_gate,
    run_roadmap_gate,
    run_speaker_security_gate,
    run_training_gate,
    run_voice_gate,
    run_wake_reliability_gate,
)


def _spec() -> dict:
    return app.openapi()


def test_api_contract_gate_passes() -> None:
    result = run_api_contract_gate(_spec())
    assert result.passed, result.to_dict()


def test_filter_gate_passes() -> None:
    result = run_filter_gate()
    assert result.passed, result.to_dict()


async def test_voice_gate_passes() -> None:
    result = await run_voice_gate(_spec())
    assert result.passed, result.to_dict()


async def test_training_gate_passes(db_session) -> None:
    result = await run_training_gate(_spec(), db_session)
    assert result.passed, result.to_dict()


def test_observability_gate_passes() -> None:
    result = run_observability_gate(_spec())
    assert result.passed, result.to_dict()


def test_deployment_gate_passes() -> None:
    result = run_deployment_gate()
    assert result.passed, result.to_dict()


def test_ci_parity_gate_passes() -> None:
    result = run_ci_parity_gate()
    assert result.passed, result.to_dict()


def test_ci_parity_gate_detects_drift(tmp_path) -> None:
    broken = tmp_path / "ci.yml"
    broken.write_text(
        "name: ci\non: push\njobs:\n  verify:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo 'nothing runs the real checks'\n",
        encoding="utf-8",
    )
    result = run_ci_parity_gate(broken)
    assert not result.passed
    assert not any(c.passed for c in result.checks)


def test_regression_gate_passes_without_baseline() -> None:
    result = run_regression_gate(None, {"latency_chat_first_token_ms": 50.0})
    assert result.passed, result.to_dict()


def test_regression_gate_detects_latency_increase() -> None:
    result = run_regression_gate(
        {"latency_chat_first_token_ms": 100.0},
        {"latency_chat_first_token_ms": 150.0},
    )
    assert not result.passed
    assert not result.checks[0].passed
    assert "50.0%" in result.checks[0].detail


def test_regression_gate_accepts_small_latency_delta() -> None:
    result = run_regression_gate(
        {"latency_chat_first_token_ms": 100.0},
        {"latency_chat_first_token_ms": 105.0},
    )
    assert result.passed, result.to_dict()


def test_regression_gate_ignores_noise_below_absolute_floor() -> None:
    # 43% on a 17ms baseline with <10ms absolute growth is load jitter, not a
    # real regression: the gate requires >10% AND >10ms absolute growth.
    result = run_regression_gate(
        {"latency_chat_first_token_ms": 17.2},
        {"latency_chat_first_token_ms": 24.7},
    )
    assert result.passed, result.to_dict()


def test_regression_gate_detects_large_absolute_increase() -> None:
    result = run_regression_gate(
        {"latency_chat_first_token_ms": 100.0},
        {"latency_chat_first_token_ms": 200.0},
    )
    assert not result.passed
    assert "100.0%" in result.checks[0].detail


def test_regression_gate_accepts_full_stack_load_jitter() -> None:
    # 12ms of jitter on a 28ms in-process median (native stack running on an
    # 8GB Mac) must not fail the gate: the absolute floor is 25ms.
    result = run_regression_gate(
        {"latency_chat_first_token_ms": 16.5},
        {"latency_chat_first_token_ms": 28.7},
    )
    assert result.passed, result.to_dict()


def test_regression_gate_detects_rank_regression() -> None:
    result = run_regression_gate(
        {"retrieval_target_rank": 1},
        {"retrieval_target_rank": 3},
    )
    assert not result.passed
    assert "cur_rank=3" in result.checks[0].detail


async def test_latency_gate_passes(client) -> None:
    from app.scripts.eval_gates import run_latency_gate

    result = await run_latency_gate()
    assert result.passed, result.to_dict()


async def test_restore_drill_gate_passes(client) -> None:
    from app.scripts.eval_gates import run_restore_gate

    result = await run_restore_gate()
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


def test_report_counts_skipped_gates() -> None:
    gates = [
        GateResult(
            name="asr_quality",
            passed=True,
            skipped=True,
            skip_reason="no eval artifact",
        )
    ]
    report = build_report(gates)
    assert report["summary"]["passed"] == 1
    assert report["summary"]["skipped"] == 1
    assert report["gates"][0]["skipped"] is True
@pytest.mark.parametrize(
    ("env_var", "gate_fn"),
    [
        ("EV_ASR_EVAL_REPORT", run_asr_quality_gate),
        ("EV_SPEAKER_EVAL_REPORT", run_speaker_security_gate),
        ("EV_RETRIEVAL_EVAL_REPORT", run_retrieval_quality_gate),
        ("EV_FACE_EVAL_REPORT", run_face_recognition_gate),
        ("EV_WAKE_EVAL_REPORT", run_wake_reliability_gate),
    ],
)
def test_ml_gates_skip_without_artifacts(
    monkeypatch,
    tmp_path,
    env_var: str,
    gate_fn,
) -> None:
    monkeypatch.setenv(env_var, str(tmp_path / "missing.json"))
    result = gate_fn()
    assert result.skipped, result.to_dict()
    assert "no eval artifact" in result.skip_reason
    assert result.passed


def test_ml_gate_fails_when_threshold_missed(monkeypatch, tmp_path) -> None:
    path = tmp_path / "asr.json"
    path.write_text(
        json.dumps(
            {
                "provider": "parakeet-eou-120m",
                "degraded": False,
                "wer_clean": 0.20,
                "wer_owner_speech": 0.20,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EV_ASR_EVAL_REPORT", str(path))
    result = run_asr_quality_gate()
    assert not result.passed, result.to_dict()
    assert any(c.name == "wer_clean_within_budget" and not c.passed for c in result.checks)


def test_ml_gate_skips_degraded_artifact(monkeypatch, tmp_path) -> None:
    path = tmp_path / "asr.json"
    path.write_text(
        json.dumps(
            {
                "provider": "parakeet-eou-120m",
                "degraded": True,
                "wer_clean": 0.0,
                "wer_owner_speech": 0.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EV_ASR_EVAL_REPORT", str(path))
    result = run_asr_quality_gate()
    assert result.skipped, result.to_dict()
    assert "degraded" in result.skip_reason


def test_asr_gate_skips_measured_false_artifact(monkeypatch, tmp_path) -> None:
    path = tmp_path / "asr.json"
    path.write_text(
        json.dumps(
            {
                "provider": "parakeet-eou-120m",
                "measured": False,
                "error": "provider unavailable",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EV_ASR_EVAL_REPORT", str(path))
    result = run_asr_quality_gate()
    assert result.skipped, result.to_dict()
    assert "measured=false" in result.skip_reason


def test_asr_gate_accepts_agent4_wer_mean_artifact(monkeypatch, tmp_path) -> None:
    path = tmp_path / "asr.json"
    path.write_text(
        json.dumps(
            {
                "provider": "parakeet-eou-120m",
                "measured": True,
                "wer_mean": 0.07,
                "samples": 10,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EV_ASR_EVAL_REPORT", str(path))
    result = run_asr_quality_gate()
    assert result.passed, result.to_dict()
    assert result.metrics["asr_wer_clean"] == 0.07
    assert any(c.name == "wer_owner_speech_not_measured" for c in result.checks)


def test_ml_gate_fails_malformed_artifact(monkeypatch, tmp_path) -> None:
    path = tmp_path / "asr.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("EV_ASR_EVAL_REPORT", str(path))
    result = run_asr_quality_gate()
    assert not result.passed
    assert not result.checks[0].passed


def test_speaker_gate_derives_zero_false_accepts_from_roc(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "speaker.json"
    path.write_text(
        json.dumps(
            {
                "algorithm": "campp",
                "degraded": False,
                "eer": 0.02,
                "threshold": 0.85,
                "owner_count": 50,
                "impostor_count": 100,
                "roc": [
                    [0.0, 0.5, 0.7],
                    [0.0, 0.8, 0.8],
                    [0.01, 1.0, 0.9],
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EV_SPEAKER_EVAL_REPORT", str(path))
    result = run_speaker_security_gate()
    assert result.passed, result.to_dict()
    assert result.metrics["speaker_far_at_threshold"] == 0.0


def test_speaker_gate_fails_when_far_at_threshold_nonzero(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "speaker.json"
    path.write_text(
        json.dumps(
            {
                "algorithm": "campp",
                "degraded": False,
                "eer": 0.02,
                "far_at_threshold": 0.01,
                "impostor_count": 100,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EV_SPEAKER_EVAL_REPORT", str(path))
    result = run_speaker_security_gate()
    assert not result.passed
    assert any(c.name == "zero_false_accepts_at_threshold" and not c.passed for c in result.checks)


def test_retrieval_gate_reads_ev_eval_report(monkeypatch, tmp_path) -> None:
    path = tmp_path / "retrieval.json"
    path.write_text(
        json.dumps(
            {
                "before_after": {
                    "provider": {
                        "provider": "granite-r2",
                        "degraded": False,
                        "ndcg_at_10": 0.85,
                        "top5_hit_rate": 0.95,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EV_RETRIEVAL_EVAL_REPORT", str(path))
    result = run_retrieval_quality_gate()
    assert result.passed, result.to_dict()
    assert result.metrics["retrieval_ndcg_at_10"] == 0.85


def test_face_gate_requires_complete_stranger_rejection(monkeypatch, tmp_path) -> None:
    path = tmp_path / "face.json"
    path.write_text(
        json.dumps(
            {
                "provider": "sface",
                "degraded": False,
                "tar_held_out": 0.97,
                "far_held_out": 0.001,
                "strangers_total": 50,
                "strangers_unknown": 49,
                "stranger_rejection_rate": 0.98,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EV_FACE_EVAL_REPORT", str(path))
    result = run_face_recognition_gate()
    assert not result.passed
    assert any(
        c.name == "stranger_rejection_complete" and not c.passed for c in result.checks
    )


def test_wake_gate_fails_on_excessive_false_accepts(monkeypatch, tmp_path) -> None:
    path = tmp_path / "wake.json"
    path.write_text(
        json.dumps(
            {
                "provider": "openwakeword",
                "degraded": False,
                "false_accepts_per_12h": 3.0,
                "recall": 0.95,
                "hours_audio": 12.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EV_WAKE_EVAL_REPORT", str(path))
    result = run_wake_reliability_gate()
    assert not result.passed
    assert any(
        c.name == "false_accepts_within_budget" and not c.passed for c in result.checks
    )


async def test_grounding_gate_measures_corpus() -> None:
    result = await run_grounding_gate()
    assert result.passed, result.to_dict()
    assert (result.metrics.get("grounding_recall") or 0.0) >= 0.95
    false_removal = result.metrics.get("grounding_false_removal_rate")
    assert false_removal is not None and false_removal <= 0.05


def test_regression_gate_detects_higher_is_better_ml_regression() -> None:
    result = run_regression_gate(
        {"retrieval_ndcg_at_10": 0.90},
        {"retrieval_ndcg_at_10": 0.82},
    )
    assert not result.passed
    assert "tol=0.01" in result.checks[0].detail


def test_regression_gate_detects_lower_is_better_ml_regression() -> None:
    result = run_regression_gate(
        {"speaker_eer": 0.02},
        {"speaker_eer": 0.04},
    )
    assert not result.passed


def test_regression_gate_accepts_ml_metric_improvement() -> None:
    result = run_regression_gate(
        {"asr_wer_clean": 0.08},
        {"asr_wer_clean": 0.06},
    )
    assert result.passed, result.to_dict()
