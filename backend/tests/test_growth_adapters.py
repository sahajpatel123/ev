"""Proof tests for the GE1/GE2 review fixes: adapter shapes, key separation,
budget enforcement, exit codes. Every test here fails on the pre-fix code."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


# --- gauntlet: failures are data, not dropped (review #1) ---

def test_gauntlet_scripted_parse_counts_failing_case():
    from app.growth.suites.gauntlet import parse_scripted

    p = parse_scripted({
        "n": 10, "real_brain_only": 2, "pass_at_1": 75.0,
        "failed": [{"id": "g1", "difficulty": "hard", "problems": ["boom"]}],
        "ran_ids": ["g1", "g2"],
        "by_difficulty": {"hard": {"n": 8, "plumbing_pass_at_1": 87.5}},
        "unauthorized": 0, "false_success": 1}, sample=1.0)
    assert (p["n"], p["passed"], p["failed"], p["skipped"]) == (10, 7, 1, 2)
    assert p["failures"][0]["id"] == "g1" and "gauntlet" in p["failures"][0]["repro"]
    assert p["cases_run"] == ["g1", "g2"] and p["false_success"] == 1


def test_gauntlet_real_parse_propagates_stage1_and_cost():
    from app.growth.suites.gauntlet import parse_real

    assert parse_real({"ready": False, "detail": "no key"})["ready"] is False
    p = parse_real({
        "ready": True, "spent_usd": 0.5, "cost_source": "provider_reported",
        "stage1_ran": 4, "stage1_passed": 3, "stage1_eligible": 5,
        "stage1_false_success": 1, "stage1_unauthorized": 0,
        "stage1_by_difficulty": {"hard": {"n": 4, "passed": 3}},
        "stage1_failures": [{"id": "h1", "problems": ["x"]}],
        "stage1_ran_ids": ["h1", "h2", "h3", "h4"]})
    assert p["ready"] and (p["n"], p["passed"], p["failed"], p["skipped"]) == (4, 3, 1, 1)
    assert (p["cost_usd"], p["cost_source"], p["false_success"]) == (0.5, "provider_reported", 1)
    assert len(p["cases_run"]) == 4


def test_gauntlet_scripted_json_e2e_has_failed_and_ran_ids():
    cmd = [sys.executable, "-m", "app.scripts.gauntlet", "--split", "dev",
           "--stride", "16", "--stride-index", "0", "--json"]
    p = subprocess.run(cmd, cwd=str(BACKEND), capture_output=True, text=True, timeout=900)
    summary = json.loads(p.stdout.strip().splitlines()[-1])
    assert "failed" in summary and isinstance(summary["failed"], list)
    assert summary["ran_ids"], "must record the exact cases run"
    assert summary["ran_ids"] == sorted(summary["ran_ids"]), "stride order must be deterministic"


def test_gauntlet_real_sample_blocked_honestly():
    from app.growth.suites.gauntlet import run

    r = run(budget=1.0, sample=0.5, out="/tmp/ge-g.json")
    assert r["status"] == "blocked" and "no subset mode" in r["coverage_note"]


# --- gates: skips are skips (review #5) ---

def test_gates_parse_counts_skipped_gates_as_skipped():
    from app.growth.suites.gates import parse_gates

    p = parse_gates([
        {"name": "g1", "passed": True,
         "checks": [{"name": "a", "passed": True}, {"name": "b", "passed": False, "detail": "x"}]},
        {"name": "g2", "passed": True, "skipped": True, "skip_reason": "no weights",
         "checks": [{"name": "c", "passed": True}]}], sample=1.0)
    assert (p["n"], p["passed"], p["failed"], p["skipped"]) == (2, 1, 1, 1)
    assert "no weights" in " ".join(p["skip_reasons"])
    assert p["failures"][0]["id"] == "g1/b"


# --- unit: node ids, errors, scrub (review #9, #20, #25) ---

JUNIT = """<testsuite><testcase classname="tests.test_a" name="test_one" time="0.1"/>
<testcase classname="tests.test_a" name="test_two" time="0.2"><failure message="assert EV_FOO_KEY=sekret-123">x</failure></testcase>
<testcase classname="tests.test_b.TestC" name="test_three" time="0.3"><error message="boom">y</error></testcase>
<testcase classname="tests.test_b" name="test_four" time="0"><skipped/></testcase></testsuite>"""


def test_unit_parse_maps_nodes_counts_errors_scrubs(tmp_path):
    from app.growth.suites.unit import _node_index, parse_junit

    index = _node_index(["tests/test_a.py::test_one", "tests/test_a.py::test_two",
                         "tests/test_b.py::TestC::test_three", "tests/test_b.py::test_four"])
    f = tmp_path / "r.xml"
    f.write_text(JUNIT)
    cases = parse_junit(str(f), index)["cases"]
    by_id = {c["id"]: c for c in cases}
    assert by_id["tests/test_a.py::test_two"]["failed"] is True
    assert "sekret-123" not in by_id["tests/test_a.py::test_two"]["detail"]
    assert "<redacted>" in by_id["tests/test_a.py::test_two"]["detail"]
    assert by_id["tests/test_b.py::TestC::test_three"]["errored"] is True
    assert by_id["tests/test_b.py::test_four"]["skipped"] is True
    assert by_id["tests/test_a.py::test_one"]["file"] == "tests/test_a.py"


# --- keys: separation holds (review #3, #15, #27) ---

def test_suite_env_strips_creds_routes_eval_sets_hatch(monkeypatch):
    from app.growth import keys as K

    monkeypatch.setenv("EV_MASTER_KEY", "live-master")
    monkeypatch.setenv("EV_GOOGLE_API_KEY", "live-google")
    monkeypatch.setenv("OPENROUTER_API_KEY", "live-unprefixed")
    monkeypatch.setenv("SENTRY_DSN", "live-dsn")
    env = K.suite_env(eval_key="eval-KEY", extra={"EV_VAULT_KEY": "sneaky"})
    for v in ("live-master", "live-google", "live-unprefixed", "live-dsn", "sneaky"):
        assert v not in env.values()
    assert env["EV_OPENROUTER_API_KEY"] == "eval-KEY"
    assert env["EV_SECRETS_FILE"] == K.NO_SECRETS_FILE


def test_real_brain_prefers_env_key_over_files(monkeypatch, tmp_path):
    decoy = tmp_path / "decoy.env"
    decoy.write_text("EV_OPENROUTER_API_KEY=decoy-KEY-000\n")
    monkeypatch.setenv("EV_SECRETS_FILE", str(decoy))
    monkeypatch.setenv("EV_OPENROUTER_API_KEY", "eval-Routed-Key-1")
    monkeypatch.setenv("EV_CHAT_PROVIDER", "muse")
    from gauntlet.real_brain import load_provider_status

    ready, detail = load_provider_status()
    assert ready, detail
    assert f"({len('eval-Routed-Key-1')} chars)" in detail, detail


# --- budget: caps enforced, exit codes (review #4, #16, #24) ---

def test_room_usd_clamps_and_zero_cap_binds(monkeypatch):
    import app.growth.budget as B

    monkeypatch.setattr(B, "caps", lambda: (1.0, 20.0))
    monkeypatch.setattr(B, "spent", lambda d, m: (0.0, 0.0))
    assert B.room_usd(5.0) == 1.0 and B.room_usd(0.5) == 0.5 and B.room_usd(None) == 1.0
    monkeypatch.setattr(B, "caps", lambda: (0.0, 20.0))
    assert B.room_usd(5.0) == 0.0, "explicit zero cap must bind, not reset to default"


def test_main_clamps_budget_to_room(monkeypatch, tmp_path):
    import app.growth.budget as B
    import app.growth.run as R

    monkeypatch.setattr(B, "caps", lambda: (1.0, 20.0))
    monkeypatch.setattr(B, "spent", lambda d, m: (0.0, 0.0))
    monkeypatch.setattr(R, "load_eval_key", lambda: "test-key")
    monkeypatch.setattr(R, "record", lambda res: 1)
    monkeypatch.setattr(R, "record_spend", lambda s, c: None)
    out = str(tmp_path / "r.json")
    rc = R.main(["latency", "--budget", "5", "--out", out])
    assert rc == 2  # not_built stub still exits 2
    assert json.loads(Path(out).read_text())["budget_usd"] == 1.0


def test_main_rejects_negative_budget(tmp_path):
    import app.growth.run as R

    assert R.main(["unit", "--budget", "-1", "--out", str(tmp_path / "r.json")]) == 2


def test_main_blocked_without_eval_key(monkeypatch, tmp_path):
    import app.growth.run as R

    monkeypatch.setattr(R, "load_eval_key", lambda: "")
    out = str(tmp_path / "r.json")
    assert R.main(["gauntlet", "--budget", "1", "--out", out]) == 2
    assert json.loads(Path(out).read_text())["status"] == "blocked"


# --- journeys: scratch audit, derived safety (review #12, #13) ---

def test_journeys_writes_no_prod_audit_and_derives_safety():
    from app.growth.suites import journeys

    prod = Path.home() / ".ev" / "fabric_audit.jsonl"
    before = (prod.stat().st_mtime_ns, prod.stat().st_size) if prod.is_file() else None
    r = journeys.run(budget=None, sample=1.0, out="/tmp/ge-j2.json")
    after = (prod.stat().st_mtime_ns, prod.stat().st_size) if prod.is_file() else None
    assert before == after, "suite must not touch the production audit log"
    assert r["safety"]["false_success"] is None
    assert r["safety"]["unauthorized_effects"] == 0
    assert not any("(skipped)" in c for c in r["cases_run"])


# --- cug: snapshot/restore, never checkout (review #14) ---

def test_cug_snapshot_restore_preserves_owner_bytes(monkeypatch, tmp_path):
    from app.growth.suites import cug

    monkeypatch.setattr(cug, "CUGDIR", tmp_path)
    (tmp_path / "sim.json").write_text('[{"owner": true}]')
    before = cug.snapshot()
    (tmp_path / "sim.json").write_text('[{"run": true}]')
    (tmp_path / "live.json").write_text('[{"run": true}]')
    cug.restore(before)
    assert (tmp_path / "sim.json").read_text() == '[{"owner": true}]'
    assert not (tmp_path / "live.json").exists()


# --- gesture: no phantom cases (review #10) ---

def test_gesture_missing_replay_line_blocks(monkeypatch, tmp_path):
    from app.growth.suites import gesture

    tool = tmp_path / "PresenceChecks"
    tool.write_text("x")
    monkeypatch.setattr(gesture, "TOOL", tool)
    monkeypatch.setattr(gesture.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "presence-checks passed=9 failed=0\n"))
    r = gesture.run(budget=None, sample=1.0, out="/tmp/ge-gg.json")
    assert r["status"] == "blocked"


def test_gesture_counts_binary_verdict_without_phantom(monkeypatch, tmp_path):
    from app.growth.suites import gesture

    tool = tmp_path / "PresenceChecks"
    tool.write_text("x")
    monkeypatch.setattr(gesture, "TOOL", tool)
    out = ("presence-checks passed=9 failed=1\nFAIL replay-zero-activations activations=3\n"
           "INFO replay events=10000 chordBlocks=200 taps=5 bounces=0 activations=3\n")
    monkeypatch.setattr(gesture.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, out))
    r = gesture.run(budget=None, sample=1.0, out="/tmp/ge-gg.json")
    assert (r["n"], r["passed"], r["failed"]) == (10, 9, 1)
    assert "activations=3" in r["coverage_note"]


# --- orb: steady-state oracle (review #11) ---

def test_orb_verdict_uses_steady_not_raw_max():
    from app.growth.suites.orb import verdict_row

    warmup_spike = {"state": "thinking", "framesRendered": 600, "processCPUPct": 1.2,
                    "frameGPUMaxMs": 9.9, "frameGPUSteadyMaxMs": 0.8, "gpuSpikesOverBudget": 0}
    assert verdict_row(warmup_spike)[0] is True
    spiky = dict(warmup_spike, gpuSpikesOverBudget=2)
    assert verdict_row(spiky)[0] is False
    static_frames = {"state": "idle", "framesRendered": 3, "processCPUPct": 0.1,
                     "frameGPUSteadyMaxMs": -1, "gpuSpikesOverBudget": 0}
    assert verdict_row(static_frames)[0] is False
    static_clean = dict(static_frames, framesRendered=0)
    assert verdict_row(static_clean)[0] is True
