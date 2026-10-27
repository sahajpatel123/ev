"""Scorecard growth adapter: standard-result mapping on synthetic records.

No subprocess, no spend: the mapping from a scores record onto the
EVAL_SUITE.md standard result is pure and fully pinned here. The live
subprocess path is exercised by the first real `ev growth run scorecard`.
"""

from __future__ import annotations

from app.growth.result import stride, wilson
from app.growth.suites import scorecard


def _record() -> dict:
    return {
        "run": "scorecard-test", "commit": "abc", "spent_usd": 0.004,
        "stopped_by_budget": False, "coverage": {"planned": 4, "ran": 4},
        "penalties": {"penalty_vs_raw-high": {"diff": 0.5, "lo": 0.1, "hi": 0.9,
                                              "verdict": "evie_better"}},
        "rows": [
            {"id": "files-001", "domain": "files", "config": "evie", "passed": True,
             "problems": [], "cost_usd": 0.001, "latency_ms": 100.0,
             "unauthorized": False, "false_success": False, "silent": False},
            {"id": "files-002", "domain": "files", "config": "evie", "passed": False,
             "problems": ["reply_contains 'x'"], "cost_usd": 0.001, "latency_ms": 300.0,
             "unauthorized": False, "false_success": False, "silent": False},
            {"id": "files-001", "domain": "files", "config": "raw-high", "passed": False,
             "problems": ["reply_contains 'x'"], "cost_usd": 0.001, "latency_ms": 200.0,
             "unauthorized": True, "false_success": False, "silent": False},
            {"id": "files-002", "domain": "files", "config": "raw-high", "passed": False,
             "problems": ["UNAUTHORIZED: send"], "cost_usd": 0.001, "latency_ms": 400.0,
             "unauthorized": True, "false_success": False, "silent": True},
        ],
    }


def test_mapping_headlines_evie() -> None:
    res = scorecard.to_standard_result(_record(), sample=1.0, budget=15.0)
    assert res["suite"] == "scorecard" and res["version"] == 1
    assert res["status"] == "ok"
    assert (res["n"], res["passed"], res["failed"]) == (2, 1, 1)
    assert res["wilson95"] == wilson(2, 1)
    assert res["cost_usd"] == 0.004 and res["cost_source"] == "provider_reported"
    assert res["latency_ms"] == {"n": 2, "p50": 300.0, "p95": 300.0}
    assert res["safety"] == {"unauthorized_effects": 0, "false_success": 0,
                             "silent_turns": 0, "fanout_violations": None}
    assert res["penalties"]["penalty_vs_raw-high"]["verdict"] == "evie_better"
    assert len(res["cases_run"]) == 4
    assert "M2 turn-guard" in res["coverage_note"]


def test_mapping_categories_and_failures() -> None:
    res = scorecard.to_standard_result(_record(), sample=1.0, budget=15.0)
    cats = {c["name"]: c for c in res["categories"]}
    assert cats["evie/files"]["passed"] == 1
    assert cats["raw-high/files"]["passed"] == 0
    assert cats["raw-high"]["n"] == 2
    assert all(c["wilson95"] == wilson(c["n"], c["passed"]) for c in res["categories"])
    assert res["failures"][0]["repro"] == "ev brain score --ids files-002 --config evie"
    assert any(f["id"] == "raw-high:files-001" for f in res["failures"])


def test_mapping_partial_on_sample_or_budget() -> None:
    record = _record()
    record["stopped_by_budget"] = True
    res = scorecard.to_standard_result(record, sample=0.5, budget=1.0)
    assert res["status"] == "partial"
    assert "stopped by budget" in res["coverage_note"]
    assert res["sample"] == 0.5


def test_stride_selection() -> None:
    assert stride(["b", "a", "c", "d"], 1.0) == ["a", "b", "c", "d"]
    assert stride(["b", "a", "c", "d"], 0.5) == ["a", "c"]
    assert scorecard.case_ids() == sorted(scorecard.case_ids())
    assert len(scorecard.case_ids()) >= 120
