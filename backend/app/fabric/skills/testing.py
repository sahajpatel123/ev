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

    # -- assertions ----------------------------------------------------------
    def scripts(self, pattern: str = "") -> list[str]:
        rx = re.compile(pattern, re.S)
        return [c["script"] for c in self.calls if c["kind"] == "osascript"
                and rx.search(c["script"])]

    def ran(self, pattern: str) -> bool:
        rx = re.compile(pattern, re.S)
        return any(rx.search(c.get("script") or " ".join(c.get("argv") or []))
                   for c in self.calls)
