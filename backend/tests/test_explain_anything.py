"""Unified explain: projects, files, folders, PDFs answer fast and purpose-first."""

from __future__ import annotations

from pathlib import Path

import pytest


def _seed_wish(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "OVERVIEW.md").write_text(
        "# Overview\n\n## What it is\nSweet Potato is a birthday film for two.\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        '{"name": "wish", "description": "Sweet Potato birthday film",'
        ' "dependencies": {"next": "15.0.0", "three": "0.160.0"}}',
        encoding="utf-8",
    )
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "scene.ts").write_text("export const scene = 1;\n", encoding="utf-8")
    return root


def _code_env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    from app.config import settings
    from app.ev.code_runtime import clear_sticky_project

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    code_home = tmp_path / "Code"
    wish = _seed_wish(code_home / "wish")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(code_home))
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "code_model", "")
    monkeypatch.setattr("app.gateway.muse.muse_brain_active", lambda: False)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: False)
    try:
        from app.ev.code_sandbox import reset_folder_map

        reset_folder_map()
    except Exception:
        pass
    try:
        from app.ev.luna_code import _LAST_CODE_JOBS

        _LAST_CODE_JOBS.clear()
    except Exception:
        pass
    clear_sticky_project()
    return sandbox, wish


def _files_env(tmp_path: Path, monkeypatch) -> Path:
    from app.config import settings
    from app.ev.file_index import reset_file_index

    root = tmp_path / "laptop"
    root.mkdir()
    monkeypatch.setattr(settings, "laptop_files", True)
    monkeypatch.setattr(settings, "laptop_files_root", str(root))
    reset_file_index()
    return root


def test_explain_ask_covers_analyze_and_all_targets() -> None:
    from app.ev.explain import looks_like_explain_ask

    for ask in (
        "give me info about the wish project",
        "analyze the wish project",
        "analyse this PDF",
        "review the downloads folder",
        "break down this file for me",
        "give me the gist of Q3 report",
        "tell me about my notes file",
        "summarize this folder",
    ):
        assert looks_like_explain_ask(ask), ask
    assert not looks_like_explain_ask("write a script that prints hello")
    assert not looks_like_explain_ask("send mom a text saying hi")


def test_analyze_verbs_route_to_fast_code_explain(tmp_path: Path, monkeypatch) -> None:
    from app.ev.luna_code import is_read_only_code_ask, looks_like_code_explain

    _code_env(tmp_path, monkeypatch)
    assert looks_like_code_explain("analyze the wish project")
    assert is_read_only_code_ask("analyze the wish project")
    assert looks_like_code_explain("give me info about the wish project")


@pytest.mark.asyncio
async def test_explain_project_is_purpose_not_file_dump(
    tmp_path: Path, monkeypatch
) -> None:
    from app.ev.explain import explain_anything
    from app.ev.luna_code import _spoken_is_file_dump

    _code_env(tmp_path, monkeypatch)
    result = explain_anything("give me info about the wish project")
    assert result.get("ok") is True
    assert result.get("kind") == "project"
    spoken = str(result.get("spoken") or "")
    lowered = spoken.lower()
    assert "sweet potato" in lowered
    assert not _spoken_is_file_dump(spoken)
    assert "scene.ts" not in spoken
    assert "package.json" not in lowered


@pytest.mark.asyncio
async def test_explain_project_analyze_variant(tmp_path: Path, monkeypatch) -> None:
    from app.ev.explain import explain_anything

    _code_env(tmp_path, monkeypatch)
    result = explain_anything("analyze the wish project for me")
    assert result.get("ok") is True
    assert "sweet potato" in str(result.get("spoken") or "").lower()


def test_explain_folder_surveys_without_dump(tmp_path: Path, monkeypatch) -> None:
    from app.ev.explain import explain_anything

    root = _files_env(tmp_path, monkeypatch)
    trip = root / "trip"
    trip.mkdir()
    (trip / "README.md").write_text("# Trip\nBeach weekend plan.\n", encoding="utf-8")
    (trip / "flight.txt").write_text("flight details\n", encoding="utf-8")
    (trip / "hotel.txt").write_text("hotel details\n", encoding="utf-8")

    result = explain_anything("summarize the trip folder")
    assert result.get("ok") is True
    assert result.get("kind") == "folder"
    spoken = str(result.get("spoken") or "")
    assert "trip" in spoken.lower()
    assert len(spoken) <= 700


def test_explain_text_file_summarizes(tmp_path: Path, monkeypatch) -> None:
    from app.ev.explain import explain_anything

    root = _files_env(tmp_path, monkeypatch)
    note = root / "notes.txt"
    note.write_text(
        "Quarterly plan\n\nWe will ship the dashboard in June. "
        "The API stays offline-first for the owner.\n",
        encoding="utf-8",
    )
    result = explain_anything("explain my notes file")
    assert result.get("ok") is True
    assert result.get("kind") == "file"
    spoken = str(result.get("spoken") or "")
    assert "notes.txt" in spoken
    assert "dashboard" in spoken.lower() or "offline" in spoken.lower()


