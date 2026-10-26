# mypy: ignore-errors
# Test doubles: deliberately loose, never imported by production code.
"""Test doubles for the HANDS runtime: a scripted machine with a virtual clock.

`FakeRuntime` answers osascript/process calls from ordered rules (first match
wins), records every call, and never sleeps for real: `sleep()` advances a
virtual clock so countdown/ad timing tests run instantly and deterministically.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .runtime import Proc

Responder = Proc | str | Callable[["FakeRuntime", str], Proc | str]


class FakeRuntime:
    def __init__(self, home: Path | None = None, *, start: float = 1_000_000.0):
        self._rules: list[tuple[re.Pattern[str], Responder, str]] = []
        self.calls: list[dict[str, Any]] = []
        self._clock = start
        self._home = home or Path("/nonexistent-home")
        self.binaries: dict[str, str] = {}
        self.on_tick: list[Callable[[float], None]] = []
        self.sys: dict[str, Any] = {"locked": False, "display_asleep": False, "idle_s": 3.0,
                                    "on_console": True}

    # -- scripting -----------------------------------------------------------
    def when(self, pattern: str, response: Responder, *, kind: str = "osascript") -> FakeRuntime:
        """kind: osascript | run. `pattern` is a regex searched in the script/argv text."""
        self._rules.append((re.compile(pattern, re.S), response, kind))
        return self

    def _answer(self, kind: str, text: str) -> Proc:
        for rx, response, rule_kind in self._rules:
            if rule_kind == kind and rx.search(text):
                value = response(self, text) if callable(response) else response
                if isinstance(value, Proc):
                    return value
                return Proc(True, out=str(value))
        return Proc(False, err=f"fake: no rule for {kind}: {text[:120]}", code=1)

    # -- Runtime protocol ----------------------------------------------------
    def osascript(self, script: str, *, lang: str = "applescript",
                  timeout: float = 20.0, argv: list[str] | None = None) -> Proc:
        self.calls.append({"kind": "osascript", "lang": lang, "script": script,
                           "argv": list(argv or []), "t": self._clock})
        return self._answer("osascript", script)

    def run(self, argv: list[str], *, timeout: float = 20.0,
            input: str | None = None) -> Proc:
        self.calls.append({"kind": "run", "argv": list(argv), "input": input,
                           "t": self._clock})
        return self._answer("run", " ".join(argv))

    def sleep(self, seconds: float) -> None:
        self._clock += max(0.0, seconds)
        for fn in list(self.on_tick):
            fn(self._clock)

    def monotonic(self) -> float:
        return self._clock

    def now(self) -> float:
        return self._clock

    def home(self) -> Path:
        return self._home

    def which(self, name: str) -> str | None:
        return self.binaries.get(name)

    def system_state(self) -> dict[str, Any]:
        return dict(self.sys)

    # -- assertions ----------------------------------------------------------
    def scripts(self, pattern: str = "") -> list[str]:
        rx = re.compile(pattern, re.S)
        return [c["script"] for c in self.calls if c["kind"] == "osascript"
                and rx.search(c["script"])]

    def ran(self, pattern: str) -> bool:
        rx = re.compile(pattern, re.S)
        return any(rx.search(c.get("script") or " ".join(c.get("argv") or []))
                   for c in self.calls)


# ------------------------------------------------------------------ fakes ----

HELP = {
    "claude": ("Claude Code - starts an interactive session by default\n"
               "  --resume <session-id>   Resume a conversation by session ID\n"
               "  -c, --continue          Continue the most recent conversation\n"),
    "codex": ("Commands:\n  exec    Run non-interactively\n  resume  Resume a previous interactive session\n"
              "  fork    Fork a previous interactive session\n"),
    "codex resume": "Usage: codex resume [OPTIONS] [SESSION_ID]\n  SESSION_ID  Conversation/session id (UUID) or name\n",
    "opencode": ("  --continue, -c          Continue the last session\n"
                 "  --session, -s string    Session ID to continue, or to create if it does not exist\n"),
    "muse": "Commands:\n  resume  Resume a previous session (--last or <session-ref>)\n  exec  Run one prompt\n",
    "muse resume": "muse resume <session-ref>\n<session-ref> is an exact session UUID or Session Name.\n",
    "gemini": ("  -r, --resume   Resume a previous session. Use \"latest\" for most recent or "
               "index number (e.g. --resume 5)  [string]\n"),
}


def fake_cli_runtime(home: Path, *, installed: tuple[str, ...] = ("claude", "codex", "opencode", "muse", "gemini"),
                     help_overrides: dict[str, str] | None = None) -> FakeRuntime:
    """A FakeRuntime where the named CLIs exist and print realistic `--help`."""
    rt = FakeRuntime(home=home)
    texts = dict(HELP)
    texts.update(help_overrides or {})
    for name in installed:
        rt.binaries[name] = f"/usr/local/bin/{name}"

    def help_for(_rt: FakeRuntime, text: str) -> Proc:
        parts = text.split()
        key = " ".join(p for p in parts[1:] if p != "--help")
        exe = parts[0].rsplit("/", 1)[-1]
        full = f"{exe} {key}".strip()
        return Proc(True, out=texts.get(full, ""))

    rt.when(r"/usr/local/bin/\w+( resume)? --help", help_for, kind="run")
    rt.when(r"/usr/local/bin/\w+ --version", lambda _r, t: Proc(True, out=f"{t.split()[0].rsplit('/',1)[-1]} 9.9.9"), kind="run")
    return rt


def hex_uuid(rng, *, v7: bool = False) -> str:
    h = "".join(rng.choice("0123456789abcdef") for _ in range(32))
    if v7:
        h = "01a1" + h[4:]
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def make_fake_home(root: Path, *, seed: int = 7, per_cli: int = 6, work_root: Path | None = None) -> dict[str, Any]:
    """Create fake session stores in the real on-disk formats; returns ground truth.

    Includes a deliberately confusable pair per CLI: two real ids that differ
    only at one hex-confusable position (6/b), so an OCR read of that position
    is genuinely ambiguous and must be flagged, never guessed.
    """
    import json
    import random
    import sqlite3

    rng = random.Random(seed)
    truth: dict[str, list[dict[str, Any]]] = {"claude": [], "codex": [], "opencode": [], "muse": []}
    names = ["Code/ev", "Code/orb", "Projects/api", "Code/presence", "Desktop/notes", "Code/site"]
    if work_root is not None:  # real directories, so grounded sessions can actually be started
        work = [str(Path(work_root) / n) for n in names]
        for w in work:
            Path(w).mkdir(parents=True, exist_ok=True)
    else:
        work = ["/Users/test/" + n for n in names]
    base = 1_790_000_000.0

    def confusable_pair(v7: bool) -> tuple[str, str]:
        a = hex_uuid(rng, v7=v7)
        i = next(k for k, ch in enumerate(a) if ch not in "-" and k > 12)
        a = a[:i] + "6" + a[i + 1:]
        return a, a[:i] + "b" + a[i + 1:]

    # claude: ~/.claude/projects/<encoded cwd>/<uuid>.jsonl
    cdir = root / ".claude" / "projects"
    ids = [hex_uuid(rng) for _ in range(per_cli - 2)] + list(confusable_pair(False))
    for n, sid in enumerate(ids):
        cwd = work[n % len(work)]
        d = cdir / cwd.replace("/", "-")
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{sid}.jsonl"
        rows = [{"type": "system", "cwd": cwd, "sessionId": sid},
                {"type": "user", "cwd": cwd, "sessionId": sid,
                 "message": {"role": "user", "content": f"task {n} in {cwd.rsplit('/', 1)[-1]}"}}]
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        import os
        os.utime(f, (base + n, base + n))
        truth["claude"].append({"id": sid, "cwd": cwd})

    # codex: ~/.codex/sessions/Y/M/D/rollout-<ts>-<uuid>.jsonl + session_index.jsonl
    xdir = root / ".codex" / "sessions" / "2026" / "10" / "20"
    xdir.mkdir(parents=True, exist_ok=True)
    ids = [hex_uuid(rng, v7=True) for _ in range(per_cli - 2)] + list(confusable_pair(True))
    index = []
    for n, sid in enumerate(ids):
        cwd = work[(n + 1) % len(work)]
        f = xdir / f"rollout-2026-10-20T10-0{n}-00-{sid}.jsonl"
        f.write_text(json.dumps({"type": "session_meta",
                                 "payload": {"id": sid, "cwd": cwd}}) + "\n")
        import os
        os.utime(f, (base + 100 + n, base + 100 + n))
        index.append({"id": sid, "thread_name": f"codex task {n}", "updated_at": "2026-10-20"})
        truth["codex"].append({"id": sid, "cwd": cwd})
    (root / ".codex" / "session_index.jsonl").write_text(
        "\n".join(json.dumps(r) for r in index) + "\n")

    # opencode: sqlite session table, ids ses_<26 alnum>
    odb = root / ".local" / "share" / "opencode"
    odb.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(odb / "opencode.db")
    con.execute("create table session (id text primary key, directory text, title text, "
                "time_updated integer, time_archived integer)")
    alnum = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for n in range(per_cli):
        sid = "ses_" + "".join(rng.choice(alnum) for _ in range(26))
        cwd = work[(n + 2) % len(work)]
        con.execute("insert into session values (?,?,?,?,NULL)",
                    (sid, cwd, f"oc task {n}", int((base + 200 + n) * 1000)))
        truth["opencode"].append({"id": sid, "cwd": cwd})
    con.commit()
    con.close()

    # muse: sqlite sessions table
    mdb = root / ".local" / "share" / "muse"
    (mdb / "session-admission-locks").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(mdb / "session-index.db")
    con.execute("create table sessions (session_id text, workspace_root text, title text, "
                "session_name text, updated_at_us integer)")
    ids = [hex_uuid(rng, v7=True) for _ in range(per_cli - 2)] + list(confusable_pair(True))
    for n, sid in enumerate(ids):
        cwd = work[(n + 3) % len(work)]
        con.execute("insert into sessions values (?,?,?,?,?)",
                    (sid, cwd, f"muse task {n}", None, int((base + 300 + n) * 1e6)))
        truth["muse"].append({"id": sid, "cwd": cwd})
    con.commit()
    con.close()
    return truth


class FakeGhosttyDriver:
    """A Ghostty stand-in that records what a plan would build and serves read-back."""

    def __init__(self, rt: FakeRuntime | None = None):
        self.rt = rt or FakeRuntime()
        self.builds: list[dict[str, Any]] = []
        self.screens: dict[str, str] = {}
        self.cwds: dict[str, str] = {}
        self.fail_build: str | None = None
        self.ps_lines: list[str] = []
        self.start_processes = True

        def ps(_rt: FakeRuntime, _text: str) -> Proc:
            return Proc(True, out="\n".join(self.ps_lines) + "\n")

        # processes the fake panes "started" show up in `ps`, so a started session can be verified
        self.rt._rules.insert(0, (re.compile(r"^ps -axo pid=,tty=,command="), ps, "run"))

    def front_window(self) -> str | None:
        return "tab-group-front"

    def build(self, tree, leaves, *, window_id=None, new_window=False, set_ratios=True,
              ratio_tol=0.02):
        if self.fail_build:
            raise RuntimeError(self.fail_build)
        from . import layout_tree as lt

        order = lt.panes(tree)
        terms = {str(p["id"]): f"TERM-{len(self.builds)}-{p['id']}" for p in order}
        for p, leaf in zip(order, leaves, strict=True):
            tid = terms[str(p["id"])]
            self.cwds[tid] = leaf.get("cwd", "")
            cmd = leaf.get("command") or ""
            self.screens[tid] = f"$ {cmd}\n" if cmd else "$ \n"
            if cmd and self.start_processes:
                self.ps_lines.append(f"  {1000 + len(self.ps_lines)} ttys00{len(self.ps_lines)} {cmd.split(';')[-1].strip()}")
        rec = {"tree": tree, "leaves": leaves, "window_id": window_id, "new_window": new_window}
        self.builds.append(rec)
        return {"window_id": window_id or "tab-group-new", "tab_id": f"tab-{len(self.builds)}",
                "opened": "window" if new_window or not window_id else "tab", "terminals": terms,
                "audit": {"method": "ax", "ok": True, "max_err": 0.004, "rounds": 1}}

    def inventory(self, *, only_windows=None):
        from .packs.ghostty import Term

        return [Term("tab-group-new", "tab-1", 1, True, tid, cwd, "t") for tid, cwd in self.cwds.items()]

    def read_text(self, terminal_id: str) -> str:
        return self.screens.get(terminal_id, "")


# ------------------------------------------------------------ browser/page sim --

class PageSim:
    """A media page with an ad queue, driven by the runtime's virtual clock.

    Models what the fixture page does: ads play for `len` seconds; a skippable ad
    shows a countdown for `skip_after` seconds, then a skip control; a click on a
    ready control ends the ad `slow` seconds later (a page may ignore untrusted
    clicks). Returns exactly the JSON shapes the real page scripts return.
    """

    def __init__(self, rt: FakeRuntime, ads: list[dict[str, Any]] | None = None, *, content_len: int = 120,
                 playing: bool = True, trusted_only: bool = False, slow: float = 0.0, hidden: bool = False):
        self.rt = rt
        self.queue = [dict(a) for a in (ads or [])]
        self.total = len(self.queue)
        self.idx = -1
        self.cur: dict[str, Any] | None = None
        self.started = 0.0
        self.ending_at: float | None = None
        self.content_len = content_len
        self.content_t = 0.0
        self.playing = playing
        self.trusted_only = trusted_only
        self.slow = slow
        self.armed: dict[str, Any] | None = None
        self.clicks = 0
        self.rejected = 0
        self.skipped = 0
        self.clicked_at: float | None = None
        self.skip_available_at: float | None = None
        self.volume, self.muted, self.rate, self.paused = 1.0, False, 1.0, not playing
        self.captions = False
        self.last = rt.now()
        self.events: list[tuple[float, str]] = []
        if self.queue:
            self._next_ad(rt.now())

    # -- timeline ------------------------------------------------------------
    def _next_ad(self, now: float) -> None:
        if not self.queue:
            self.cur = None
            self.ending_at = None
            return
        self.cur = self.queue.pop(0)
        self.idx += 1
        self.started = now
        self.ending_at = None
        self.skip_available_at = (now + self.cur["skip_after"]) if self.cur["kind"] == "S" else None
        self.events.append((now, f"ad-start {self.idx}"))

    def _click(self, at: float, trusted: bool) -> bool:
        self.clicks += 1
        if not self.cur or self.cur["kind"] != "S" or self.skip_available_at is None or at < self.skip_available_at:
            return False
        if self.trusted_only and not trusted:
            self.rejected += 1
            return False
        self.skipped += 1
        self.clicked_at = at
        self.ending_at = at + self.slow
        self.events.append((at, "skip-click"))
        return True

    def advance(self) -> None:
        now = self.rt.now()
        while True:
            if self.armed and self.cur and self.cur["kind"] == "S" and self.skip_available_at is not None \
                    and not self.ending_at and len(self.armed["log"]) < self.armed["max"]:
                t = max(self.armed["at"], self.skip_available_at)
                if t <= now and t <= self.armed["deadline"]:
                    self._click(t, trusted=False)
                    self.armed["log"].append(t)
                    continue
            if self.cur:
                end = self.ending_at if self.ending_at is not None else self.started + self.cur["len"]
                if end <= now:
                    self.events.append((end, "ad-end"))
                    self._next_ad(end)
                    continue
            break
        if not self.cur and not self.paused:
            self.content_t += max(0.0, now - self.last) * self.rate
        self.last = now

    # -- page script entry points ---------------------------------------------------
    def handle(self, body: str) -> dict[str, Any]:
        from .packs import web_media as wm

        self.advance()
        if body == wm.PROBE_BODY:
            return self.probe()
        from .watchers import WATCH_STATE_BODY, WATCH_STOP_BODY

        if body == WATCH_STATE_BODY:
            a = self.armed
            return {"armed": bool(a) and a["deadline"] >= self.rt.now() and len(a["log"]) < a["max"],
                    "clicks": len(a["log"]) if a else 0, "log": [{"t": t} for t in (a or {"log": []})["log"]],
                    "checks": 1}
        if body == WATCH_STOP_BODY:
            self.armed = None
            return {"armed": False}
        if body == wm.ARM_STATE_BODY:
            a = self.armed or {"log": []}
            return {"armed": bool(self.armed) and (self.armed or {}).get("deadline", 0) >= self.rt.now(),
                    "clicks": len(a["log"]), "log": [{"t": t} for t in a["log"]]}
        if body == wm.DISARM_BODY:
            self.armed = None
            return {"armed": False}
        if body == wm.CLICK_SKIP_BODY:
            if not self.cur or self.cur["kind"] != "S":
                return {"ok": False, "error": "skip not ready", "state": "none"}
            if self.skip_available_at is None or self.rt.now() < self.skip_available_at:
                return {"ok": False, "error": "skip not ready", "state": "countdown"}
            self._click(self.rt.now(), trusted=False)
            return {"ok": True, "label": "Skip Ad", "at": self.rt.now()}
        if "setInterval" in body and "__evHandsSkip" in body:
            import re

            ms = int(re.search(r"deadline:Date\.now\(\)\+(\d+)", body).group(1))
            mc = int(re.search(r"maxClicks:(\d+)", body).group(1))
            now = self.rt.now()
            self.armed = {"at": now, "deadline": now + ms / 1000.0, "max": mc, "log": []}
            return {"armed": True, "armedAt": now}
        if "function findAll(sel)" in body and "var sel=" in body:
            return {"n": 0, "matches": []}  # this page has no matching interactive element
        if "__evHandsWatch" in body and "MutationObserver" in body:
            import re

            ms = int(re.search(r"deadline:Date\.now\(\)\+(\d+)", body).group(1))
            mc = int(re.search(r"max:(\d+)", body).group(1))
            now = self.rt.now()
            self.armed = {"at": now, "deadline": now + ms / 1000.0, "max": mc, "log": []}
            return {"armed": True, "armedAt": now}
        if "var a=" in body and "primaryVideo" in body:
            import re

            m = re.search(r"var a=(\"[a-z_]+\"),val=(null|-?[0-9.]+);", body)
            action = json.loads(m.group(1))
            val = None if m.group(2) == "null" else float(m.group(2))
            return self.control(action, val)
        raise AssertionError("unhandled page script")

    def media(self) -> dict[str, Any]:
        return {"paused": self.paused, "ended": False,
                "t": round(self.content_t if not self.cur else (self.rt.now() - self.started), 1),
                "duration": float(self.content_len), "muted": self.muted,
                "volume": self.volume, "rate": self.rate, "ready": 4}

    def probe(self) -> dict[str, Any]:
        ad: dict[str, Any] = {"active": False, "signals": [], "remaining_s": None, "index": None,
                              "total": None, "skip": {"state": "none", "label": "", "wait_s": 0,
                                                      "enabled": False, "rect": None}}
        if self.cur:
            now = self.rt.now()
            ad.update(active=True, signals=["class:ad-showing"],
                      remaining_s=max(0, int(self.cur["len"] - (now - self.started))),
                      index=self.idx + 1, total=self.total)
            if self.cur["kind"] == "S" and self.skip_available_at is not None:
                if now >= self.skip_available_at:
                    ad["skip"] = {"state": "ready", "label": "Skip Ad", "wait_s": 0, "enabled": True,
                                  "rect": [500, 300, 60, 24]}
                else:
                    ad["skip"] = {"state": "countdown", "label": f"Skip in {int(self.skip_available_at - now + 0.99)}",
                                  "wait_s": int(self.skip_available_at - now + 0.99), "enabled": False,
                                  "rect": [500, 300, 60, 24]}
        return {"ok": True, "url": "http://x/", "title": "t", "hidden": False, "media_count": 1,
                "playing": (not self.paused), "media": self.media(), "ad": ad,
                "captions": {"label": "Subtitles/closed captions", "on": self.captions}}

    def control(self, action: str, val: float | None) -> dict[str, Any]:
        if action == "play":
            self.paused = False
        elif action == "pause":
            self.paused = True
        elif action == "toggle":
            self.paused = not self.paused
        elif action == "seek_by":
            self.content_t = max(0.0, self.content_t + float(val or 0))
        elif action == "seek_to":
            self.content_t = max(0.0, float(val or 0))
        elif action == "volume_to":
            self.volume = max(0.0, min(1.0, float(val or 0)))
            self.muted = False if self.volume > 0 else self.muted
        elif action == "volume_by":
            self.volume = max(0.0, min(1.0, self.volume + float(val or 0)))
            self.muted = False if self.volume > 0 else self.muted
        elif action == "mute":
            self.muted = True
        elif action == "unmute":
            self.muted = False
        elif action == "rate":
            self.rate = float(val or 1)
        elif action == "captions":
            before = str(self.captions).lower()
            self.captions = not self.captions
            return {"ok": True, "action": action, "control": "Subtitles", "before": before,
                    "after": str(self.captions).lower()}
        elif action in ("next", "previous"):
            return {"ok": True, "action": action, "control": action}
        return {"ok": True, "action": action, "media": self.media()}


class FakeAx:
    """AxClient stand-in: scripted `cgwindows`, `find` and `act` answers."""

    def __init__(self, *, windows: list[dict[str, Any]] | None = None, available: bool = True):
        self.windows = windows or []
        self._available = available
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def available(self) -> bool:
        return self._available

    def call(self, *args: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("call", args))
        if args and args[0] == "cgwindows":
            return {"ok": True, "windows": self.windows}
        return {"ok": False, "error": {"code": "unsupported", "message": "fake"}}

    def find(self, *a: Any, **k: Any) -> dict[str, Any]:
        self.calls.append(("find", a))
        return {"ok": True, "matches": []}

    def enable(self, app: str) -> dict[str, Any]:
        return {"ok": True}


class FakeBrowser:
    """BrowserDriver stand-in: tabs backed by PageSim objects."""

    def __init__(self, rt: FakeRuntime, kind: str = "chrome"):
        from .packs.browsers import Tab

        self._Tab = Tab
        self.rt = rt
        self.kind = kind
        self.app = {"chrome": "Google Chrome", "safari": "Safari"}[kind]
        self.process = self.app
        self.pages: dict[str, PageSim | Exception] = {}
        self._tabs: list[Any] = []
        self.js_calls: list[tuple[str, str]] = []
        self.is_running = True

    def add(self, window: str, tab: str, title: str, url: str, page: PageSim | Exception | None = None, *,
            active: bool = False, incognito: bool = False):
        self._tabs.append(self._Tab(self.kind, window, tab, len(self._tabs) + 1, title, url, active, incognito))
        if page is not None:
            self.pages[tab] = page
        return self

    def running(self) -> bool:
        return self.is_running

    def installed(self) -> bool:
        return True

    def tabs(self, *, window_ids=None, include_excluded=False):
        from .packs.browsers import host_excluded

        out = [t for t in self._tabs if not t.incognito
               and (window_ids is None or t.window_id in window_ids)
               and (include_excluded or not host_excluded(t.host))]
        return out

    def js_json(self, tab, body: str, *, timeout: float = 15.0):
        from .packs.browsers import BrowserError

        self.js_calls.append((tab.tab_id, body[:40]))
        page = self.pages.get(tab.tab_id)
        if isinstance(page, BrowserError):
            raise page
        if page is None:
            return {"ok": True, "playing": False, "media_count": 0, "ad": {"active": False}, "media": None}
        return page.handle(body)

    def js(self, tab, code, *, timeout: float = 15.0):
        return json.dumps(self.js_json(tab, code))

    def activate_tab(self, tab):
        for t in self._tabs:
            t.active = t.tab_id == tab.tab_id and t.window_id == tab.window_id

    def open_url(self, url, *, new_window=False):
        tid = f"t{len(self._tabs) + 1}"
        self.add("w9", tid, url, url, None)
        return {"window_id": "w9", "tab_id": tid}

    def close_tab(self, tab):
        self._tabs = [t for t in self._tabs if t.tab_id != tab.tab_id]

    def close_window(self, window_id):
        self._tabs = [t for t in self._tabs if t.window_id != window_id]
