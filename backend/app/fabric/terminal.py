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
from pathlib import Path


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


def _tmux(*args: str, timeout: int = 10, socket: str | None = None) -> subprocess.CompletedProcess:
    base = ["tmux"] + (["-L", socket] if socket else [])
    return subprocess.run([*base, *args], capture_output=True, text=True, timeout=timeout)


def has_tmux() -> bool:
    return shutil.which("tmux") is not None


class TmuxLayout:
    """2x2 (or NxM) pane layouts with per-pane cwd+command and read-back."""

    def __init__(self, root=None, socket: str | None = None):
        from pathlib import Path
        self.root = Path(str(root)) if root is not None else Path(".")
        self.socket = socket
        self.session: str | None = None

    def _t(self, *args: str, timeout: int = 10):
        return _tmux(*args, timeout=timeout, socket=self.socket)

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
        first_cwd = first.get("cwd") or str(self.root)
        self._t("new-session", "-d", "-s", name, "-c", first_cwd)
        # Split panes do NOT reliably inherit the session cwd (server cwd
        # leaks in), so every split carries an explicit -c.
        for extra in (panes[1:] if len(panes) > 1 else []):
            self._t("split-window", "-t", name, "-c", extra.get("cwd") or str(self.root))
            self._t("select-layout", "-t", name, "tiled")
            time.sleep(0.2)
        ids = _pane_ids(name, self.socket)
        # Top-up splits for panes beyond the grid loop (same count, safe).
        while len(ids) < len(panes):
            self._t("split-window", "-t", name, "-c", str(self.root))
            self._t("select-layout", "-t", name, "tiled")
            time.sleep(0.2)
            ids = _pane_ids(name, self.socket)
        ids = ids[:len(panes)]
        for pid, p in zip(ids, panes, strict=True):
            cmd = p.get("command", "")
            if cmd:
                self._t("send-keys", "-t", pid, cmd, "Enter")
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
        for pid in _pane_ids(self.session, self.socket):
            p = self._t("capture-pane", "-p", "-t", pid)
            out[pid] = p.stdout or ""
        return {"ok": True, "verified": True, "evidence": {"panes": out}, "error": None, "undo": None}

    def close(self) -> dict:
        if not self.session:
            return {"ok": False, "verified": True, "evidence": {}, "error": "no session", "undo": None}
        self._t("kill-session", "-t", self.session)
        alive = self._t("has-session", "-t", self.session).returncode == 0
        ok = not alive
        self.session = None if ok else self.session
        return {"ok": ok, "verified": ok, "evidence": {}, "error": None if ok else "still alive",
                "undo": None}

    def attach(self, session: str) -> dict:
        """Reattach a new handle to a live session (post-restart reads)."""
        alive = self._t("has-session", "-t", session).returncode == 0
        if not alive:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": "session not found", "undo": None}
        self.session = session
        return {"ok": True, "verified": True, "evidence": {"session": session},
                "error": None, "undo": None}

    def snapshot_live(self, session: str | None = None) -> dict:
        """Describe a LIVE tmux session as a layout spec (cwd per pane).

        Honest limit: tmux reports each pane's current process + cwd, not
        the originally typed command. Restoring a snapshot reopens shells
        in the same cwds; long-running commands are not resurrected.
        """
        target = session or self.session
        if not target:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": "no session", "undo": None}
        p = self._t("list-panes", "-t", target, "-F",
                    "#{pane_id}\t#{pane_current_path}\t#{pane_current_command}")
        if p.returncode != 0:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": "session not found", "undo": None}
        panes = []
        for line in p.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            _pid, cwd, cmd = parts
            panes.append({"cwd": cwd, "command": "", "running": cmd})
        return {"ok": True, "verified": True,
                "evidence": {"session": target, "panes": panes}, "error": None,
                "undo": None}


def _pane_ids(session: str, socket: str | None = None) -> list[str]:
    p = _tmux("list-panes", "-t", session, "-F", "#{pane_id}", socket=socket)
    if p.returncode != 0:
        return []
    return [line.strip() for line in p.stdout.splitlines() if line.strip()]


