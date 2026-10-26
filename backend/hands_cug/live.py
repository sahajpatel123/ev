"""Live environment for CUG: GUI lock, fixture server, a PRIVATE Chrome instance, own Ghostty windows.

Rules (H-I3/H-I4): the shared GUI lock is held for the run; Chrome is only started if it is not
already running (so no owner tab can be touched); every window/tab created here is closed in
`close()`; the owner's Ghostty/Safari state is snapshotted before and after to prove no task had
effects outside its scope.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from app.fabric.skills.api import Hands
from app.fabric.skills.guilock import gui_lock
from app.fabric.skills.packs.browsers import ChromeDriver, Tab
from app.fabric.skills.packs.ghostty import GhosttyDriver, GhosttyError
from app.fabric.skills.runtime import SystemRuntime, process_running

from .fixture_server import FixtureServer

CHROME_EXE = "Google Chrome.app/Contents/MacOS/Google Chrome"


class LiveEnv:
    def __init__(self, *, chrome: bool = True, ghostty: bool = False, purpose: str = "CUG live"):
        self.rt = SystemRuntime(route="direct")
        self.want_chrome, self.want_ghostty, self.purpose = chrome, ghostty, purpose
        self.lock = None
        self.srv: FixtureServer | None = None
        self.chrome: ChromeDriver | None = None
        self.ghostty = GhosttyDriver(self.rt)
        self.profile = Path(tempfile.mkdtemp(prefix="hands-chrome-"))
        self.tmp = Path(tempfile.mkdtemp(prefix="cug-live-"))
        self.created_ghostty: list[str] = []
        self.opened: list[Tab] = []
        self.before: dict[str, Any] = {}
        self.hands: Hands | None = None

    # -- lifecycle --------------------------------------------------------------------
    def __enter__(self) -> LiveEnv:
        self._lock_cm = gui_lock("CLI4", self.purpose, wait_s=300)
        self._lock_cm.__enter__()
        self.srv = FixtureServer().start()
        self.before = self.foreign_snapshot()
        if self.want_chrome:
            self._start_chrome()
        self.hands = self.make_hands()
        return self

    def __exit__(self, *exc: Any) -> None:
        for wid in list(self.created_ghostty):
            with contextlib.suppress(GhosttyError):
                self.ghostty.close_window(wid)
        if self.chrome:
            self.rt.osascript('tell application "Google Chrome" to quit', timeout=15)
            time.sleep(1.5)
        if self.srv:
            self.srv.stop()
        self._lock_cm.__exit__(None, None, None)

    def make_hands(self, **kw: Any) -> Hands:
        browsers = [self.chrome] if self.chrome else []
        kw.setdefault("browsers", browsers)
        kw.setdefault("state_dir", self.tmp / "state")
        kw.setdefault("home", self.tmp / "home")
        return Hands(self.rt, **kw)

    def _start_chrome(self) -> None:
        if process_running(self.rt, CHROME_EXE):
            raise RuntimeError("Chrome is already running; refusing to touch someone else's browser")
        (self.profile / "Default").mkdir(parents=True, exist_ok=True)
        (self.profile / "Default" / "Preferences").write_text(
            json.dumps({"browser": {"allow_javascript_apple_events": True}}))
        subprocess.run(["open", "-na", "Google Chrome", "--args", f"--user-data-dir={self.profile}", "--no-first-run",
                        "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required",
                        "--window-size=900,700", "--window-position=820,380", "about:blank"], check=True)
        for _ in range(60):
            time.sleep(0.5)
            if process_running(self.rt, CHROME_EXE):
                break
        time.sleep(2.5)
        self.chrome = ChromeDriver(self.rt)

    # -- pages -----------------------------------------------------------------------------
    def open(self, page: str, *, new_window: bool = False, wait: float = 1.8, **q: Any) -> Tab:
        assert self.chrome and self.srv
        made = self.chrome.open_url(self.srv.url(page, **q), new_window=new_window)
        time.sleep(wait)
        tabs = self.chrome.tabs(window_ids=[made["window_id"]])
        tab = next(t for t in tabs if t.tab_id == made["tab_id"]) if made["tab_id"] else tabs[-1]
        self.opened.append(tab)
        return tab

    def close_tabs(self) -> None:
        assert self.chrome
        for t in list(self.opened):
            with contextlib.suppress(Exception):
                self.chrome.close_tab(t)
        self.opened.clear()
        time.sleep(0.3)

    def fixture(self, tab: Tab) -> dict[str, Any]:
        assert self.chrome
        out = self.chrome.js(tab, "document.documentElement.getAttribute('data-fixture')")
        try:
            return json.loads(out)
        except (ValueError, TypeError):
            return {}

    def attr(self, tab: Tab, name: str) -> str:
        assert self.chrome
        return self.chrome.js(tab, f"document.documentElement.getAttribute('{name}')")

    def js(self, tab: Tab, code: str) -> str:
        assert self.chrome
        return self.chrome.js(tab, code)

    # -- foreign state -----------------------------------------------------------------------
    def foreign_snapshot(self) -> dict[str, Any]:
        """Counts and a hash of ids/titles of windows that are NOT ours: Ghostty and Safari."""
        snap: dict[str, Any] = {}
        try:
            terms = [t for t in self.ghostty.inventory() if t.window_id not in self.created_ghostty]
            snap["ghostty"] = {"terminals": len(terms),
                               "hash": hashlib.sha256("|".join(sorted(t.terminal_id for t in terms)).encode()).hexdigest()[:12]}
        except GhosttyError:
            snap["ghostty"] = None
        if process_running(self.rt, "Safari.app/Contents/MacOS/Safari"):
            p = self.rt.osascript('tell application "Safari" to return (count of windows)', timeout=8)
            snap["safari_windows"] = (p.out or "").strip() if p.ok else None
        return snap

    def scope_violation(self) -> bool:
        now = self.foreign_snapshot()
        return now.get("ghostty") != self.before.get("ghostty") or now.get("safari_windows") != self.before.get("safari_windows")


def cpu_seconds(pid: int) -> float:
    p = subprocess.run(["ps", "-o", "cputime=", "-p", str(pid)], capture_output=True, text=True)
    t = p.stdout.strip()
    if not t:
        return 0.0
    parts = t.replace("-", ":").split(":")
    nums = [float(x) for x in parts]
    s = 0.0
    for x in nums:
        s = s * 60 + x
    return s
