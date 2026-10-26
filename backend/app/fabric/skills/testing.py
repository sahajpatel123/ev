"""Test doubles for the HANDS runtime: a scripted machine with a virtual clock.

`FakeRuntime` answers osascript/process calls from ordered rules (first match
wins), records every call, and never sleeps for real: `sleep()` advances a
virtual clock so countdown/ad timing tests run instantly and deterministically.
"""

from __future__ import annotations

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


def make_fake_home(root: Path, *, seed: int = 7, per_cli: int = 6) -> dict[str, Any]:
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
    work = ["/Users/test/Code/ev", "/Users/test/Code/orb", "/Users/test/Projects/api",
            "/Users/test/Code/presence", "/Users/test/Desktop/notes", "/Users/test/Code/site"]
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
