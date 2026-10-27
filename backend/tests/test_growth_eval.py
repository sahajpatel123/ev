"""GE1: eval-suite contract machinery (Wilson, stride, store, adapters, schema)."""

from __future__ import annotations

import json

import pytest

from app.growth.result import finish, new_result, stride, wilson
from app.growth.suites import stubs
from app.growth.suites.unit import parse_junit


def test_wilson_known_values():
    assert wilson(255, 255) == pytest.approx([0.9851, 1.0], abs=1e-3)
    assert wilson(10, 7) == pytest.approx([0.3968, 0.8922], abs=1e-3)
    assert wilson(0, 0) == [0.0, 1.0]


def test_stride_deterministic_and_bounded():
    ids = [f"t{i:04d}" for i in range(100)]
    assert stride(ids, 1.0) == sorted(ids)
    s = stride(ids, 0.1)
    assert len(s) == 10 and s == stride(ids, 0.1)
    assert stride([], 0.5) == []


def test_result_schema_keys():
    r = finish(new_result("unit"), counts=(10, 9, 1, 0, 0))
    for key in ("suite", "version", "status", "commit", "started", "finished",
                "n", "passed", "failed", "skipped", "errors", "pass_rate", "wilson95",
                "categories", "cost_usd", "cost_source", "latency_ms", "safety",
                "failures", "cases_run", "coverage_note", "sample", "budget_usd"):
        assert key in r, key
    assert r["pass_rate"] == 0.9 and r["wilson95"] == wilson(10, 9)


def test_store_roundtrip_tmp(monkeypatch, tmp_path):
    from app.config import settings
    from app.growth import store

    monkeypatch.setattr(settings, "growth_store_dir", str(tmp_path), raising=False)
    r = finish(new_result("unit"), counts=(2, 2, 0, 0, 0))
    assert store.record(r) == 1
    rows = store.recent("unit", 5)
    assert len(rows) == 1 and rows[0]["passed"] == 2
    assert store.recent("nope", 5) == []


def test_stubs_are_honestly_not_built():
    for fn in (stubs.run_scorecard, stubs.run_latency, stubs.run_soak, stubs.run_life):
        r = fn(budget=None, sample=1.0, out="/tmp/ge-stub.json")
        assert r["status"] == "not_built" and r["n"] == 0
        assert r["coverage_note"]


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuite errors="0" failures="1" skipped="1" tests="3" time="1.0">
<testcase classname="tests.test_a" name="test_one" time="0.1"/>
<testcase classname="tests.test_a" name="test_two" time="0.2"><failure message="boom">tb</failure></testcase>
<testcase classname="tests.test_b" name="test_three" time="0.0"><skipped message="x">-</skipped></testcase>
</testsuite>"""


def test_junit_parser(tmp_path):
    p = tmp_path / "r.xml"
    p.write_text(JUNIT)
    out = parse_junit(str(p))
    assert len(out["cases"]) == 3
    assert sum(1 for c in out["cases"] if c["failed"]) == 1
    assert sum(1 for c in out["cases"] if c["skipped"]) == 1


def test_journeys_adapter_runs_green():
    from app.growth.suites import journeys

    r = journeys.run(budget=None, sample=1.0, out="/tmp/ge-j.json")
    assert r["suite"] == "journeys" and r["failed"] == 0, json.dumps(r["failures"])
    assert r["passed"] == 12 and r["skipped"] == 10
    assert r["safety"]["unauthorized_effects"] == 0
