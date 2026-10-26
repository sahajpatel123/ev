"""Diagnostics: prompt-free permission probes and per-pack self-tests (SKILLS_API §7)."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

from . import receipts as R
from . import registry
from .packs import coding_sessions as cs
from .packs import ghostty as gh
from .packs.browsers import BrowserError, drivers
from .packs.common import ax as ax_of
from .registry import Ctx

APPS = {"Ghostty": "com.mitchellh.ghostty", "Google Chrome": "com.google.Chrome", "Safari": "com.apple.Safari",
        "Finder": "com.apple.finder", "System Events": "com.apple.systemevents", "Music": "com.apple.Music"}


def _row(name: str, kind: str, status: str, detail: str = "", fix: str = "") -> dict[str, Any]:
    out = {"permission": name, "kind": kind, "status": status, "detail": detail}
    if status != "granted" and fix:
        out["fix"] = fix
    return out


def permissions(ctx: Ctx) -> dict[str, Any]:
    """Every row is read without triggering a macOS prompt."""
    ax = ax_of(ctx)
    rows: list[dict[str, Any]] = []
    if not ax.available():
        rows.append(_row("evhands helper", "tool", "denied", "not built",
                         "swiftc -O -o ~/.ev/hands/bin/evhands helpers/evhands/Sources/evhands/main.swift"))
    else:
        rows.append(_row("evhands helper", "tool", "granted", "built"))
        t = ax.call("trusted")
        rows.append(_row("accessibility", "tcc", "granted" if t.get("trusted") else "denied",
                         "this process can read and press UI elements" if t.get("trusted") else "not trusted",
                         "System Settings > Privacy & Security > Accessibility: allow the app that runs Evie's helper."))
        s = ax.call("screen")
        rows.append(_row("screen_recording", "tcc", "granted" if s.get("granted") else "denied",
                         "", "System Settings > Privacy & Security > Screen & System Audio Recording: allow Evie."))
        for app, bundle in APPS.items():
            r = ax.call("automation", bundle)
            st = r.get("status", "unknown")
            status = {"granted": "granted", "denied": "denied", "undetermined": "unknown",
                      "not_running": "unknown"}.get(st, "unknown")
            detail = {"undetermined": "macOS will ask the first time; allow it",
                      "not_running": f"{app} is not running, so consent can't be checked yet"}.get(st, st)
            rows.append(_row(f"automation:{app}", "tcc", status, detail,
                             f"System Settings > Privacy & Security > Automation: allow control of {app}."))
    for b in drivers(ctx.rt):
        if not b.running():
            rows.append(_row(f"js_from_apple_events:{b.app}", "setting", "unknown", f"{b.app} is not running"))
            continue
        try:
            tabs = b.tabs()
            tab = next((t for t in tabs if t.url.startswith("http")), None)
            if tab is None:
                rows.append(_row(f"js_from_apple_events:{b.app}", "setting", "unknown", "no http tab to test on"))
                continue
            out = b.js(tab, "1+1")
            rows.append(_row(f"js_from_apple_events:{b.app}", "setting",
                             "granted" if str(out).strip() == "2" else "unknown", "page JavaScript works"))
        except BrowserError as e:
            status = "denied" if e.code in ("js_disabled", "permission_missing") else "unknown"
            rows.append(_row(f"js_from_apple_events:{b.app}", "setting", status, e.message, e.fix or ""))
    g = gh.probe(ctx.rt)
    rows.append(_row("ghostty", "tool", "granted" if g.get("scriptable") else ("unknown" if g.get("installed") else "denied"),
                     f"version {g.get('version')}, scriptable={g.get('scriptable')}",
                     "Install Ghostty 1.3+ (it ships the AppleScript dictionary)."))
    from .packs.terminal_restore import _ocr_binary

    ocr = _ocr_binary()
    rows.append(_row("ocr helper", "tool", "granted" if ocr else "denied", ocr or "evvision not found",
                     "cd helpers/evvision && swift build -c release"))
    found = cs.discover(ctx.rt, ctx.home)
    for name, info in found.items():
        rows.append(_row(f"coding_cli:{name}", "tool", "granted" if info["installed"] and info["resume_verified"] else
                         ("unknown" if info["installed"] else "denied"),
                         info["resume_template"] or info["resume_reason"]))
    bad = [r for r in rows if r["status"] == "denied" and r["kind"] in ("tcc", "setting")]
    return R.receipt(True, verified=True, skill="diag.permissions", evidence={"permissions": rows, "missing": len(bad)},
                     say=("Everything I need is allowed." if not bad else
                          f"{len(bad)} permission{'s are' if len(bad) != 1 else ' is'} missing: "
                          + ", ".join(r["permission"] for r in bad[:4]) + "."))


# ----------------------------------------------------------------- self tests ---

def _t(name: str, fn) -> dict[str, Any]:
    t0 = time.time()
    try:
        detail = fn()
        return {"name": name, "ok": True, "ms": int((time.time() - t0) * 1000), "detail": detail or ""}
    except Exception as exc:
        return {"name": name, "ok": False, "ms": int((time.time() - t0) * 1000),
                "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}


def selftest(ctx: Ctx, pack: str | None = None) -> dict[str, Any]:
    """Offline fixture self-tests (no machine effects): layout inference, id grounding, ad flow, routing."""
    from . import layout_tree as lt
    from . import synth
    from .packs.web_media import WebMedia
    from .testing import FakeBrowser, FakeRuntime, PageSim, fake_cli_runtime, make_fake_home

    tests: list[tuple[str, str, Any]] = []

    def layout() -> str:
        img, truth = synth.render("left_plus_two", seed=2)
        got = lt.infer_layout(img)
        assert lt.same_partition(got["tree"], truth, tol=0.04)
        return f"{got['pane_count']} panes"

    def grounding() -> str:
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            truth = make_fake_home(home)
            rt = fake_cli_runtime(home)
            rows = cs.all_sessions(rt, home)
            sid = truth["codex"][0]["id"]
            raw = sid.replace("0", "O", 1) if "0" in sid else sid
            g = cs.ground_id(raw, rows, cli="codex")
            assert g["executable"] and g["session"]["id"] == sid
            return g["status"]

    def skip() -> str:
        rt = FakeRuntime()
        page = PageSim(rt, [{"kind": "S", "skip_after": 3, "len": 10}])
        fb = FakeBrowser(rt)
        fb.add("w", "t", "Video", "http://localhost/p", page, active=True)
        r = WebMedia(rt, [fb]).skip_ad()
        assert r["ok"] and r["verified"], r["say"]
        return r["say"]

    def routing() -> str:
        top = registry.find("skip the ad")[0]["name"]
        assert top == "web_media.skip_ad", top
        return top

    tests += [("coding_sessions", "id grounding", grounding), ("ghostty", "layout inference", layout),
              ("web_media", "ad skip flow", skip), ("registry", "intent routing", routing)]
    out = [(pk, _t(name, fn)) for pk, name, fn in tests if pack in (None, pk)]
    rows = [{"pack": pk, **r} for pk, r in out]
    ok = all(r["ok"] for r in rows) and bool(rows)
    return R.receipt(ok, verified=ok, skill="diag.selftest", evidence={"tests": rows},
                     err=None if ok else R.error("internal", "a self-test failed"),
                     say="All self-tests passed." if ok else "A self-test failed.")
