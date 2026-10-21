"""Terminal continuity: screenshot -> resumed Ghostty sessions, all local."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from app.ev.terminal_continuity import (
    extract_resume_commands,
    find_terminal_screenshots,
    looks_like_terminal_resume,
    resume_from_screenshot,
    resume_named_sessions,
)

OCR_SAMPLE = """~ ❯ opencode run ~/Code/ev
~/Code/ev ❯ Muse --continue
$ echo hello
"""


def test_extract_resume_commands_finds_cli_invocations() -> None:
    commands = extract_resume_commands(OCR_SAMPLE)
    assert "opencode run ~/Code/ev" in commands
    assert "Muse --continue" in commands
    assert all("echo" not in command for command in commands)


def test_extract_resume_commands_dedupes_and_bounds() -> None:
    text = ("Muse --continue\n" * 20) + ("opencode x\n" * 20)
    commands = extract_resume_commands(text, limit=3)
    assert len(commands) <= 3
    assert len(set(commands)) == len(commands)


def test_classifier_matches_resume_ask() -> None:
    assert looks_like_terminal_resume(
        "launch my ghostty and run those commands to resume my continuity session"
    )
    assert looks_like_terminal_resume("I have a screenshot of ghostty sessions")
    assert not looks_like_terminal_resume("what is the weather today")


def test_find_screenshots_prefers_newest(tmp_path) -> None:
    old = tmp_path / "old.png"
    new = tmp_path / "new.png"
    old.write_bytes(b"o")
    new.write_bytes(b"n")
    import os

    os.utime(old, (1, 1))
    shots = find_terminal_screenshots(str(tmp_path))
    assert shots[0].endswith("new.png")


class _Ghostty:
    def __init__(self) -> None:
        self.typed: list[tuple[str, str]] = []

    def open_window(self, **kwargs):
        return {"ok": True, "evidence": {"window_id": "w1"}}

    def input_text(self, window_id: str, text: str, **kwargs):
        self.typed.append((window_id, text))
        return {"ok": True, "evidence": {"sent_bytes": len(text)}}

    def read_screen(self, window_id: str, **kwargs):
        return {"ok": True, "evidence": {"text": "$ ", "chars": 2}}


@pytest.mark.asyncio
async def test_resume_from_screenshot_end_to_end(monkeypatch, tmp_path) -> None:
    import app.fabric.ghostty as ghostty_module
    import app.fabric.screen_pipeline as pipeline_module

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png")
    monkeypatch.setattr(
        ghostty_module, "probe", lambda: {"ok": True, "evidence": {"bundle_present": True}}
    )
    fake = _Ghostty()
    monkeypatch.setattr(ghostty_module, "GhosttyScripted", lambda *a, **k: fake)

    async def fake_ocr(path):
        return {"ok": True, "evidence": {"text": OCR_SAMPLE}}

    monkeypatch.setattr(pipeline_module, "ocr_path", fake_ocr)
    result = await resume_from_screenshot(str(shot))
    assert result["ok"] is True
    assert len(result["sessions"]) == 2
    assert all(item["typed"] for item in result["sessions"])
    assert [text for _, text in fake.typed] == [
        "opencode run ~/Code/ev",
        "Muse --continue",
    ]


@pytest.mark.asyncio
async def test_resume_without_ghostty_is_honest(monkeypatch, tmp_path) -> None:
    import app.fabric.ghostty as ghostty_module

    monkeypatch.setattr(
        ghostty_module, "probe", lambda: {"ok": True, "evidence": {"bundle_present": False}}
    )
    result = await resume_named_sessions(["Muse --continue"])
    assert result["ok"] is False
    assert result["error"] == "ghostty_missing"


@pytest.mark.asyncio
async def test_empty_ocr_is_honest(monkeypatch, tmp_path) -> None:
    import app.fabric.ghostty as ghostty_module
    import app.fabric.screen_pipeline as pipeline_module

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"fake-png")
    monkeypatch.setattr(
        ghostty_module, "probe", lambda: {"ok": True, "evidence": {"bundle_present": True}}
    )

    async def fake_ocr(path):
        return {"ok": True, "evidence": {"text": "no commands here at all"}}

    monkeypatch.setattr(pipeline_module, "ocr_path", fake_ocr)
    result = await resume_from_screenshot(str(shot))
    assert result["ok"] is False
    assert result["error"] == "no_resume_commands"


# --------------------------------------------------------------------------- #
# Resume-attach: address the owner's live tmux sessions by name
# --------------------------------------------------------------------------- #

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


def _isolated_registry(monkeypatch, tmp_path: Path) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "agent_state_db", str(tmp_path / "tasks.db"))


def _scratch_session() -> str:
    name = f"ev-attach-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["tmux", "new-session", "-d", "-s", name], check=True, timeout=15)
    return name


def _kill_session(name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", name], timeout=15,
                   capture_output=True)


@needs_tmux
def test_entry_for_attaches_to_live_tmux_session(monkeypatch, tmp_path) -> None:
    from app.agent import local_tools

    _isolated_registry(monkeypatch, tmp_path)
    name = _scratch_session()
    try:
        entry = local_tools._entry_for({"session": name})
        assert entry is not None
        assert entry["kind"] == "tmux"
        assert entry["session"] == name
        again = local_tools._entry_for({"terminal_id": entry["id"]})
        assert again is not None and again["session"] == name
    finally:
        _kill_session(name)


@needs_tmux
def test_entry_for_missing_session_returns_none(monkeypatch, tmp_path) -> None:
    from app.agent import local_tools

    _isolated_registry(monkeypatch, tmp_path)
    assert local_tools._entry_for({"session": "ev-no-such-session-xyz"}) is None


@pytest.mark.asyncio
@needs_tmux
async def test_terminal_run_and_read_on_attached_session(monkeypatch, tmp_path) -> None:
    from app.agent import local_tools
    from app.agent.tools import ToolContext

    _isolated_registry(monkeypatch, tmp_path)
    name = _scratch_session()
    try:
        ctx = ToolContext(root=tmp_path, approved=True, run_id="test")
        marker = f"EVIE-ATTACH-{uuid.uuid4().hex[:6]}"
        ran = await local_tools._terminal_run(
            {"session": name, "command": f"echo {marker}", "wait_s": 1.0, "lines": 20},
            ctx,
        )
        assert ran["ok"] is True, ran
        assert marker in str(ran["result"]["output"])
        read = await local_tools._terminal_read({"session": name, "lines": 20}, ctx)
        assert read["ok"] is True
        assert marker in str(read["result"]["output"])
    finally:
        _kill_session(name)


@pytest.mark.asyncio
async def test_terminal_open_attach_resumes_visibly(monkeypatch, tmp_path) -> None:
    from app.agent import local_tools
    from app.agent.tools import ToolContext
    from app.fabric import terminal as terminal_module

    _isolated_registry(monkeypatch, tmp_path)
    calls: list[str] = []

    class _Layout:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.window_id = ""

        def attach(self, session: str):
            calls.append(session)
            self.window_id = "w9"
            return {"ok": True, "verified": True,
                    "evidence": {"window_id": "w9", "session": session},
                    "error": None, "undo": None}

    monkeypatch.setattr(terminal_module, "GhosttyLayout", _Layout)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tmux")
    ctx = ToolContext(root=tmp_path, approved=True, run_id="test")
    out = await local_tools._terminal_open({"attach": "opencode"}, ctx)
    assert out["ok"] is True, out
    assert out["result"]["resumed"] is True
    assert out["result"]["session"] == "opencode"
    assert calls == ["opencode"]
    entry = local_tools._entry_for({"terminal_id": out["result"]["terminal_id"]})
    assert entry is not None and entry["kind"] == "ghostty"


@needs_tmux
def test_ghostty_layout_attach_missing_session_is_honest() -> None:
    from app.fabric.terminal import GhosttyLayout

    out = GhosttyLayout(root="/tmp").attach("ev-no-such-session-xyz")
    assert out["ok"] is False
    assert out["error"] == "session not found"
