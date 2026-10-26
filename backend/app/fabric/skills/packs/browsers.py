"""Browser driver: tabs and page JavaScript for Chrome and Safari via Apple Events.

Structured channel only. Page JavaScript runs through each browser's own
scripting verb (Chrome `execute javascript`, Safari `do JavaScript`), which the
browser permits only when "Allow JavaScript from Apple Events" is on; when it
is off this module says so with the exact setting (`js_disabled`), never a
generic failure. Private/incognito windows and excluded hosts are never listed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ..runtime import Proc, Runtime, applescript_quote, default_runtime, process_running

SEP = "\x1f"

JS_FIX = {
    "chrome": "In Chrome choose View > Developer > Allow JavaScript from Apple Events.",
    "safari": "In Safari open Settings > Developer (enable the Develop menu in Advanced first) "
              "and tick Allow JavaScript from Apple Events.",
}


@dataclass
class Tab:
    browser: str
    window_id: str
    tab_id: str
    index: int
    title: str
    url: str
    active: bool
    incognito: bool = False

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    def to_dict(self) -> dict[str, Any]:
        return {"browser": self.browser, "window_id": self.window_id, "tab_id": self.tab_id,
                "index": self.index, "title": self.title, "url": self.url,
                "active": self.active, "incognito": self.incognito, "host": self.host}


class BrowserError(RuntimeError):
    def __init__(self, code: str, message: str, fix: str | None = None):
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def excluded_hosts() -> list[str]:
    try:
        from app.config import settings

        raw = settings.hands_exclude_hosts
    except Exception:
        raw = ""
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def host_excluded(host: str, extra: list[str] | None = None) -> bool:
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in [*excluded_hosts(), *(extra or [])])


def _int(value: str) -> int:
    if not re.fullmatch(r"-?\d+", str(value).strip()):
        raise BrowserError("invalid_args", f"not a numeric id: {value!r}")
    return int(value)


class BrowserDriver:
    kind = ""
    app = ""
    process = ""

    def __init__(self, rt: Runtime | None = None):
        self.rt = rt or default_runtime()

    def running(self) -> bool:
        return process_running(self.rt, f"{self.app}.app/Contents/MacOS/{self.process}")

    def installed(self) -> bool:
        return self.rt.run(["test", "-d", f"/Applications/{self.app}.app"], timeout=5).ok

    def _as(self, script: str, what: str, timeout: float = 20.0) -> Proc:
        p = self.rt.osascript(script, timeout=timeout)
        if not p.ok:
            raise self._classify(p, what)
        return p

    def _classify(self, p: Proc, what: str) -> BrowserError:
        low = (p.err or "").lower()
        if p.permission_denied:
            return BrowserError("permission_missing", f"{what}: macOS blocked Apple Events to {self.app}",
                                f"System Settings > Privacy & Security > Automation: allow this app to control {self.app}.")
        if p.app_not_running:
            return BrowserError("not_running", f"{self.app} is not running")
        if "allow javascript from apple events" in low or "javascript through applescript is turned off" in low \
                or "javascript from apple events" in low:
            return BrowserError("js_disabled", f"{self.app} blocks JavaScript from Apple Events",
                                JS_FIX[self.kind])
        if p.timed_out:
            return BrowserError("timeout", f"{what}: osascript timed out")
        if "can’t get" in low or "can't get" in low or "invalid index" in low:
            return BrowserError("not_found", f"{what}: that tab or window is gone")
        return BrowserError("internal", f"{what}: {(p.err or p.out)[:200]}")

    # interface
    def tabs(self, *, window_ids: list[str] | None = None, include_excluded: bool = False) -> list[Tab]:
        raise NotImplementedError

    def js(self, tab: Tab, code: str, *, timeout: float = 15.0) -> str:
        raise NotImplementedError

    def open_url(self, url: str, *, new_window: bool = False) -> dict[str, str]:
        raise NotImplementedError

    def activate_tab(self, tab: Tab) -> None:
        raise NotImplementedError

    def close_tab(self, tab: Tab) -> None:
        raise NotImplementedError

    def close_window(self, window_id: str) -> None:
        raise NotImplementedError

    # shared
    def js_json(self, tab: Tab, body: str, *, timeout: float = 15.0) -> dict[str, Any]:
        """Run a JS body that returns a JSON-serialisable value; parse it."""
        wrapped = ("(function(){try{var __r=(function(){" + body + "})();"
                   "return JSON.stringify(__r===undefined?null:__r);}"
                   "catch(e){return JSON.stringify({__error:String(e&&e.message||e)});}})()")
        out = self.js(tab, wrapped, timeout=timeout)
        try:
            data = json.loads(out)
        except ValueError:
            raise BrowserError("internal", f"page returned non-JSON: {out[:80]!r}") from None
        if isinstance(data, dict) and "__error" in data:
            raise BrowserError("internal", f"page script error: {data['__error'][:160]}")
        return data if isinstance(data, dict) else {"value": data}

    def _filter(self, tabs: list[Tab], include_excluded: bool) -> list[Tab]:
        out = []
        for t in tabs:
            if t.incognito:
                continue  # private windows are never read
            if not include_excluded and host_excluded(t.host):
                continue
            out.append(t)
        return out


class ChromeDriver(BrowserDriver):
    kind = "chrome"
    app = "Google Chrome"
    process = "Google Chrome"

    _LIST = (
        'tell application "Google Chrome"\n'
        "  set sep to character id 31\n"
        '  set out to ""\n'
        "  repeat with w in windows\n"
        "    set wid to (id of w) as text\n"
        "    set md to (mode of w) as text\n"
        "    set ai to active tab index of w\n"
        "    set ids to id of every tab of w\n"
        "    set urls to URL of every tab of w\n"
        "    set ttls to title of every tab of w\n"
        "    repeat with i from 1 to count of ids\n"
        "      set out to out & wid & sep & ((item i of ids) as text) & sep & i & sep & "
        "((i = ai) as text) & sep & (item i of ttls) & sep & (item i of urls) & sep & md & linefeed\n"
        "    end repeat\n"
        "  end repeat\n"
        "  return out\n"
        "end tell")

    def tabs(self, *, window_ids: list[str] | None = None, include_excluded: bool = False) -> list[Tab]:
        if not self.running():
            raise BrowserError("not_running", "Google Chrome is not running")
        p = self._as(self._LIST, "list tabs", timeout=30)
        tabs = []
        for line in p.out.splitlines():
            f = line.split(SEP)
            if len(f) < 7:
                continue
            if window_ids is not None and f[0] not in window_ids:
                continue
            tabs.append(Tab("chrome", f[0], f[1], int(f[2] or 0), f[4], f[5],
                            f[3].strip().lower() == "true", incognito=f[6].strip().lower() == "incognito"))
        return self._filter(tabs, include_excluded)

    def js(self, tab: Tab, code: str, *, timeout: float = 15.0) -> str:
        wid, tid = _int(tab.window_id), _int(tab.tab_id)
        script = ('tell application "Google Chrome"\n'
                  f"  set t to tab id {tid} of window id {wid}\n"
                  f"  return execute t javascript {applescript_quote(code)}\n"
                  "end tell")
        return self._as(script, "page script", timeout=timeout).out

    def open_url(self, url: str, *, new_window: bool = False) -> dict[str, str]:
        q = applescript_quote(url)
        if new_window or not self.running():
            body = (f'set w to make new window\nset URL of active tab of w to {q}\n'
                    "return ((id of w) as text) & character id 31 & ((id of active tab of w) as text)")
        else:
            body = (f"if (count of windows) = 0 then make new window\n"
                    f"tell front window\n  set t to make new tab with properties {{URL:{q}}}\n"
                    "  return ((id of it) as text) & character id 31 & ((id of t) as text)\nend tell")
        out = self._as(f'tell application "Google Chrome"\n{body}\nend tell', "open url").out
        wid, tid = (out.split(SEP) + ["", ""])[:2]
        return {"window_id": wid, "tab_id": tid}

    def activate_tab(self, tab: Tab) -> None:
        self._as('tell application "Google Chrome"\n'
                 f"  set w to window id {_int(tab.window_id)}\n"
                 f"  set active tab index of w to {int(tab.index)}\n"
                 "  set index of w to 1\nend tell", "switch tab")

    def close_tab(self, tab: Tab) -> None:
        self._as('tell application "Google Chrome"\n'
                 f"  close (tab id {_int(tab.tab_id)} of window id {_int(tab.window_id)})\nend tell", "close tab")

    def close_window(self, window_id: str) -> None:
        self._as(f'tell application "Google Chrome" to close window id {_int(window_id)}', "close window")


class SafariDriver(BrowserDriver):
    """Safari tabs have no stable ids: `tab_id` is the index inside the window."""

    kind = "safari"
    app = "Safari"
    process = "Safari"

    _LIST = (
        'tell application "Safari"\n'
        "  set sep to character id 31\n"
        '  set out to ""\n'
        "  repeat with w in windows\n"
        "    set wid to (id of w) as text\n"
        "    set ci to 0\n"
        "    try\n"
        "      set ci to index of current tab of w\n"
        "    end try\n"
        "    set n to count of tabs of w\n"
        "    repeat with i from 1 to n\n"
        "      set t to tab i of w\n"
        "      set out to out & wid & sep & i & sep & i & sep & ((i = ci) as text) & sep & (name of t) & sep & "
        "(URL of t) & sep & \"normal\" & linefeed\n"
        "    end repeat\n"
        "  end repeat\n"
        "  return out\n"
        "end tell")

    def tabs(self, *, window_ids: list[str] | None = None, include_excluded: bool = False) -> list[Tab]:
        if not self.running():
            raise BrowserError("not_running", "Safari is not running")
        p = self._as(self._LIST, "list tabs", timeout=30)
        tabs = []
        for line in p.out.splitlines():
            f = line.split(SEP)
            if len(f) < 7:
                continue
            if window_ids is not None and f[0] not in window_ids:
                continue
            url = f[5] if f[5] != "missing value" else ""
            tabs.append(Tab("safari", f[0], f[1], int(f[2] or 0), f[4], url, f[3].strip().lower() == "true"))
        return self._filter(tabs, include_excluded)

    def js(self, tab: Tab, code: str, *, timeout: float = 15.0) -> str:
        script = ('tell application "Safari"\n'
                  f"  return do JavaScript {applescript_quote(code)} in (tab {_int(tab.index)} of window id {_int(tab.window_id)})\n"
                  "end tell")
        return self._as(script, "page script", timeout=timeout).out

    def open_url(self, url: str, *, new_window: bool = False) -> dict[str, str]:
        q = applescript_quote(url)
        if new_window or not self.running():
            body = (f"make new document with properties {{URL:{q}}}\n"
                    "return ((id of front window) as text) & character id 31 & \"1\"")
        else:
            body = ("if (count of windows) = 0 then make new document\n"
                    "tell front window\n"
                    f"  set current tab to (make new tab with properties {{URL:{q}}})\n"
                    "  return ((id of it) as text) & character id 31 & ((index of current tab) as text)\nend tell")
        out = self._as(f'tell application "Safari"\n{body}\nend tell', "open url").out
        wid, tid = (out.split(SEP) + ["", ""])[:2]
        return {"window_id": wid, "tab_id": tid}

    def activate_tab(self, tab: Tab) -> None:
        self._as('tell application "Safari"\n'
                 f"  set w to window id {_int(tab.window_id)}\n"
                 f"  set current tab of w to tab {_int(tab.index)} of w\n  set index of w to 1\nend tell",
                 "switch tab")

    def close_tab(self, tab: Tab) -> None:
        self._as('tell application "Safari"\n'
                 f"  close (tab {_int(tab.index)} of window id {_int(tab.window_id)})\nend tell", "close tab")

    def close_window(self, window_id: str) -> None:
        self._as(f'tell application "Safari" to close window id {_int(window_id)}', "close window")


DRIVERS: dict[str, type[BrowserDriver]] = {"chrome": ChromeDriver, "safari": SafariDriver}


def drivers(rt: Runtime | None = None, only: list[str] | None = None) -> list[BrowserDriver]:
    rt = rt or default_runtime()
    return [cls(rt) for name, cls in DRIVERS.items() if not only or name in only]
