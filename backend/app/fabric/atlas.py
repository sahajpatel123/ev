"""App capability atlas (WP6): installed automation surfaces, data-driven lanes.

Scans /Applications (+ ~/Applications) for scriptable apps (sdef),
URL schemes (Info.plist), user Shortcuts, and automation CLIs. Cached as
a content-addressed JSON file (~/.ev/atlas.json), rescanned when the app
dirs change. `route()` matches DOMAINS to observed capabilities — it
never names apps itself, so lane choice follows installed reality.
"""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

APP_DIRS = ("/Applications", str(Path.home() / "Applications"),
            "/System/Applications")

DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "note": ("note", "memo"),
    "reminder": ("reminder", "remind", "todo", "task"),
    "timer": ("timer", "clock", "alarm", "countdown"),
    "mail": ("mail",),
    "message": ("message", "chat"),
    "calendar": ("calendar", "cal", "event"),
}

AUTOMATION_CLIS = ("shortcuts", "osascript", "screencapture", "open", "pbcopy",
                   "pbpaste", "tmux", "ghostty", "docker", "mdfind")


def cache_path() -> Path:
    return Path.home() / ".ev" / "atlas.json"


def _apps_mtime() -> float:
    newest = 0.0
    for raw in APP_DIRS:
        try:
            newest = max(newest, Path(raw).stat().st_mtime)
        except OSError:
            pass
    return newest


def _read_plist(app: Path) -> dict[str, Any]:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as fh:
            data = plistlib.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _schemes(info: dict) -> list[str]:
    out: list[str] = []
    try:
        for entry in info.get("CFBundleURLTypes") or []:
            for scheme in (entry or {}).get("CFBundleURLSchemes") or []:
                if scheme:
                    out.append(str(scheme).lower())
    except (AttributeError, TypeError):
        pass
    return sorted(set(out))


def scan_apps() -> list[dict[str, Any]]:
    apps: list[dict[str, Any]] = []
    for raw in APP_DIRS:
        base = Path(raw)
        if not base.is_dir():
            continue
        try:
            bundles = sorted(base.iterdir())
        except OSError:
            continue
        for bundle in bundles:
            if bundle.suffix != ".app":
                continue
            info = _read_plist(bundle)
            resources = bundle / "Contents" / "Resources"
            try:
                sdef = any(p.suffix == ".sdef" for p in resources.iterdir()) \
                    if resources.is_dir() else False
            except OSError:
                sdef = False
            apps.append({
                "name": bundle.stem,
                "bundle_id": str(info.get("CFBundleIdentifier") or ""),
                "version": str(info.get("CFBundleShortVersionString") or ""),
                "path": str(bundle),
                "scriptable": bool(sdef or info.get("OSAScriptingDefinition")),
                "url_schemes": _schemes(info),
            })
    apps.sort(key=lambda row: row["name"].lower())
    return apps


def scan_shortcuts() -> list[str]:
    if shutil.which("shortcuts") is None:
        return []
    try:
        p = subprocess.run(["shortcuts", "list"], capture_output=True, text=True,
                           timeout=30)
    except Exception:
        return []
    if p.returncode != 0:
        return []
    return sorted({line.strip() for line in p.stdout.splitlines() if line.strip()})


def scan_clis() -> dict[str, str | None]:
    return {name: shutil.which(name) for name in AUTOMATION_CLIS}


def scan(*, force: bool = False) -> dict[str, Any]:
    """Full scan, cached. Rescans when app dirs changed or forced."""
    path = cache_path()
    mtime = _apps_mtime()
    if not force:
        try:
            cached = json.loads(path.read_text())
            if isinstance(cached, dict) and float(cached.get("apps_mtime", 0)) >= mtime:
                return cached
        except (OSError, ValueError):
            pass
    data = {"apps": scan_apps(), "shortcuts": scan_shortcuts(),
            "clis": scan_clis(), "scanned_at": time.time(), "apps_mtime": mtime}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError:
        pass
    return data


