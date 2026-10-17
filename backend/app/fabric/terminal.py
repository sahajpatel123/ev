"""Terminal bridge: backend probe + tmux layout lane.

Ghostty is canonical per D2, but scripting surface is probed empirically
(see probe_ghostty); tmux is the persistence/addressing layer that works
everywhere. Layout restore shows the pane->command plan and needs ONE confirm.
"""
from __future__ import annotations
import shutil
import subprocess
import time
import uuid


def probe_ghostty() -> dict:
    """Empirical Ghostty scripting surface. No assumptions, only observations."""
    found = {
        "cli": shutil.which("ghostty"),
        "app_bundle": "/Applications/Ghostty.app",
        "version": None,
        "sdef": None,
        "url_scheme": None,
    }
    import os
    found["bundle_present"] = os.path.exists(found["app_bundle"])
    if found["cli"]:
        try:
            p = subprocess.run([found["cli"], "--version"], capture_output=True, text=True, timeout=5)
            found["version"] = (p.stdout or p.stderr or "").strip()[:200]
        except Exception as e:
            found["version"] = f"probe failed: {e}"
    # AppleScript dictionary?
    try:
        p = subprocess.run(["sdef", "/Applications/Ghostty.app"], capture_output=True, text=True, timeout=10)
        if p.returncode == 0 and p.stdout.strip():
            found["sdef"] = "present"
        else:
            found["sdef"] = "absent (no scriptable dictionary)"
    except Exception as e:
        found["sdef"] = f"probe failed: {e}"
    return {"ok": True, "verified": True, "evidence": found, "error": None, "undo": None}


def _tmux(*args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=timeout)


def has_tmux() -> bool:
    return shutil.which("tmux") is not None


class TmuxLayout:
    """2x2 (or NxM) pane layouts with per-pane cwd+command and read-back."""

    def __init__(self, root=None):
        from pathlib import Path
        self.root = Path(str(root)) if root is not None else Path(".")
        self.session: str | None = None

    def plan(self, panes: list[dict]) -> dict:
        """Show the exact pane->command plan. Pure; needs confirm to execute."""
        rows = [{"pane": i, "cwd": p.get("cwd", ""), "command": p.get("command", ""),
                 "title": p.get("title", "")} for i, p in enumerate(panes)]
        return {"ok": True, "verified": True, "evidence": {"plan": rows, "needs_confirm": True},
                "error": None, "undo": None}

    def open(self, panes: list[dict], confirm: bool = False) -> dict:
        if not has_tmux():
            return {"ok": False, "verified": True, "evidence": {},
                    "error": "tmux not installed", "undo": None}
        if not confirm:
            r = self.plan(panes)
            r["evidence"]["opened"] = False
            return r
        name = f"ev-layout-{uuid.uuid4().hex[:8]}"
        first = panes[0] if panes else {}
        _tmux("new-session", "-d", "-s", name, "-c", first.get("cwd", str(self.root)))
        # split into len(panes) panes (supports up to 4 in a 2x2 grid)
        ids = _pane_ids(name)
        while len(ids) < len(panes):
            _tmux("split-window", "-t", name)
            _tmux("select-layout", "-t", name, "tiled")
            time.sleep(0.2)
            ids = _pane_ids(name)
        ids = ids[:len(panes)]
        for pid, p in zip(ids, panes):
            cmd = p.get("command", "")
            if p.get("cwd"):
                _tmux("send-keys", "-t", pid, f"cd {p['cwd']}", "Enter")
                time.sleep(0.2)
            if cmd:
                _tmux("send-keys", "-t", pid, cmd, "Enter")
        self.session = name
        panes_back = self.read_panes()
        ok = panes_back["ok"]
        return {"ok": ok, "verified": panes_back.get("verified", False),
                "evidence": {"session": name, "panes": panes_back.get("evidence", {}).get("panes", {})},
                "error": None if ok else panes_back.get("error"), "undo": {"session": name}}

    def read_panes(self) -> dict:
        if not self.session:
            return {"ok": False, "verified": True, "evidence": {}, "error": "no session", "undo": None}
        out: dict[str, str] = {}
        for pid in _pane_ids(self.session):
            p = _tmux("capture-pane", "-p", "-t", pid)
            out[pid] = p.stdout or ""
        return {"ok": True, "verified": True, "evidence": {"panes": out}, "error": None, "undo": None}

    def close(self) -> dict:
        if not self.session:
            return {"ok": False, "verified": True, "evidence": {}, "error": "no session", "undo": None}
        _tmux("kill-session", "-t", self.session)
        alive = _tmux("has-session", "-t", self.session).returncode == 0
        ok = not alive
        self.session = None if ok else self.session
        return {"ok": ok, "verified": ok, "evidence": {}, "error": None if ok else "still alive",
                "undo": None}


def _pane_ids(session: str) -> list[str]:
    p = _tmux("list-panes", "-t", session, "-F", "#{pane_id}")
    if p.returncode != 0:
        return []
    return [line.strip() for line in p.stdout.splitlines() if line.strip()]
