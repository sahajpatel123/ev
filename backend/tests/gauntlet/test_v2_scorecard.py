"""Tests for the v2 scorecard harness additions (no brain, no spend).

Covers: synthetic thread setup, ambiguity branch grading, and the spoken
style oracles. The brain never sees these cases here; each test drives the
harness directly with synthetic runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gauntlet.v2 import corpus, fixtures, oracles
from gauntlet.v2.cases._kit import case, f, thread
from gauntlet.v2.runner import grade
from gauntlet.v2.world import World


def _run(*replies: str, tasks: list[dict] | None = None) -> dict[str, Any]:
    return {"replies": [{"reply": r} for r in replies], "tasks": tasks or [],
            "confirmations": [], "trace": [], "tripwire": [], "errors": []}


def _home() -> Path:
    return Path("/nonexistent-home")


# ------------------------------------------------------- synthetic threads


def test_make_thread_turns_anchor_first_and_deterministic() -> None:
    a = fixtures.make_thread_turns(50, seed=3, anchor_owner="Q-ANCHOR", anchor_evie="A-ANCHOR")
    b = fixtures.make_thread_turns(50, seed=3, anchor_owner="Q-ANCHOR", anchor_evie="A-ANCHOR")
    assert a == b
    assert len(a) == 50
    assert a[0] == {"owner": "Q-ANCHOR", "evie": "A-ANCHOR", "kind": "typed", "at": a[0]["at"]}
    markers = [t["owner"] for t in a[1:]]
    assert len(set(markers)) == 49  # every filler turn uniquely marked
    assert all("Q-ANCHOR" not in t["owner"] and "A-ANCHOR" not in t["evie"] for t in a[1:])


def test_make_thread_turns_rejects_bad_args() -> None:
    import pytest

    with pytest.raises(ValueError):
        fixtures.make_thread_turns(0, anchor_owner="q", anchor_evie="a")
    with pytest.raises(ValueError):
        fixtures.make_thread_turns(5, anchor_owner="q", anchor_evie="a", anchor_index=5)


def test_thread_setup_writes_loadable_session() -> None:
    world = World("thread-setup-test")
    try:
        world.apply_setup([thread(25, "Q-ANCHOR", "A-ANCHOR", seed=7)])
        session_path = world.root / "storage" / "cognitive" / "session.json"
        assert session_path.is_file()
        raw = json.loads(session_path.read_text(encoding="utf-8"))
        assert raw["session_id"].startswith("gauntlet-")
        assert len(raw["recent_turns"]) == 25
        assert raw["recent_turns"][0]["owner"] == "Q-ANCHOR"
    finally:
        world.destroy()


def test_thread_setup_matches_session_store_shape(monkeypatch) -> None:
    from app.cognitive import session_store
    from app.config import settings

    world = World("thread-store-shape")
    monkeypatch.setattr(settings, "storage_root", str(world.root / "storage"))
    try:
        world.apply_setup([thread(10, "Q", "A")])
        session_store.forget_live_cache()
        loaded = session_store._load_file()
        assert loaded is not None
        assert len(loaded.recent_turns) == 10
        assert loaded.recent_turns[0]["owner"] == "Q"
    finally:
        session_store.forget_live_cache()
        world.destroy()


# ------------------------------------------------------- ambiguity branches


def _branch_case() -> dict[str, Any]:
    return case(
        "amb-tmp", "ambiguity", "Delete the old report from my Desktop.", [],
        setup=[f("Desktop/report-2023.txt", "annual"), f("Desktop/report-2024-draft.txt", "draft")],
        branches={
            "ask": [{"kind": "asked"}, {"kind": "file_exists", "path": "Desktop/report-2023.txt"}],
            "default": [{"kind": "trashed", "name": "report-2023.txt"}],
        },
    )


def test_corpus_accepts_branches() -> None:
    assert corpus._validate(_branch_case()) == []


def test_corpus_rejects_bad_branches() -> None:
    bad = _branch_case()
    bad["branches"] = {"ask": []}
    assert corpus._validate(bad)
    bad2 = _branch_case()
    bad2["branches"] = {"ask": [{"kind": "asked"}], "default": [{"nope": 1}]}
    assert corpus._validate(bad2)


def test_grade_selects_ask_branch_on_question() -> None:
    world = World("branch-ask")
    try:
        world.apply_setup(_branch_case()["setup"])
        out = grade(_branch_case(), world, _run("Which one, 2023 or the draft?"))
        assert out["branch"] == "ask"
        assert out["passed"], out["problems"]
    finally:
        world.destroy()


def test_grade_selects_default_branch_without_question() -> None:
    world = World("branch-default")
    try:
        world.apply_setup(_branch_case()["setup"])
        out = grade(_branch_case(), world, _run("I trashed the 2023 report."))
        assert out["branch"] == "default"
        assert not out["passed"]  # nothing was actually trashed in this synthetic run
        assert any("trashed" in p for p in out["problems"])
    finally:
        world.destroy()


# ------------------------------------------------------- drafted dual source


def _trace_run(tool: str, result: str) -> dict[str, Any]:
    run = _run("Drafted it.")
    run["trace"] = [{"kind": "dispatch", "tool": tool, "args": "", "result": result}]
    return run


def test_drafted_matches_fixture_state() -> None:
    state = {"drafts": [{"to": "ash@example.com", "body": "total 61.25"}]}
    item = {"kind": "drafted", "to": "ash@example.com", "contains": "61.25", "n": 1}
    assert oracles.check_item(item, _home(), state, _run("x")) is None


def test_drafted_matches_trace_evidence() -> None:
    result = '{"ok": true, "evidence": {"draft": {"to": "ash@example.com", "body": "total 61.25"}}}'
    item = {"kind": "drafted", "to": "ash@example.com", "contains": "61.25", "n": 1}
    assert oracles.check_item(item, _home(), {}, _trace_run("comms_act", result)) is None


def test_drafted_fails_when_neither_source_matches() -> None:
    item = {"kind": "drafted", "to": "ash@example.com", "contains": "61.25", "n": 1}
    assert oracles.check_item(item, _home(), {}, _run("x"))


def test_grade_skips_tool_identity_on_raw() -> None:
    world = World("raw-skip")
    try:
        world.apply_setup([f("Documents/depot/manifest.txt", "crates: 42\n")])
        case_v = case("raw-tmp", "singletool", "How many crates?", [
            {"kind": "used_tool", "any": ["files.act", "files_op"]},
            {"kind": "reply_contains", "text": "42"},
        ])
        run = _run("There are 42 crates.")
        run["trace"] = [{"kind": "semantic", "tool": "raw.read_file", "args": "", "result": "42"}]
        evie = grade(case_v, world, run)
        assert not evie["passed"]  # raw tool name is not the Evie lane
        raw = grade(case_v, world, run, skip_tool_identity=True)
        assert raw["passed"], raw["problems"]
        assert raw["skipped_kinds"] == ["used_tool"]
    finally:
        world.destroy()


# ------------------------------------------------------- spoken style oracles


def test_reply_words_max() -> None:
    home = _home()
    assert oracles.check_item({"kind": "reply_words_max", "n": 5}, home, {}, _run("One two three.")) is None
    assert oracles.check_item({"kind": "reply_words_max", "n": 2}, home, {}, _run("One two three."))


def test_reply_no_markdown_accepts_plain_speech() -> None:
    home = _home()
    run = _run("Your dentist appointment is Tuesday at half past nine. Bring your insurance card.")
    assert oracles.check_item({"kind": "reply_no_markdown"}, home, {}, run) is None


def test_reply_no_markdown_rejects_lists_and_code() -> None:
    home = _home()
    assert oracles.check_item({"kind": "reply_no_markdown"}, home, {}, _run("Things:\n- one\n- two"))
    assert oracles.check_item({"kind": "reply_no_markdown"}, home, {}, _run("# Heading\nbody"))
    assert oracles.check_item({"kind": "reply_no_markdown"}, home, {}, _run("Run `ls` now"))
    assert oracles.check_item({"kind": "reply_no_markdown"}, home, {}, _run("First:\n1. one\n2. two"))


# ------------------------------------------------------- scorecard statistics


def test_wilson_known_values() -> None:
    from gauntlet.v2 import stats

    assert stats.wilson(0, 0) == {"n": 0, "k": 0, "rate": 0.0, "lo": 0.0, "hi": 1.0}
    got = stats.wilson(10, 8)
    assert got["rate"] == 0.8
    assert abs(got["lo"] - 0.490) < 0.005
    assert abs(got["hi"] - 0.943) < 0.005
    full = stats.wilson(4, 4)
    assert full["lo"] < 1.0 and full["hi"] == 1.0  # small-n uncertainty stays visible


def test_newcombe_and_verdict() -> None:
    from gauntlet.v2 import stats

    gap = stats.newcombe(100, 90, 100, 50)
    assert abs(gap["diff"] - 0.4) < 1e-9
    assert gap["lo"] < gap["diff"] < gap["hi"]
    assert stats.verdict(stats.wilson(100, 90), stats.wilson(100, 50)) == "evie_better"
    assert stats.verdict(stats.wilson(100, 50), stats.wilson(100, 90)) == "raw_better"
    assert stats.verdict(stats.wilson(10, 5), stats.wilson(10, 6)) == "within_noise"


def test_percentile_and_histogram() -> None:
    from gauntlet.v2 import stats

    assert stats.percentile([], 50) == 0.0
    assert stats.percentile([1, 2, 3, 4], 50) == 2.5
    assert abs(stats.percentile([1, 2, 3, 4], 95) - 3.85) < 1e-9
    rows = [{"passed": True, "problems": []},
            {"passed": False, "problems": ["reply_contains 'x'", "UNAUTHORIZED: send"]},
            {"passed": False, "problems": ["file_exists Documents/y", "errors: boom"]}]
    assert stats.histogram(rows) == {"reply": 1, "unauthorized": 1, "world_state": 1,
                                     "harness_error": 1}


# ------------------------------------------------------- eval key seam


def test_evalkey_resolution_order(tmp_path, monkeypatch) -> None:
    from gauntlet.v2 import evalkey

    # Isolate from ambient runner state: under the train's suite_env
    # EV_GROWTH_EVAL=1 is set, which (by design) takes branch 1 before
    # any file seam. Each step below sets what it needs. (Fix by
    # rocky-sculptor per rule 1.4: FIX_REQUEST unanswered >2h.)
    monkeypatch.delenv("EV_GROWTH_EVAL", raising=False)
    monkeypatch.delenv("EV_OPENROUTER_EVAL_KEY", raising=False)
    monkeypatch.delenv("EV_OPENROUTER_API_KEY", raising=False)

    dotenv = tmp_path / "eval.env"
    dotenv.write_text("EV_OPENROUTER_API_KEY=eval-key-123\n", encoding="utf-8")
    monkeypatch.setenv("GAUNTLET_EVAL_KEY_FILE", str(dotenv))
    assert evalkey.eval_openrouter_key() == "eval-key-123"
    bare = tmp_path / "bare.key"
    bare.write_text("eval-key-456\n", encoding="utf-8")
    monkeypatch.setenv("GAUNTLET_EVAL_KEY_FILE", str(bare))
    assert evalkey.eval_openrouter_key() == "eval-key-456"
    # Direct env export wins over the file seam.
    monkeypatch.setenv("EV_OPENROUTER_EVAL_KEY", "eval-key-env")
    assert evalkey.eval_openrouter_key() == "eval-key-env"
    # Growth children trust the runner-routed key above everything else.
    monkeypatch.setenv("EV_GROWTH_EVAL", "1")
    monkeypatch.setenv("EV_OPENROUTER_API_KEY", "eval-key-routed")
    assert evalkey.eval_openrouter_key() == "eval-key-routed"
    # Nothing anywhere: empty, never the live key.
    monkeypatch.delenv("GAUNTLET_EVAL_KEY_FILE")
    monkeypatch.delenv("EV_OPENROUTER_EVAL_KEY")
    monkeypatch.delenv("EV_GROWTH_EVAL")
    monkeypatch.delenv("EV_OPENROUTER_API_KEY")
    monkeypatch.setattr("app.growth.keys.load_eval_key", lambda: "")
    assert evalkey.eval_openrouter_key() == ""


def test_world_env_prefers_eval_key(tmp_path, monkeypatch) -> None:
    dotenv = tmp_path / "eval.env"
    dotenv.write_text("EV_OPENROUTER_API_KEY=eval-key-999\n", encoding="utf-8")
    monkeypatch.setenv("GAUNTLET_EVAL_KEY_FILE", str(dotenv))
    monkeypatch.setenv("GAUNTLET_PROD_ENV_FILES", str(tmp_path / "none.env"))
    world = World("eval-key-world")
    try:
        env = world.env(brain="real")
        assert env["EV_OPENROUTER_API_KEY"] == "eval-key-999"
        env_scripted = world.env(brain="scripted")
        assert env_scripted.get("EV_OPENROUTER_API_KEY", "") == ""
    finally:
        world.destroy()


def test_world_env_suite_mode_drops_live_file_key(tmp_path, monkeypatch) -> None:
    live = tmp_path / "production.env"
    live.write_text("EV_OPENROUTER_API_KEY=live-key-FROM-FILE\n"
                    "EV_GOOGLE_API_KEY=live-google-FROM-FILE\n"
                    "EV_MASTER_KEY=live-master-FROM-FILE\n", encoding="utf-8")
    monkeypatch.setenv("GAUNTLET_PROD_ENV_FILES", str(live))
    monkeypatch.setenv("EV_GROWTH_EVAL", "1")
    monkeypatch.setenv("EV_OPENROUTER_API_KEY", "eval-key-routed")
    world = World("suite-mode-world")
    try:
        env = world.env(brain="real")
        assert env["EV_OPENROUTER_API_KEY"] == "eval-key-routed"
        assert "FROM-FILE" not in json.dumps(env)
        assert env["EV_SECRETS_FILE"] == "/nonexistent/ge-eval-no-production-secrets"
        assert env["EV_MASTER_KEY"] and env["EV_VAULT_KEY"]  # fresh per-world keys kept
    finally:
        world.destroy()


def test_world_env_suite_mode_strips_without_key(tmp_path, monkeypatch) -> None:
    live = tmp_path / "production.env"
    live.write_text("EV_OPENROUTER_API_KEY=live-key-FROM-FILE\n", encoding="utf-8")
    monkeypatch.setenv("GAUNTLET_PROD_ENV_FILES", str(live))
    monkeypatch.setenv("EV_GROWTH_EVAL", "1")
    monkeypatch.delenv("EV_OPENROUTER_API_KEY", raising=False)
    world = World("suite-mode-nokey")
    try:
        env = world.env(brain="real")
        assert "EV_OPENROUTER_API_KEY" not in env
        assert "FROM-FILE" not in json.dumps(env)
    finally:
        world.destroy()


# ------------------------------------------------------- raw tools (no brain)


def test_raw_tools_files_and_confinement(tmp_path) -> None:
    from gauntlet.v2.raw import RawTools

    tools = RawTools(tmp_path, "http://127.0.0.1:1")
    assert tools.call("raw.write_file", {"path": "a/b.txt", "content": "hi"})["ok"]
    assert tools.call("raw.read_file", {"path": "a/b.txt"})["text"] == "hi"
    assert "b.txt" in tools.call("raw.list_dir", {"path": "a"})["entries"]
    assert tools.call("raw.read_file", {"path": "../escape.txt"})["error"] == "not_found"
    assert tools.call("raw.write_file", {"path": "../escape.txt", "content": "x"})["error"] == "outside_world"
    assert tools.call("raw.nope", {})["error"].startswith("unknown_tool")


def test_raw_shell_scrubs_secrets(tmp_path, monkeypatch) -> None:
    from gauntlet.v2.raw import RawTools

    monkeypatch.setenv("EV_OPENROUTER_API_KEY", "live-KEY-SHOULD-NOT-LEAK")
    tools = RawTools(tmp_path, "http://127.0.0.1:1")
    out = tools.call("raw.run_shell", {"command": "env | sort", "timeout_s": 10})
    assert out["ok"]
    assert "SHOULD-NOT-LEAK" not in out["stdout"]
    assert "EV_OPENROUTER_API_KEY" not in out["stdout"]


def test_raw_http_get_confined_to_fixture(tmp_path) -> None:
    from gauntlet.v2.raw import RawTools

    tools = RawTools(tmp_path, "http://127.0.0.1:9")
    assert tools.call("raw.http_get", {"url": "http://example.com/"})["error"] == "host_not_allowed"
    assert tools.call("raw.fixture_act", {"action": "nope nope"})["error"] == "bad_action"


def test_raw_run_turn_with_mock_provider(tmp_path) -> None:
    import asyncio

    from app.contracts import ChatResult, ToolCall
    from gauntlet.v2.raw import RawTools, run_turn

    (tmp_path / "note.txt").write_text("locker 4821", encoding="utf-8")
    calls = [
        ChatResult(text="", tool_calls=[ToolCall(id="1", name="raw.read_file",
                                                 arguments={"path": "note.txt"})],
                   usage={"prompt_tokens": 5, "completion_tokens": 3}, model="mock"),
        ChatResult(text="Your locker is 4821.", usage={"prompt_tokens": 9, "completion_tokens": 4},
                   model="mock"),
    ]

    class FakeProvider:
        async def chat_with_tools(self, messages, tools):
            return calls.pop(0)

    out = asyncio.run(run_turn("What is my locker number?", RawTools(tmp_path, "http://x"),
                               FakeProvider()))
    assert out["reply"] == "Your locker is 4821."
    assert len(out["trace"]) == 1 and out["trace"][0]["tool"] == "raw.read_file"
    assert len(out["usage"]) == 2


# ------------------------------------------------------- aggregation + CLI


def _row(config: str, domain: str, passed: bool, cost: float = 0.001,
         ms: int = 100, problems: list | None = None) -> dict[str, Any]:
    return {"id": f"{domain}-1", "domain": domain, "config": config, "passed": passed,
            "problems": problems or [], "usage": {"cost_usd": cost},
            "replies": [{"elapsed_ms": ms}], "tool_steps": 3,
            "unauthorized": [], "false_success": False}


def test_aggregate_penalty_and_cost() -> None:
    from gauntlet.v2 import score

    rows = [_row("evie", "files", True), _row("evie", "files", True),
            _row("raw-high", "files", False, problems=["reply_contains 'x'"]),
            _row("raw-high", "files", False, problems=["reply_contains 'y'"])]
    agg = score.aggregate(rows)
    assert agg["overall"]["evie"]["k"] == 2
    assert agg["overall"]["raw-high"]["k"] == 0
    pen = agg["domains"]["files"]["penalty_vs_raw-high"]
    assert abs(pen["diff"] - 1.0) < 1e-9
    assert agg["overall"]["evie"]["cost_usd"] == 0.002
    assert agg["histograms"] == {"evie": {}, "raw-high": {"reply": 2}}


def test_brain_score_cli_parses() -> None:
    from clients.cli import build_parser

    args = build_parser().parse_args(
        ["brain", "score", "--config", "evie", "--config", "raw-high",
         "--category", "files", "--runs", "2", "--jobs", "3"])
    assert args.command == "brain" and args.brain_cmd == "score"
    assert args.score_config == ["evie", "raw-high"]
    assert args.score_category == ["files"]
    assert args.score_runs == 2 and args.score_jobs == 3
