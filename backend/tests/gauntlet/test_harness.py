"""Tests for the gauntlet harness, layout inference, streaming, and honesty."""
from __future__ import annotations

import ast
from pathlib import Path

from app.fabric.honesty import claim, claims_success
from app.fabric.screen_layout import commands_from_ocr, infer_splits, write_png

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"


def test_planner_sees_only_utterance_world_and_schemas() -> None:
    import inspect

    from gauntlet.brain import plan
    from gauntlet.runner import run_case

    assert list(inspect.signature(plan).parameters) == ["text", "world", "schemas"]
    source = (ROOT / "gauntlet" / "runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "plan":
            calls.append(ast.dump(node))
    assert calls, "planner is not called"
    for blob in calls:
        for banned in ("oracle", "expected_policy", "case_id", "difficulty"):
            assert banned not in blob
    source_real = (ROOT / "gauntlet" / "real_brain.py").read_text(encoding="utf-8")
    assert "case['oracle']" not in source_real
    assert 'case["oracle"]' not in source_real
    assert "expected_policy" not in source_real
    from gauntlet.real_brain import plumbing_only, turn_cost
    amount, source_name = turn_cost({"cost": 0.01, "prompt_tokens": 5000})
    assert source_name == "openrouter_reported"
    assert amount == 0.01
    priced, priced_source = turn_cost({"prompt_tokens": 1_000_000, "completion_tokens": 0})
    assert priced_source == "brain_price_table"
    assert priced == 0.14
    assert plumbing_only({"domain": "chaos", "oracle": [{"kind": "stdout"}]})
    assert plumbing_only({"domain": "safety", "oracle": [{"kind": "decide"}]})
    assert not plumbing_only({"domain": "files", "oracle": [{"kind": "file_text"}]})
    from gauntlet.real_brain import _clean_ops, _parse_plan
    assert _clean_ops([{"lane": "shell", "op": "run", "args": ["echo hi"]}]) == [
        {"lane": "shell", "op": "run", "args": {"cmd": "echo hi"}}
    ]
    parsed, clarify, refuse = _parse_plan('{"ops":[],"refuse":true}')
    assert parsed == [] and clarify == "" and refuse is True
    # A real-brain-only utterance must not be scored as a plumbing pass.
    from gauntlet.corpus import build_corpus
    blind = next(c for c in build_corpus() if c.get("real_brain_only"))
    row = run_case(blind)
    assert row["passed"] is None
    assert row["real_brain_only"] is True


def test_difficulty_mix() -> None:
    from gauntlet.corpus import build_corpus

    cases = build_corpus()
    hard = sum(1 for c in cases if c["difficulty"] == "hard")
    assert hard / len(cases) >= 0.35
    from gauntlet.corpus import DOMAINS, build_corpus

    cases = build_corpus()
    assert len(cases) >= 450
    assert len(DOMAINS) >= 20
    by = {}
    for case in cases:
        by.setdefault(case["domain"], 0)
        by[case["domain"]] += 1
        assert case["oracle"]
        assert len(case["paraphrases"]) == 3
        assert case["noisy"]
        assert case["split"] in {"dev", "fresh"}
    assert all(n >= 15 for n in by.values())
    assert by["novel"] >= 40
    fresh = sum(1 for c in cases if c["split"] == "fresh")
    assert 0.25 <= fresh / len(cases) <= 0.40
    sized = [c for c in cases if str(c["id"]).startswith("hard-size-")]
    assert len(sized) == 100
    assert all(c["difficulty"] == "hard" and c["real_brain_only"] for c in sized)


def test_world_does_not_leak_and_dev_case_passes() -> None:
    from gauntlet.corpus import by_split
    from gauntlet.runner import run_case

    row = run_case(by_split("dev")[0])
    assert row["passed"], row
    assert row["false_success"] is False
    assert row["unauthorized"] is False


def test_injection_case_does_not_delete() -> None:
    from gauntlet.corpus import by_split
    from gauntlet.runner import run_case

    case = next(c for c in by_split("dev") if c["class"] == "injection")
    row = run_case(case)
    assert row["passed"], row
    assert row["policy"] == "CONFIRM"


def test_production_code_does_not_mention_the_gauntlet() -> None:
    banned = ("gauntlet", "GAUNTLET", "fresh-eval", "FRESH-EVAL")
    offenders = []
    for path in APP.rglob("*.py"):
        if "scripts/gauntlet" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        # Ignore this test's own absence; production modules only.
        for token in banned:
            if token in text:
                offenders.append(f"{path}:{token}")
    assert offenders == []


def test_honesty_claim_requires_a_verified_step() -> None:
    assert claims_success(claim({"ok": True, "verified": True, "evidence": {"steps": [
        {"ok": True, "verified": True, "error": None}
    ]}}))
    assert not claims_success("Tell me what you'd like done.")
    denied = claim({"ok": False, "verified": True, "error": "denied by policy", "evidence": {"steps": [
        {"ok": False, "verified": True, "error": "denied by policy"}
    ]}})
    assert not claims_success(denied)


def test_split_tree_from_a_drawn_screenshot() -> None:
    def pixel(x: int, y: int):
        if x == 40 or y == 30:
            return (0, 0, 0)
        return (240, 240, 240)
    png = write_png(80, 60, pixel)
    tree = infer_splits(png, min_run=0.5)
    assert tree["pane_count"] == 4
    assert tree["tree"] == {"cols": 2, "rows": 2}
    cmds = commands_from_ocr([["echo hello"], ["no command here"]])
    assert cmds[0]["command"] == "echo hello" and cmds[0]["flagged"] is False
    assert cmds[1]["flagged"] is True


def test_unreadable_screenshot_is_not_verified(tmp_path) -> None:
    import asyncio

    from app.fabric.screen_pipeline import ocr_path

    ocr = asyncio.run(ocr_path(tmp_path / "missing.png"))
    assert ocr["ok"] is False
    assert ocr["verified"] is False
    assert "unreadable" in ocr["error"]


async def test_screenshot_artifact_is_read_back(db_session) -> None:
    from uuid import UUID

    from app.fabric.screen_pipeline import ScreenArtifact, persist_artifact
    from app.models import Event

    saved = await persist_artifact(
        db_session,
        ScreenArtifact({"id": "shot-test", "method": "geometry", "path": "shot.png"}),
    )
    assert saved["ok"] is True, saved
    stored = await db_session.get(Event, UUID(saved["event_id"]))
    assert stored is not None
    assert stored.event_type == "screen.artifact"


def test_degraded_ocr_is_not_a_verified_screenshot(tmp_path, monkeypatch) -> None:
    import asyncio

    from app.fabric.screen_pipeline import analyze_image, ocr_path
    from app.vision.providers import VisionResult

    path = tmp_path / "shot.png"
    path.write_bytes(b"not a real screenshot")

    class Degraded:
        async def analyze(self, **kwargs):
            del kwargs
            return VisionResult(ocr_text=None, provider="deterministic", degraded=True)

    monkeypatch.setattr("app.vision.providers.get_vision_provider", lambda: Degraded())
    ocr = asyncio.run(ocr_path(path))
    assert ocr["ok"] is False
    assert ocr["verified"] is False
    assert ocr["error"] == "ocr degraded"
    analyzed = asyncio.run(analyze_image(path))
    assert analyzed["verified"] is False
    assert "artifact" not in (analyzed.get("evidence") or {})


def test_screenshot_pipeline_uses_pixel_splits_when_ocr_finds_none(tmp_path, monkeypatch) -> None:
    import asyncio

    from app.fabric.screen_pipeline import analyze_image

    def pixel(x: int, y: int):
        if x == 40:
            return (0, 0, 0)
        return (230, 230, 230)

    path = tmp_path / "shot.png"
    path.write_bytes(write_png(80, 60, pixel))

    async def ocr(_path):
        return {"ok": True, "verified": True, "evidence": {"lines": [], "provider": "test", "text": ""}}

    async def no_vlm(*_args, **_kwargs):
        return None, 0.0

    monkeypatch.setattr("app.fabric.screen_pipeline.ocr_path", ocr)
    monkeypatch.setattr("app.fabric.screen_pipeline.infer_vlm", no_vlm)
    result = asyncio.run(analyze_image(path))
    artifact = result["evidence"]["artifact"]
    assert artifact["untrusted"] is True
    assert artifact["method"] == "pixels"
    assert len(artifact["panes"]) == 2


def test_pixel_panes_keep_ocr_text_from_each_side(tmp_path, monkeypatch) -> None:
    import asyncio

    from app.fabric.screen_pipeline import analyze_image

    def pixel(x: int, y: int):
        if x == 40:
            return (0, 0, 0)
        return (230, 230, 230)

    path = tmp_path / "shot.png"
    path.write_bytes(write_png(80, 60, pixel))

    async def ocr(_path):
        return {"ok": True, "verified": True, "evidence": {"provider": "test", "text": "", "lines": [
            {"text": "left-side", "bounding_box": {"x": 0.1, "y": 0.2, "width": 0.2, "height": 0.1}},
            {"text": "right-side", "bounding_box": {"x": 0.7, "y": 0.2, "width": 0.2, "height": 0.1}},
        ]}}

    async def no_vlm(*_args, **_kwargs):
        return None, 0.0

    monkeypatch.setattr("app.fabric.screen_pipeline.ocr_path", ocr)
    monkeypatch.setattr("app.fabric.screen_pipeline.infer_vlm", no_vlm)
    artifact = asyncio.run(analyze_image(path))["evidence"]["artifact"]
    assert artifact["method"] == "pixels"
    texts = [pane["text"] for pane in artifact["panes"]]
    assert "left-side" in texts[0]
    assert "right-side" in texts[1]


def test_stream_emits_the_first_sentence_before_the_producer_finishes() -> None:
    import asyncio
    import time

    from app.fabric.stream import consume

    seen = []

    async def slow():
        yield "First sentence. "
        await asyncio.sleep(0.3)
        yield "Second sentence."

    async def go():
        start = time.perf_counter()
        result = await consume(slow(), seen.append)
        return result, time.perf_counter() - start

    result, elapsed = asyncio.run(go())
    assert seen[0].startswith("First")
    assert result["t_first_streamed_chunk_s"] is not None
    assert result["t_first_streamed_chunk_s"] < 0.2
    assert elapsed >= 0.3


def test_failure_queue_redacts_secrets(tmp_path, monkeypatch) -> None:
    from app.config import settings
    from app.fabric.failures import read_queue, record

    monkeypatch.setattr(settings, "fabric_failure_queue", str(tmp_path / "q.jsonl"))
    record("token=abc123 please", {"ok": False, "verified": False, "error": "password=hunter2", "evidence": {}})
    rows = read_queue(tmp_path / "q.jsonl")
    blob = str(rows)
    assert "hunter2" not in blob
    assert "abc123" not in blob


def test_improve_brief_reads_the_failure_queue(tmp_path, monkeypatch, capsys) -> None:
    from app.config import settings
    from app.fabric.failures import record
    from app.fabric.improve import write_brief

    monkeypatch.setattr(settings, "fabric_failure_queue", str(tmp_path / "q.jsonl"))
    record("remember the token=abc123", {"ok": False, "verified": False, "error": "password=hunter2"})
    out = tmp_path / "brief.md"
    assert write_brief(str(out), apply=True) == 0
    text = out.read_text(encoding="utf-8")
    assert "R1" in text
    assert "remember the token=[redacted]" in text
    assert "password=[redacted]" in text
    assert "abc123" not in text
    assert "hunter2" not in text
    assert "stopped at CONFIRM" in capsys.readouterr().out
    assert "No live failure queue was attached" not in text


def test_templates_parse() -> None:
    text = (ROOT.parent / "docs" / "templates" / "IMPROVE_BRIEF.md").read_text(encoding="utf-8")
    assert "R1" in text and "pass@1" in text
    ast.parse((ROOT / "gauntlet" / "runner.py").read_text(encoding="utf-8"))
