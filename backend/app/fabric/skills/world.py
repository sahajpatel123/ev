"""The world model: a live, cheap, structured index of the Mac (SKILLS_API §2).

Sections are refreshed on demand and cached for `EV_HANDS_WORLD_TTL_S`. Each
section reads through the cheapest structured channel (lsappinfo, CoreGraphics
window list, Apple Events, tmux, ctypes) with no model call and no focus change.
Exclusions are applied here, before anything reaches evidence: password
managers and excluded hosts are counted, never shown; private windows are not read.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import receipts as R
from . import system_state
from .ax import AxClient
from .packs import tmux_world
from .packs.browsers import BrowserError, drivers, host_excluded
from .packs.ghostty import GhosttyDriver, GhosttyError
from .registry import Ctx
from .runtime import Runtime

SECTIONS = ("apps", "windows", "tabs", "terminals", "media", "system", "focus", "displays")


def _excluded_apps() -> list[str]:
    try:
        from app.config import settings

        raw = settings.hands_exclude_apps
    except Exception:
        raw = ""
    return [a.strip().lower() for a in raw.split(",") if a.strip()]


def _ttl() -> float:
    try:
        from app.config import settings

        return float(settings.hands_world_ttl_s)
    except Exception:
        return 2.0


def match(item: dict[str, Any], where: dict[str, Any] | None) -> bool:
    """Exact keys, `<key>_contains` (case-insensitive substring), `<key>_in` (membership)."""
    for key, want in (where or {}).items():
        if key.endswith("_contains"):
            have = str(_dig(item, key[:-9]) or "").lower()
            if str(want).lower() not in have:
                return False
        elif key.endswith("_in"):
            if _dig(item, key[:-3]) not in want:
                return False
        else:
            have = _dig(item, key)
            if isinstance(want, str) and isinstance(have, str):
                if have.lower() != want.lower():
                    return False
            elif have != want:
                return False
    return True


def _dig(item: dict[str, Any], path: str) -> Any:
    cur: Any = item
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


_APP_HEAD = re.compile(r'^\s*\d+\)\s+"(.*?)"\s+ASN:(\S+):')


def parse_lsappinfo(text: str) -> list[dict[str, Any]]:
    apps: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in text.splitlines():
        m = _APP_HEAD.match(line)
        if m:
            cur = {"name": m.group(1), "asn": m.group(2), "bundle_id": "", "pid": 0, "type": ""}
            apps.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("bundleID="):
            cur["bundle_id"] = s.split("=", 1)[1].strip('"') if "NULL" not in s else ""
        elif s.startswith("pid ="):
            pm = re.match(r"pid = (\d+)", s)
            tm = re.search(r'type="(\w+)"', s)
            if pm:
                cur["pid"] = int(pm.group(1))
            if tm:
                cur["type"] = tm.group(1)
        elif s.startswith("bundle path="):
            cur["path"] = s.split("=", 1)[1].strip('"')
    return apps


class World:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.rt: Runtime = ctx.rt
        self._cache: dict[str, tuple[float, Any]] = {}
        self._excluded_count = 0
        self.ax = ctx.ax or AxClient(self.rt)
        self.ghostty = ctx.ghostty or GhosttyDriver(self.rt, self.ax)
        self.browsers = ctx.browsers if ctx.browsers is not None else drivers(self.rt)

    # -- cache ------------------------------------------------------------------
    def _section(self, name: str, build, *, refresh: Any, max_age_s: float | None) -> tuple[Any, float]:
        ttl = _ttl() if max_age_s is None else max_age_s
        now = self.rt.monotonic()
        hit = self._cache.get(name)
        if refresh is not True and hit and (now - hit[0]) <= ttl and refresh != "force":
            return hit[1], round(now - hit[0], 2)
        value = build()
        self._cache[name] = (now, value)
        return value, 0.0

    # -- sections -----------------------------------------------------------------
    def _apps(self) -> list[dict[str, Any]]:
        p = self.rt.run(["lsappinfo", "list"], timeout=10)
        front = (self.rt.run(["lsappinfo", "front"], timeout=5).out or "").strip().replace("ASN:", "").strip(":")
        excl = _excluded_apps()
        out = []
        for a in parse_lsappinfo(p.out or ""):
            if a["type"] != "Foreground":
                continue
            if a["name"].lower() in excl:
                self._excluded_count += 1
                continue
            out.append({"name": a["name"], "bundle_id": a["bundle_id"], "pid": a["pid"],
                        "frontmost": bool(front) and a["asn"].strip(":") == front,
                        "hidden": False, "scriptable": None})
        return out

    def _windows(self) -> list[dict[str, Any]]:
        res = self.ax.call("cgwindows") if self.ax.available() else {"ok": False}
        excl = _excluded_apps()
        out = []
        if res.get("ok"):
            for i, w in enumerate(res.get("windows", [])):
                if w.get("layer") != 0 or w["w"] < 40 or w["h"] < 40:
                    continue
                if w["owner"].lower() in excl:
                    self._excluded_count += 1
                    continue
                out.append({"id": w["number"], "app": w["owner"], "title": w["title"],
                            "frame": {"x": w["x"], "y": w["y"], "w": w["w"], "h": w["h"]},
                            "front": not out, "minimized": False, "display": 1})
        return out

    def _tabs(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for b in self.browsers:
            try:
                if not b.running():
                    continue
                for t in b.tabs(window_ids=(self.ctx.window_ids or {}).get(b.kind)):
                    out.append({**t.to_dict(), "audible": None, "playing": None, "media_kind": None})
            except BrowserError:
                continue
        return out

    def _enrich_tabs(self, tabs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from .packs.web_media import WebMedia

        wm = WebMedia(self.rt, self.browsers, window_ids=self.ctx.window_ids)
        results, _ = wm.survey()
        by = {(pr.tab.browser, pr.tab.window_id, pr.tab.tab_id): pr for _, pr in results}
        for t in tabs:
            pr = by.get((t["browser"], t["window_id"], t["tab_id"]))
            if pr and pr.state:
                t["playing"] = bool(pr.state.get("playing"))
                t["audible"] = bool(pr.state.get("playing")) and not (pr.state.get("media") or {}).get("muted", False)
                t["media_kind"] = "video" if (pr.state.get("media") or {}).get("w") is not None else "media"
                t["ad"] = bool((pr.state.get("ad") or {}).get("active"))
        return tabs

    def _terminals(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            for t in self.ghostty.inventory(only_windows=(self.ctx.window_ids or {}).get("ghostty")):
                out.append({**t.to_dict(), "foreground_cmd": None, "pid": None, "session_hint": None})
        except GhosttyError:
            pass
        out.extend(tmux_world.panes(self.rt))
        return out

    def _system(self) -> dict[str, Any]:
        return system_state.read(self.rt, volume=True)

    def _focus(self) -> dict[str, Any]:
        front = (self.rt.run(["lsappinfo", "front"], timeout=5).out or "").strip()
        if not front:
            return {"app": None, "window_title": None, "element": None}
        info = self.rt.run(["lsappinfo", "info", "-only", "name", front], timeout=5).out or ""
        m = re.match(r'\s*"([^"]*)"\s+ASN', info)
        name = m.group(1) if m else ""
        if name.lower() in _excluded_apps():
            return {"app": "(excluded)", "window_title": None, "element": None}
        win = next((w for w in self._windows() if w["app"] == name), None)
        return {"app": name or None, "window_title": (win or {}).get("title"), "element": None}

    def _displays(self) -> list[dict[str, Any]]:
        p = self.rt.osascript('tell application "Finder" to get bounds of window of desktop', timeout=8)
        nums = [int(x) for x in re.findall(r"-?\d+", p.out or "")]
        if len(nums) == 4:
            return [{"id": 1, "frame": {"x": nums[0], "y": nums[1], "w": nums[2] - nums[0],
                                        "h": nums[3] - nums[1]}, "main": True}]
        return []

    # -- public ---------------------------------------------------------------------
    def query(self, select: str, where: dict[str, Any] | None = None, *, refresh: Any = "auto",
              max_age_s: float | None = None, limit: int = 50) -> dict[str, Any]:
        if select not in SECTIONS:
            return R.fail("invalid_args", f"select must be one of {', '.join(SECTIONS)}.")
        self._excluded_count = 0
        builders = {"apps": self._apps, "windows": self._windows, "tabs": self._tabs,
                    "terminals": self._terminals, "system": self._system, "focus": self._focus,
                    "displays": self._displays}
        enrich = select == "tabs" and any(k.split("_")[0] in ("playing", "audible", "media", "ad")
                                          for k in (where or {}))
        if select == "media":
            tabs_val, age = self._section("tabs+media", lambda: self._enrich_tabs(self._tabs()),
                                          refresh=refresh, max_age_s=max_age_s)
            items = [self._media_item(t) for t in tabs_val if t.get("playing")]
            items += self._music_sessions()
        elif enrich:
            tabs_val, age = self._section("tabs+media", lambda: self._enrich_tabs(self._tabs()),
                                          refresh=refresh, max_age_s=max_age_s)
            items = tabs_val
        else:
            val, age = self._section(select, builders[select], refresh=refresh, max_age_s=max_age_s)
            items = val if isinstance(val, list) else [val]
        rows = [i for i in items if match(i, where)][:limit]
        return R.receipt(True, verified=True, skill="world.query", channel=None,
                         evidence={"select": select, "items": rows, "n": len(rows),
                                   "generated_at": self.rt.now(), "ages": {select: age},
                                   "excluded": self._excluded_count},
                         say=None)

    def snapshot(self, *, refresh: Any = "auto") -> dict[str, Any]:
        snap: dict[str, Any] = {}
        for sec in ("apps", "windows", "tabs", "terminals", "system", "focus", "displays"):
            snap[sec] = self.query(sec, refresh=refresh)["evidence"]["items"]
        snap["media"] = self.query("media", refresh=refresh)["evidence"]["items"]
        return R.receipt(True, verified=True, skill="world.snapshot",
                         evidence={"snapshot": snap, "generated_at": self.rt.now()})

    @staticmethod
    def _media_item(t: dict[str, Any]) -> dict[str, Any]:
        return {"app": t["browser"], "window_id": t["window_id"], "tab_id": t["tab_id"],
                "title": t["title"], "url": t["url"], "state": "ad" if t.get("ad") else "playing",
                "position_s": None, "duration_s": None,
                "ad": {"active": bool(t.get("ad"))}}

    def _music_sessions(self) -> list[dict[str, Any]]:
        out = []
        for app in ("Music", "Spotify"):
            from .runtime import process_running

            if not process_running(self.rt, f"{app}.app/Contents/MacOS/{app}"):
                continue
            p = self.rt.osascript(
                f'tell application "{app}"\nif player state is playing then return '
                '(name of current track) & character id 31 & (artist of current track)\n'
                'return ""\nend tell', timeout=8)
            if p.ok and p.out.strip():
                title, _, artist = p.out.partition("\x1f")
                out.append({"app": app, "title": title, "artist": artist, "state": "playing",
                            "url": None, "window_id": None, "tab_id": None})
        return out


_ = (json, host_excluded)
