"""S33: real Ghostty/tmux layout — open, echo, read back, restore."""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


def _ghostty_scriptable() -> bool:
    from app.fabric import ghostty as g

    try:
        return bool(g.probe()["evidence"].get("scriptable"))
    except Exception:
        return False


def _ensure_ghostty_running() -> None:
    running = subprocess.run(["pgrep", "-x", "Ghostty"], capture_output=True)
    if running.returncode != 0:
        subprocess.run(["open", "-a", "Ghostty"], capture_output=True, timeout=30)
        time.sleep(3)


needs_ghostty = pytest.mark.skipif(
    not _ghostty_scriptable(), reason="Ghostty scriptable app required")
needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux required")


def _await_window_count(driver, expected: int, timeout: float = 8.0) -> int:
    """Poll the window count (close animations settle asynchronously)."""
    deadline = time.time() + timeout
    seen = -1
    while time.time() < deadline:
        seen = driver.list_windows()["evidence"]["n"]
        if seen == expected:
            return seen
        time.sleep(0.5)
    return seen


def test_s33_probe_records_surface() -> None:
    from app.fabric import ghostty as g

    found = g.probe()["evidence"]
    assert found["bundle_present"] is True
    assert found["sdef_present"] is True
    assert found["scriptable"] is True
    assert isinstance(found["windows"], int)


@needs_ghostty
def test_s33_native_window_split_input_read_close() -> None:
    from app.fabric import ghostty as g

    _ensure_ghostty_running()
    driver = g.GhosttyScripted()
    before = driver.list_windows()["evidence"]["n"]
    marker = f"S33-{uuid.uuid4().hex[:6]}"
    win = driver.open_window()
    assert win["ok"], win
    wid = win["evidence"]["window_id"]
    try:
        assert driver.terminals(wid)["evidence"]["n"] == 1
        split = driver.split(wid, "right")
        assert split["ok"], split
        assert driver.terminals(wid)["evidence"]["n"] == 2
        assert driver.input_text(wid, f"echo {marker}")["ok"]
        time.sleep(1.5)
        screen = driver.read_screen(wid)
        assert screen["ok"], screen
        assert marker in screen["evidence"]["text"], screen["evidence"]["text"][:300]
    finally:
        closed = driver.close_window(wid)
        assert closed["ok"], closed
    assert _await_window_count(driver, before) == before, \
        "must close exactly the windows it opened"


@needs_tmux
def test_s33_tmux_layout_isolated_server(tmp_path) -> None:
    from app.fabric.terminal import TmuxLayout

    sock = f"ev-s33-{uuid.uuid4().hex[:6]}"
    marker_a, marker_b = "S33A", "S33B"
    try:
        layout = TmuxLayout(root=tmp_path, socket=sock)
        opened = layout.open(
            [{"command": f"echo {marker_a}"}, {"command": f"echo {marker_b}"}],
            confirm=True)
        assert opened["ok"], opened
        time.sleep(1.5)
        panes = layout.read_panes()
        assert panes["ok"], panes
        blob = " ".join(panes["evidence"]["panes"].values())
        assert marker_a in blob and marker_b in blob, blob[:400]
        # A fresh handle reattaches by session name (restart survival).
        reattached = TmuxLayout(root=tmp_path, socket=sock)
        assert reattached.attach(opened["evidence"]["session"])["ok"]
        assert marker_a in " ".join(reattached.read_panes()["evidence"]["panes"].values())
        # Live snapshot describes the workspace honestly.
        snap = layout.snapshot_live()
        assert snap["ok"] and len(snap["evidence"]["panes"]) == 2
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True, timeout=15)


@needs_tmux
def test_s33_restore_after_kill_server(tmp_path) -> None:
    from app.fabric import terminal as term

    sock = f"ev-s33r-{uuid.uuid4().hex[:6]}"
    try:
        spec = {"panes": [{"command": "echo RESTORED-1", "cwd": str(tmp_path)},
                          {"command": "echo RESTORED-2", "cwd": str(tmp_path)}]}
        assert term.save_layout("s33r", spec, root=tmp_path)["ok"]
        first = term.TmuxLayout(root=tmp_path, socket=sock)
        assert first.open(spec["panes"], confirm=True)["ok"]
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True,
                       timeout=15, check=True)
        assert term._pane_ids(first.session or "", sock) == []
        loaded = term.load_layout("s33r", root=tmp_path)
        assert loaded["ok"]
        second = term.TmuxLayout(root=tmp_path, socket=sock)
        reopened = second.open(loaded["spec"]["panes"], confirm=True)
        assert reopened["ok"], reopened
        time.sleep(1.5)
        blob = " ".join(second.read_panes()["evidence"]["panes"].values())
        assert "RESTORED-1" in blob and "RESTORED-2" in blob, blob[:400]
        second.close()
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True, timeout=15)


def test_s33_resume_registry(tmp_path) -> None:
    from app.fabric import resume, terminal

    assert terminal.save_layout("standup-am", {"panes": [{"command": "echo x"}]},
                                root=tmp_path)["ok"]
    assert terminal.save_layout("standup-pm", {"panes": [{"command": "echo y"}]},
                                root=tmp_path)["ok"]
    assert resume.resolve("resume standup-am", root=tmp_path)["evidence"]["layout"] == "standup-am"
    assert resume.set_alias("morning", "standup-am", root=tmp_path)["ok"]
    assert resume.resolve("morning", root=tmp_path)["evidence"]["layout"] == "standup-am"
    ambiguous = resume.resolve("standup", root=tmp_path)
    assert ambiguous["ok"] is False and ambiguous["error"] == "ambiguous"
    unknown = resume.resolve("resume nope", root=tmp_path)
    assert unknown["ok"] is False and unknown["error"] == "unknown layout"


@pytest.fixture()
def fabric_on(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr("app.fabric.roots._home", lambda: home)
    from app.fabric import panic

    panic.clear()
    return home


async def test_s33_dispatch_backend_selection(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    bad = await dispatch(db_session, "terminal_layout",
                         {"op": "plan", "panes": [], "backend": "nope"},
                         actor="master", allow_sensitive=True)
    assert bad.result["ok"] is False


@needs_ghostty
async def test_s33_dispatch_ghostty_backend_live_window(
    db_session: AsyncSession, fabric_on
) -> None:
    from app.ev.tools import dispatch
    from app.fabric import ghostty as g

    _ensure_ghostty_running()
    driver = g.GhosttyScripted()
    before = driver.list_windows()["evidence"]["n"]
    opened = await dispatch(
        db_session, "terminal_layout",
        {"op": "open", "backend": "ghostty",
         "panes": [{"command": "echo DISPATCH-GHOSTTY"}]},
        actor="master", allow_sensitive=True)
    assert opened.result["ok"], opened.result
    wid = opened.result["evidence"]["window_id"]
    session = opened.result["evidence"]["session"]
    try:
        time.sleep(2.0)
        read = await dispatch(
            db_session, "terminal_layout",
            {"op": "read", "backend": "ghostty", "session": session},
            actor="master", allow_sensitive=True)
        assert read.result["ok"], read.result
        blob = " ".join(read.result["evidence"]["panes"].values())
        assert "DISPATCH-GHOSTTY" in blob, blob[:400]
    finally:
        closed = await dispatch(
            db_session, "terminal_layout",
            {"op": "close", "backend": "ghostty", "session": session,
             "window_id": wid},
            actor="master", allow_sensitive=True)
        assert closed.result["ok"], closed.result
    assert _await_window_count(driver, before) == before