def test_explain_pdf_without_engine_is_honest(tmp_path: Path, monkeypatch) -> None:
    from app.ev.explain import explain_anything

    root = _files_env(tmp_path, monkeypatch)
    pdf = root / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake\n")
    monkeypatch.setattr("app.ev.explain.extract_pdf_text", lambda *a, **k: ("", 0, "none"))
    result = explain_anything("tell me about report pdf")
    assert result.get("kind") == "pdf"
    spoken = str(result.get("spoken") or "").lower()
    assert "pdf" in spoken
    assert "report.pdf" in spoken or "report" in spoken
    assert result.get("degraded") is True
    # Never fabricate contents.
    assert "covers:" not in spoken


def test_explain_pdf_with_text_summarizes(tmp_path: Path, monkeypatch) -> None:
    from app.ev.explain import explain_anything

    root = _files_env(tmp_path, monkeypatch)
    pdf = root / "plan.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake\n")
    monkeypatch.setattr(
        "app.ev.explain.extract_pdf_text",
        lambda *a, **k: ("The project ships a solar roof in July. It saves power.", 3, "pypdf"),
    )
    result = explain_anything("summarize plan pdf")
    assert result.get("kind") == "pdf"
    assert result.get("degraded") is False
    spoken = str(result.get("spoken") or "").lower()
    assert "solar" in spoken or "july" in spoken


def test_read_pdf_uses_explainer_not_binary_refusal(
    tmp_path: Path, monkeypatch
) -> None:
    from app.ev import laptop_files

    root = _files_env(tmp_path, monkeypatch)
    pdf = root / "brief.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake\n")
    monkeypatch.setattr(
        "app.ev.explain.extract_pdf_text",
        lambda *a, **k: ("Brief about the harbor job. It starts Monday.", 1, "pypdf"),
    )
    out = laptop_files._read_file(pdf)
    assert out.get("ok") is True
    assert "not a text file" not in str(out.get("spoken") or "").lower()
    assert "harbor" in str(out.get("spoken") or "").lower()


@pytest.mark.asyncio
async def test_kernel_explain_answers_synchronously(tmp_path: Path, monkeypatch) -> None:
    from app.cognitive.kernel import _dispatch_kernel_code, _dispatch_kernel_explain
    from app.cognitive.session_store import CognitiveSession

    _code_env(tmp_path, monkeypatch)
    cognition = CognitiveSession(session_id="explain-kernel")

    hit = await _dispatch_kernel_explain(
        "give me info about the wish project",
        cognition=cognition,
        started=0.0,
    )
    assert hit is not None
    assert "sweet potato" in hit.spoken.lower()
    assert hit.kind == "explain"

    # Read-only code asks must not background ("I'm writing that now").
    calls: list[str] = []

    async def _notify(*_a, **_k):
        calls.append("backgrounded")

    monkeypatch.setattr("app.ev.luna_code.run_code_job_and_notify", _notify)
    routed = await _dispatch_kernel_code(
        None,  # type: ignore[arg-type]
        "give me info about the wish project",
        cognition=cognition,
        actor="voice",
        live_session_id="live-1",
        modality="voice",
        steering_seen=int(cognition.steering_version),
        started=0.0,
    )
    assert routed is not None
    assert "writing that now" not in routed.spoken.lower()
    assert "sweet potato" in routed.spoken.lower()
    assert calls == []


@pytest.mark.asyncio
async def test_executor_explain_act_is_one_shot(tmp_path: Path, monkeypatch) -> None:
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import CognitiveSession

    _code_env(tmp_path, monkeypatch)
    cognition = CognitiveSession(session_id="explain-exec")
    body = await execute_semantic(
        None,  # type: ignore[arg-type]
        "explain.act",
        {"query": "give me info about the wish project"},
        cognition=cognition,
        actor="test",
        live_session_id=None,
        steering_seen=int(cognition.steering_version),
    )
    assert body.get("ok") is True
    assert "sweet potato" in str(body.get("spoken") or "").lower()


def test_explain_capability_registered() -> None:
    from app.cognitive.capabilities import SEMANTIC_TOOLS

    names = {tool.get("name") for tool in SEMANTIC_TOOLS}
    assert "explain.act" in names


def test_explain_does_not_steal_camera_looks() -> None:
    from app.ev.explain import explain_anything, looks_like_explain_ask

    holding = (
        "Look at the thing I'm holding in my hand. I want you to look at it "
        "and tell me more info about this item I'm holding"
    )
    assert not looks_like_explain_ask(holding)
    assert explain_anything(holding).get("kind") == "miss" or not explain_anything(holding).get("ok")
