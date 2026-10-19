"""Tests for the gauntlet harness, layout inference, streaming, and honesty."""
from __future__ import annotations

import ast
from pathlib import Path

from app.fabric.honesty import claim, claims_success
from app.fabric.screen_layout import commands_from_ocr, infer_splits, write_png

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"


def test_corpus_shape_and_split() -> None:
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


def test_templates_parse() -> None:
    text = (ROOT.parent / "docs" / "templates" / "IMPROVE_BRIEF.md").read_text(encoding="utf-8")
    assert "R1" in text and "pass@1" in text
    ast.parse((ROOT / "gauntlet" / "runner.py").read_text(encoding="utf-8"))