class GhosttyLayout:
    """Hybrid visible layout: one Ghostty window hosting one tmux session.

    Ghostty owns the visible surfaces (native window + native splits);
    tmux owns persistence, capture, and send-keys inside them. Panes are
    tmux panes (deterministic); `native_split` exercises Ghostty's own
    split verb for surfaces tmux cannot host.
    """

    def __init__(self, root=None, socket: str | None = None):
        from pathlib import Path
        self.root = Path(str(root)) if root is not None else Path(".")
        self.socket = socket
        self.window_id: str | None = None
        self.tmux = TmuxLayout(root=root, socket=socket)

    def open(self, panes: list[dict], confirm: bool = False) -> dict:
        from . import ghostty as g

        if not confirm:
            return self.tmux.plan(panes)
        probed = g.probe()["evidence"]
        if not probed.get("scriptable"):
            return {"ok": False, "verified": True, "evidence": {},
                    "error": "ghostty not scriptable", "undo": None}
        # tmux session first (headless, deterministic), then a window born
        # attached to it: no keystroke races, no shell-readiness waits.
        created = self.tmux.open(panes, confirm=True)
        if not created.get("ok"):
            return created
        self.tmux.session = created["evidence"]["session"]
        import shutil as _shutil

        # Absolute tmux path: the spawned shell runs --noprofile --norc
        # with a minimal PATH that lacks /opt/homebrew/bin.
        tmux_bin = _shutil.which("tmux") or "tmux"
        attach = (f"{tmux_bin} {'-L ' + self.socket + ' ' if self.socket else ''}"
                  f"attach -t {self.tmux.session}")
        win = g.GhosttyScripted().open_window(command=attach)
        if not win.get("ok"):
            self.tmux.close()
            return win
        self.window_id = win["evidence"]["window_id"]
        return {"ok": True, "verified": True,
                "evidence": {"window_id": self.window_id,
                             "session": self.tmux.session,
                             "panes": created["evidence"].get("panes", {})},
                "error": None, "undo": {"window_id": self.window_id,
                                        "session": self.tmux.session}}

    def native_split(self, direction: str = "right") -> dict:
        from . import ghostty as g

        if not self.window_id:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": "no window", "undo": None}
        return g.GhosttyScripted().split(self.window_id, direction)

    def read_panes(self) -> dict:
        return self.tmux.read_panes()

    def read_visible(self) -> dict:
        """Visible-surface read-back (Ghostty screen dump, slow path)."""
        from . import ghostty as g

        if not self.window_id:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": "no window", "undo": None}
        return g.GhosttyScripted().read_screen(self.window_id)

    def close(self) -> dict:
        from . import ghostty as g

        errs = []
        tmux_res = self.tmux.close()
        if not tmux_res.get("ok") and tmux_res.get("error") != "no session":
            errs.append(str(tmux_res.get("error")))
        win_ok = True
        if self.window_id:
            win_res = g.GhosttyScripted().close_window(self.window_id)
            win_ok = bool(win_res.get("ok"))
            if not win_ok:
                errs.append(str(win_res.get("error")))
            self.window_id = None
        ok = not errs
        return {"ok": ok, "verified": ok, "evidence": {},
                "error": None if ok else "; ".join(errs), "undo": None}


_LAYOUTS_DIR = ".ev-layouts"


def _layouts_dir(root) -> Path:
    d = Path(str(root)) / _LAYOUTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _layout_name_ok(name: str) -> bool:
    cleaned = (name or "").strip()
    return bool(cleaned) and "/" not in cleaned and cleaned not in {".", ".."} and len(cleaned) <= 80


def save_layout(name: str, spec: dict, root=None) -> dict:
    """Persist a named layout spec (JSON). Pure file write; needs confirm to open."""
    import json
    import time as _time

    if not _layout_name_ok(name):
        return {"ok": False, "verified": True, "evidence": {}, "error": "bad layout name", "undo": None}
    panes = (spec or {}).get("panes", [])
    if not isinstance(panes, list) or not panes:
        return {"ok": False, "verified": True, "evidence": {}, "error": "spec needs panes", "undo": None}
    try:
        path = _layouts_dir(root or ".") / f"{name.strip()}.json"
        path.write_text(json.dumps({"name": name.strip(), "panes": panes, "saved_at": _time.time()}))
        back = json.loads(path.read_text())
        ok = back.get("panes") == panes
        return {"ok": ok, "verified": ok, "evidence": {"name": name.strip(), "panes": len(panes)},
                "error": None if ok else "read-back mismatch", "undo": {"layout_file": str(path)}}
    except OSError as e:
        return {"ok": False, "verified": True, "evidence": {}, "error": str(e), "undo": None}


def list_layouts(root=None) -> dict:
    import json

    try:
        rows = []
        d = _layouts_dir(root or ".")
        for path in sorted(d.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            rows.append({"name": data.get("name", path.stem),
                         "panes": len(data.get("panes", []) or []),
                         "saved_at": data.get("saved_at")})
        return {"ok": True, "verified": True, "layouts": rows, "evidence": {"n": len(rows)},
                "error": None, "undo": None}
    except OSError as e:
        return {"ok": False, "verified": True, "evidence": {}, "error": str(e), "undo": None}


def load_layout(name: str, root=None) -> dict:
    import json

    if not _layout_name_ok(name):
        return {"ok": False, "verified": True, "evidence": {}, "error": "bad layout name", "undo": None}
    path = _layouts_dir(root or ".") / f"{name.strip()}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"ok": False, "verified": True, "evidence": {}, "error": "unknown layout", "undo": None}
    if not isinstance(data.get("panes"), list) or not data["panes"]:
        return {"ok": False, "verified": True, "evidence": {}, "error": "layout has no panes", "undo": None}
    return {"ok": True, "verified": True, "spec": data, "evidence": {"name": name.strip()},
            "error": None, "undo": None}
