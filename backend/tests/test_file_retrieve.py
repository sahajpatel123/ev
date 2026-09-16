"""Generic retrieve: IDs, follow-ups, and arbitrary owner things — not per-type recipes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import settings
from app.ev.desk_names import reset_desk_names
from app.ev.file_retrieve import (
    clear_pending_retrieve,
    looks_like_retrieve_task,
    parse_retrieve_intent,
    pending_retrieve,
)
from app.ev.laptop_files import looks_like_file_task, parse_file_goal, run_file_goal
from app.ev.spark_act import fallback_act, live_tool_for_act
from app.ev.tool_select import resolve_live_action, select_tool


@pytest.fixture(autouse=True)
def _reset_scene():
    reset_desk_names()
    clear_pending_retrieve()
    yield
    reset_desk_names()
    clear_pending_retrieve()


@pytest.fixture
def files_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "laptop_files", True)
    monkeypatch.setattr(settings, "laptop_files_root", str(tmp_path))
    return tmp_path


def _opened(target: Path) -> dict:
    return {
        "ok": True,
        "executed": True,
        "verified": True,
        "action": "open",
        "path": str(target),
        "spoken": f"Opened {target.name}.",
        "source": "laptop_files",
    }


def test_get_my_ids_is_a_file_task_and_asks_which() -> None:
    for phrase in (
        "get me my ids",
        "get me my id's",
        "get my IDs",
        "show me my ids",
        "find my ids",
    ):
        assert looks_like_retrieve_task(phrase), phrase
        assert looks_like_file_task(phrase), phrase
        parsed = parse_file_goal(phrase)
        assert parsed is not None, phrase
        assert parsed["action"] == "ask_which"
        assert parsed.get("kind") == "retrieve"
        assert "which" in str(parsed.get("spoken") or "").lower()


def test_get_my_ids_routes_to_files_not_web() -> None:
    phrase = "get me my ids"
    assert select_tool(phrase).selected == "computer"
    live = resolve_live_action(phrase)
    assert live is not None and live[0] == "computer"
    decision = fallback_act(phrase)
    assert decision is not None and decision.act == "files"
    tool = live_tool_for_act(phrase, decision)
    assert tool is not None and tool[0] == "computer"


def test_government_id_follow_up_binds_and_opens(files_root: Path, monkeypatch) -> None:
    target = files_root / "government-id.pdf"
    target.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr("app.ev.laptop_files._open_file", _opened)

    first = parse_file_goal("get me my ids")
    assert first is not None and first["action"] == "ask_which"
    asked = asyncio.run(run_file_goal(first))
    assert asked["ok"] is True
    assert pending_retrieve() is not None
    assert looks_like_file_task("my government id")
    follow = parse_file_goal("my government id")
    assert follow is not None
    assert follow.get("retrieve") is True
    assert "government" in str(follow.get("query") or "").lower()
    result = asyncio.run(run_file_goal(follow))
    assert result.get("ok") is True
    assert Path(str(result.get("path") or "")).name == "government-id.pdf"
    assert pending_retrieve() is None


def test_typo_govenment_id_still_finds_government_file(files_root: Path, monkeypatch) -> None:
    target = files_root / "government-id.pdf"
    target.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr("app.ev.laptop_files._open_file", _opened)

    asyncio.run(run_file_goal(parse_file_goal("get me my ids")))
    follow = parse_file_goal("my govenment id")
    assert follow is not None and follow.get("retrieve") is True
    result = asyncio.run(run_file_goal(follow))
    assert result.get("ok") is True
    assert Path(str(result.get("path") or "")).name == "government-id.pdf"


def test_one_shot_government_id_opens(files_root: Path, monkeypatch) -> None:
    target = files_root / "government-id.pdf"
    target.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr("app.ev.laptop_files._open_file", _opened)

    parsed = parse_file_goal("get me my government id")
    assert parsed is not None
    assert parsed.get("retrieve") is True
    assert parsed["action"] == "open"
    result = asyncio.run(run_file_goal(parsed))
    assert result.get("ok") is True
    assert Path(str(result.get("path") or "")).name == "government-id.pdf"


def test_id_folder_hit_when_filename_is_unrelated(files_root: Path, monkeypatch) -> None:
    folder = files_root / "IDs"
    folder.mkdir()
    buried = folder / "Aadhaar.pdf"
    buried.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr("app.ev.laptop_files._open_file", _opened)

    asyncio.run(run_file_goal(parse_file_goal("get me my ids")))
    follow = parse_file_goal("government id")
    result = asyncio.run(run_file_goal(follow))
    assert result.get("ok") is True
    assert Path(str(result.get("path") or "")).name == "Aadhaar.pdf"


def test_several_id_files_ask_which_file(files_root: Path, monkeypatch) -> None:
    folder = files_root / "IDs"
    folder.mkdir()
    (folder / "Aadhaar.pdf").write_bytes(b"%PDF-1.4\n")
    (folder / "PAN.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr("app.ev.laptop_files._open_file", _opened)

    asyncio.run(run_file_goal(parse_file_goal("get me my ids")))
    follow = parse_file_goal("the government one")
    result = asyncio.run(run_file_goal(follow))
    assert result.get("action") == "ask_which"
    spoken = str(result.get("spoken") or "").lower()
    assert "aadhaar" in spoken or "pan" in spoken or "which" in spoken
    picked = parse_file_goal("aadhaar")
    assert picked is not None
    opened = asyncio.run(run_file_goal(picked))
    assert Path(str(opened.get("path") or "")).name == "Aadhaar.pdf"


def test_read_uses_owner_words_on_text(files_root: Path) -> None:
    note = files_root / "lease-terms.txt"
    note.write_text("rent is due on the first", encoding="utf-8")
    parsed = parse_file_goal("read my lease terms")
    assert parsed is not None
    assert parsed.get("retrieve") is True
    assert parsed["action"] == "read"
    result = asyncio.run(run_file_goal(parsed))
    assert result.get("ok") is True
    assert "rent is due" in str(result.get("spoken") or "").lower() or "lease-terms" in str(result.get("path") or "")


def test_open_ids_folder(files_root: Path, monkeypatch) -> None:
    folder = files_root / "IDs"
    folder.mkdir()
    (folder / "Aadhaar.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr("app.ev.laptop_files._open_file", _opened)
    parsed = parse_file_goal("open my ids folder")
    assert parsed is not None
    result = asyncio.run(run_file_goal(parsed))
    assert result.get("ok") is True
    path = Path(str(result.get("path") or ""))
    assert path.name == "IDs" or path.name == "Aadhaar.pdf"


def test_arbitrary_kinds_use_owner_words(files_root: Path, monkeypatch) -> None:
    (files_root / "lease-2024.pdf").write_bytes(b"%PDF-1.4\n")
    (files_root / "boarding-pass.png").write_bytes(b"png")
    (files_root / "insurance-card.jpg").write_bytes(b"jpg")
    monkeypatch.setattr("app.ev.laptop_files._open_file", _opened)

    cases = (
        ("get me my lease", "lease-2024.pdf"),
        ("open my boarding pass", "boarding-pass.png"),
        ("show me my insurance card", "insurance-card.jpg"),
    )
    for phrase, name in cases:
        assert looks_like_file_task(phrase), phrase
        parsed = parse_file_goal(phrase)
        assert parsed is not None, phrase
        assert parsed.get("retrieve") is True, phrase
        result = asyncio.run(run_file_goal(parsed))
        assert Path(str(result.get("path") or "")).name == name, phrase


def test_honest_miss_keeps_job_so_filename_can_follow(files_root: Path, monkeypatch) -> None:
    monkeypatch.setattr("app.ev.laptop_files._open_file", _opened)
    asyncio.run(run_file_goal(parse_file_goal("get me my ids")))
    missed = asyncio.run(run_file_goal(parse_file_goal("my government id")))
    assert missed.get("ok") is False
    assert "couldn't find" in str(missed.get("spoken") or "").lower()
    assert pending_retrieve() is not None
    (files_root / "Aadhaar.pdf").write_bytes(b"%PDF-1.4\n")
    named = parse_file_goal("aadhaar")
    assert named is not None
    found = asyncio.run(run_file_goal(named))
    assert Path(str(found.get("path") or "")).name == "Aadhaar.pdf"


def test_life_and_gear_are_not_retrieve() -> None:
    assert parse_retrieve_intent("get me my messages") is None
    assert looks_like_retrieve_task("get me my emails") is False
    assert looks_like_retrieve_task("where's my backpack") is False
    assert looks_like_file_task("get me my sandwich") is False


def test_find_resume_still_uses_existing_search() -> None:
    parsed = parse_file_goal("look up my resume on my laptop")
    assert parsed is not None
    assert parsed["action"] in {"search", "open"}
    assert "resume" in str(parsed.get("query") or "").lower()