def _matches(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(key in lowered for key in keywords)


def route(domain: str, action: str = "", data: dict | None = None) -> dict[str, Any]:
    """Data-driven lane choice for 'do X in app Y'. Never names apps.

    Rank: matching user Shortcut > URL scheme > scriptable app. Returns a
    lane the kernel can call (mac_act op / timer.act / pim.act) with `why`
    citing the observed facts, or a gap record when nothing matches.
    """
    from .gaps import record_gap

    domain_key = (domain or "").strip().lower()
    keywords = DOMAIN_KEYWORDS.get(domain_key, (domain_key,))
    data = data or scan()
    shortcuts = [s for s in (data.get("shortcuts") or []) if isinstance(s, str)]
    for name in shortcuts:
        if _matches(name, keywords):
            return {"ok": True, "verified": True,
                    "evidence": {"lane": "mac_act", "op": "shortcuts_run",
                                 "target": name, "domain": domain_key,
                                 "why": f"shortcut '{name}' matches {domain_key}"},
                    "error": None, "undo": None}
    apps = [a for a in (data.get("apps") or []) if isinstance(a, dict)]
    if domain_key == "url" or (action or "").lower().startswith(("http", "open")):
        from urllib.parse import urlsplit

        scheme = ""
        try:
            scheme = (urlsplit(action).scheme or "").lower()
        except ValueError:
            pass
        for app in apps:
            if scheme and scheme in [s.lower() for s in app.get("url_schemes", [])]:
                return {"ok": True, "verified": True,
                        "evidence": {"lane": "mac_act", "op": "open_url",
                                     "target": app["name"], "domain": domain_key,
                                     "why": f"{app['name']} owns {scheme}://"},
                        "error": None, "undo": None}
        return {"ok": True, "verified": True,
                "evidence": {"lane": "mac_act", "op": "open_url",
                             "target": "default", "domain": domain_key,
                             "why": "no scheme owner; system default handler"},
                "error": None, "undo": None}
    if domain_key == "timer":
        return {"ok": True, "verified": True,
                "evidence": {"lane": "timer.act", "op": "start",
                             "domain": domain_key,
                             "why": "timers run on the fleet service, not an app"},
                "error": None, "undo": None}
    if domain_key == "reminder":
        return {"ok": True, "verified": True,
                "evidence": {"lane": "pim.act", "op": "reminder_create",
                             "domain": domain_key,
                             "why": "reminders run on the PIM lane with EventKit ahead"},
                "error": None, "undo": None}
    for app in apps:
        hay = f"{app.get('name', '')} {app.get('bundle_id', '')}"
        if app.get("scriptable") and _matches(hay, keywords):
            return {"ok": True, "verified": True,
                    "evidence": {"lane": "mac_act", "op": "jxa",
                                 "target": app["name"], "domain": domain_key,
                                 "why": f"{app['name']} is scriptable (sdef) and matches {domain_key}"},
                    "error": None, "undo": None}
    for app in apps:
        if _matches(f"{app.get('name', '')} {app.get('bundle_id', '')}", keywords):
            return {"ok": True, "verified": True,
                    "evidence": {"lane": "operator.act", "op": "drive",
                                 "target": app["name"], "domain": domain_key,
                                 "why": f"{app['name']} matches but has no direct lane; UI operator"},
                    "error": None, "undo": None}
    gap = record_gap(f"atlas:{domain_key}:{action[:60]}")
    gap["evidence"]["domain"] = domain_key
    try:
        from .gaps import record_central

        record_central(f"atlas:{domain_key}:{action[:60]}")
    except Exception:
        pass
    return gap


def describe(data: dict | None = None) -> dict[str, Any]:
    """Compact atlas summary for discover payloads."""
    data = data or scan()
    apps = data.get("apps") or []
    return {"ok": True, "verified": True,
            "evidence": {"apps": len(apps),
                         "scriptable": sorted(a["name"] for a in apps if a.get("scriptable")),
                         "shortcuts": data.get("shortcuts") or [],
                         "clis": {k: bool(v) for k, v in (data.get("clis") or {}).items()},
                         "scanned_at": data.get("scanned_at")},
            "error": None, "undo": None}
