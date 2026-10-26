"""The OS effect surface for HANDS: osascript, processes, clocks.

Everything that touches the machine goes through a `Runtime`, so skills are
tested against `FakeRuntime` (app/fabric/skills/testing.py) and run live
against `SystemRuntime`. Third-party app scripting honours
`EV_HANDS_OSASCRIPT_ROUTE`: the Evie.app bridge carries the app's TCC grants,
`direct` uses this process's, and `auto` picks the bridge when it answers.
Every result records the identity that ran it.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

_AUTOMATION_DENIAL = ("-1743", "not authorized to send apple events", "-25211",
                      "not allowed assistive access", "-1719", "assistive access")
_NOT_RUNNING = ("-600", "application isn't running", "isn’t running", "-10810")


@dataclass
class Proc:
    ok: bool
    out: str = ""
    err: str = ""
    code: int = 0
    ms: int = 0
    identity: str = "direct"
    timed_out: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def permission_denied(self) -> bool:
        low = (self.err or "").lower()
        return any(sign in low for sign in _AUTOMATION_DENIAL)

    @property
    def app_not_running(self) -> bool:
        low = (self.err or "").lower()
        return any(sign in low for sign in _NOT_RUNNING)


class Runtime(Protocol):
    def osascript(self, script: str, *, lang: str = "applescript",
                  timeout: float = 20.0, argv: list[str] | None = None) -> Proc: ...

    def run(self, argv: list[str], *, timeout: float = 20.0,
            input: str | None = None) -> Proc: ...

    def sleep(self, seconds: float) -> None: ...

    def monotonic(self) -> float: ...

    def now(self) -> float: ...

    def home(self) -> Path: ...

    def which(self, name: str) -> str | None: ...


class SystemRuntime:
    """Real machine. Stateless apart from the cached bridge probe."""

    def __init__(self, route: str | None = None):
        self._route = route
        self._bridge_up: tuple[float, bool] | None = None

    def route(self) -> str:
        if self._route:
            return self._route
        try:
            from app.config import settings

            return str(settings.hands_osascript_route or "auto").lower()
        except Exception:
            return "auto"

    def _bridge_answers(self) -> bool:
        now = time.monotonic()
        if self._bridge_up and now - self._bridge_up[0] < 5.0:
            return self._bridge_up[1]
        try:
            from app.fabric.bridge_client import BridgeClient, bridge_available

            up = bridge_available() and bool(BridgeClient(timeout=2).call("ping").get("ok"))
        except Exception:
            up = False
        self._bridge_up = (now, up)
        return up

    def osascript(self, script: str, *, lang: str = "applescript",
                  timeout: float = 20.0, argv: list[str] | None = None) -> Proc:
        route = self.route()
        if route in ("bridge", "auto") and not argv and self._bridge_answers():
            return self._via_bridge(script, lang, timeout)
        if route == "bridge":
            return Proc(False, err="bridge unavailable: Evie.app is not answering",
                        code=-1, identity="bridge")
        return self._direct(script, lang, timeout, argv or [])

    def _via_bridge(self, script: str, lang: str, timeout: float) -> Proc:
        from app.fabric.bridge_client import BridgeClient

        t = time.monotonic()
        res = BridgeClient(timeout=timeout + 5).call(
            "jxa", {"script": script,
                    "language": "applescript" if lang == "applescript" else "javascript"},
            timeout=timeout + 5)
        ms = int((time.monotonic() - t) * 1000)
        if res.get("ok"):
            out = str((res.get("evidence") or {}).get("out") or "")
            return Proc(True, out=out.strip(), ms=ms, identity="bridge")
        return Proc(False, err=str(res.get("error") or "bridge error"), code=1,
                    ms=ms, identity="bridge")

    def _direct(self, script: str, lang: str, timeout: float, argv: list[str]) -> Proc:
        cmd = ["osascript"]
        if lang != "applescript":
            cmd += ["-l", "JavaScript"]
        cmd += ["-", *argv]
        t = time.monotonic()
        try:
            p = subprocess.run(cmd, input=script, capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return Proc(False, err="osascript timeout", code=-2, timed_out=True,
                        ms=int((time.monotonic() - t) * 1000))
        except OSError as exc:
            return Proc(False, err=f"osascript unavailable: {exc}", code=-3)
        return Proc(p.returncode == 0, out=(p.stdout or "").strip(),
                    err=(p.stderr or "").strip(), code=p.returncode,
                    ms=int((time.monotonic() - t) * 1000), identity="direct")

    def run(self, argv: list[str], *, timeout: float = 20.0,
            input: str | None = None) -> Proc:
        t = time.monotonic()
        try:
            p = subprocess.run(argv, input=input, capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return Proc(False, err="timeout", code=-2, timed_out=True,
                        ms=int((time.monotonic() - t) * 1000))
        except OSError as exc:
            return Proc(False, err=str(exc), code=-3)
        return Proc(p.returncode == 0, out=p.stdout or "", err=p.stderr or "",
                    code=p.returncode, ms=int((time.monotonic() - t) * 1000))

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> float:
        return time.time()

    def home(self) -> Path:
        return Path.home()

    def which(self, name: str) -> str | None:
        return shutil.which(name)


_default: Runtime | None = None


def default_runtime() -> Runtime:
    global _default
    if _default is None:
        _default = SystemRuntime()
    return _default


def applescript_quote(text: str) -> str:
    """A double-quoted AppleScript string literal for arbitrary text."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def as_list(text: str) -> list[str]:
    """Split osascript's `a, b, c` list output."""
    return [part.strip() for part in (text or "").split(", ") if part.strip()]


def process_running(rt: Runtime, fragment: str) -> bool:
    """True when an app's main executable is running.

    `fragment` is "<App>.app/Contents/MacOS/<exe>". Only command lines that
    START with an Applications path and that fragment count, so a shell command
    that merely mentions the name (or a helper process) never matches. `ps` is
    used instead of `pgrep`, which misses GUI-session processes in some
    sandboxed shells.
    """
    import re

    rx = re.compile(r"^\s*/(?!bin/|usr/bin/|opt/homebrew/bin/|usr/local/bin/)[^\n]*?/" + re.escape(fragment)
                    + r"(?:\s|$)")
    p = rt.run(["ps", "-axo", "command="], timeout=8)
    return any(rx.match(line) for line in (p.out or "").splitlines())
