"""Accessibility access for HANDS: the native `evhands` helper, with a slow JXA fallback.

`helpers/evhands` reads/acts on the AX tree in microseconds per attribute. It is
found at `EV_HANDS_AX_BINARY`-style locations in this order: ~/.ev/hands/bin/evhands,
the repo build, PATH; if none exists and `swiftc` does, it is compiled once from
the repo source into ~/.ev/hands/bin. Permissions are those of the launching
process; a denial returns `permission_missing` with the exact Settings path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .runtime import Runtime, default_runtime

SOURCE = Path(__file__).resolve().parents[4] / "helpers" / "evhands" / "Sources" / "evhands" / "main.swift"
EXPECT_VERSION = "1"


class AxClient:
    def __init__(self, rt: Runtime | None = None, binary: str | None = None):
        self.rt = rt or default_runtime()
        self._binary = binary
        self._checked = False

    # -- locating / building --------------------------------------------------
    def _state_dir(self) -> Path:
        try:
            from app.config import settings

            if settings.hands_state_dir:
                return Path(settings.hands_state_dir).expanduser()
        except Exception:
            pass
        return self.rt.home() / ".ev" / "hands"

    def binary(self) -> str | None:
        if self._binary:
            return self._binary
        if self._checked:
            return self._binary
        self._checked = True
        candidates = [self._state_dir() / "bin" / "evhands",
                      SOURCE.parents[2] / ".build" / "release" / "evhands"]
        for c in candidates:
            if c.exists() and self._version_ok(str(c)):
                self._binary = str(c)
                return self._binary
        found = self.rt.which("evhands")
        if found and self._version_ok(found):
            self._binary = found
            return found
        if SOURCE.exists() and self.rt.which("swiftc"):
            dest = self._state_dir() / "bin" / "evhands"
            dest.parent.mkdir(parents=True, exist_ok=True)
            p = self.rt.run(["swiftc", "-O", "-o", str(dest), str(SOURCE)], timeout=240)
            if p.ok and self._version_ok(str(dest)):
                self._binary = str(dest)
        return self._binary

    def _version_ok(self, path: str) -> bool:
        p = self.rt.run([path, "version"], timeout=10)
        try:
            return str(json.loads(p.out).get("version")) == EXPECT_VERSION
        except ValueError:
            return False

    # -- calls ----------------------------------------------------------------
    def call(self, *args: str, timeout: float = 20.0) -> dict[str, Any]:
        exe = self.binary()
        if not exe:
            return {"ok": False, "error": {"code": "unsupported",
                                           "message": "the evhands helper is not built",
                                           "fix": "Run: swiftc -O -o ~/.ev/hands/bin/evhands helpers/evhands/Sources/evhands/main.swift"}}
        p = self.rt.run([exe, *args], timeout=timeout)
        try:
            data = json.loads(p.out or "{}")
        except ValueError:
            return {"ok": False, "error": {"code": "internal", "message": (p.err or p.out)[:160]}}
        return data if isinstance(data, dict) else {"ok": False, "error": {"code": "internal", "message": "bad output"}}

    def available(self) -> bool:
        return self.binary() is not None

    def trusted(self) -> bool:
        return bool(self.call("trusted").get("trusted"))

    def panes(self, app: str, title: str | None = None) -> dict[str, Any]:
        a = ["panes", app] + (["--title", title] if title else [])
        return self.call(*a)

    def windows(self, app: str) -> dict[str, Any]:
        return self.call("windows", app)

    def tree(self, app: str, *, depth: int = 6, limit: int = 400, title: str | None = None) -> dict[str, Any]:
        a = ["tree", app, "--depth", str(depth), "--limit", str(limit)] + (["--title", title] if title else [])
        return self.call(*a, timeout=40)

    def find(self, app: str, *, role: str | None = None, name: str | None = None,
             title: str | None = None, max_hits: int = 20, depth: int = 16) -> dict[str, Any]:
        a = ["find", app, "--max", str(max_hits), "--depth", str(depth)]
        if role:
            a += ["--role", role]
        if name:
            a += ["--name", name]
        if title:
            a += ["--title", title]
        return self.call(*a, timeout=40)

    def act(self, app: str, path: list[int], verb: str, *, value: str | None = None,
            title: str | None = None) -> dict[str, Any]:
        a = ["act", app, "--path", ".".join(str(i) for i in path), "--do", verb]
        if value is not None:
            a += ["--value", value]
        if title:
            a += ["--title", title]
        return self.call(*a)

    def enable(self, app: str) -> dict[str, Any]:
        return self.call("enable", app)

    def click(self, x: float, y: float, *, clicks: int = 1, button: str = "left") -> dict[str, Any]:
        return self.call("click", str(x), str(y), "--clicks", str(clicks), "--button", button)

    def key(self, keycode: int, mods: str = "") -> dict[str, Any]:
        a = ["key", str(keycode)] + (["--mods", mods] if mods else [])
        return self.call(*a)


def escape_regex(text: str) -> str:
    return re.escape(text)
