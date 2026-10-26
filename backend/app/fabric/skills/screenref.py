"""Screenshot-as-reference, generalised (SKILLS_API §6, task D).

`screen_reference(image, target)` NEVER acts. It reads the image with the
cheapest sufficient channels (pixels, OCR, an injected brain-vision callable),
grounds every value against local truth, and returns a plan the owner confirms.
`screen_apply(plan_id)` executes exactly that plan and reads each target back.
Anything the grounding could not confirm is flagged and not executed. Everything
derived from the image has provenance "screenshot" (R7).

Targets: terminal_layout (T1), browser_tabs, window_arrangement, settings_toggles.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import receipts as R
from .packs import terminal_restore as tr
from .packs.browsers import BrowserError, host_excluded
from .packs.common import browsers, cache_plan, cached_plan, ghostty, session_index, shell_risk
from .receipts import Recorder
from .registry import Ctx
from .ui import UI

TARGETS = ("terminal_layout", "browser_tabs", "window_arrangement", "settings_toggles")


def resolve_image(ctx: Ctx, image: str | None) -> Path | None:
    if image and image != "last":
        p = Path(image).expanduser()
        return p if p.is_file() else None
    if ctx.extra.get("last_image"):
        return Path(ctx.extra["last_image"])
    try:
        from app.fabric.screen_pipeline import resolve_last

        found = resolve_last()
        if found and found.get("path"):
            return Path(found["path"])
    except Exception:
        pass
    return None


def _allow_history(ctx: Ctx) -> bool:
    if "allow_history" in ctx.extra:
        return bool(ctx.extra["allow_history"])
    try:
        from app.config import settings

        return bool(settings.hands_allow_history)
    except Exception:
        return False


def _ocr(ctx: Ctx, path: Path) -> dict[str, Any]:
    fn = ctx.ocr_fn or tr.evvision_ocr(ctx.rt)
    return fn(path)


def reference(ctx: Ctx, image: str | None, target: str, *, hint: str | None = None) -> dict[str, Any]:
    if target not in TARGETS:
        return R.fail("invalid_args", f"target must be one of {', '.join(TARGETS)}.", provenance="screenshot")
    path = resolve_image(ctx, image)
    if path is None:
        return R.fail("not_found", "I couldn't find that screenshot.", provenance="screenshot",
                      fix="Take a screenshot, or give me the file's path.")
    key = f"ref:{target}:{path}:{path.stat().st_mtime_ns}:{hint or ''}"
    plan = cached_plan(key)
    if plan is None:
        build = {"terminal_layout": _plan_terminal, "browser_tabs": _plan_tabs,
                 "window_arrangement": _plan_windows, "settings_toggles": _plan_toggles}[target]
        plan = build(ctx, path, hint)
        if plan.get("error"):
            return R.fail(plan["error"]["code"], plan["error"]["message"], fix=plan["error"].get("fix"),
                          provenance="screenshot", evidence=plan.get("evidence"))
        plan["target"], plan["image"] = target, str(path)
        plan["plan_id"] = "ref-" + hashlib.sha256(key.encode()).hexdigest()[:14]
        cache_plan(key, plan)
        cache_plan(plan["plan_id"], plan)
    card = {"title": plan["title"], "rows": plan["rows"], "risk": "confirm", "physical": bool(plan.get("physical")),
            "plan_id": plan["plan_id"], "expires_s": 300, "layout_ascii": plan.get("layout_ascii", "")}
    return R.receipt(True, verified=False, skill="screen.reference", provenance="screenshot", channel="ocr",
                     plan=plan["steps"], confirm=card, needs_confirm=True,
                     evidence={"target": target, "plan_id": plan["plan_id"], "flags": plan.get("flags", []),
                               "untrusted": True, "image": str(path)},
                     say=plan["say"])


def apply(ctx: Ctx, plan_id: str, *, approved: bool = False, approval_id: str | None = None) -> dict[str, Any]:
    plan = cached_plan(plan_id)
    if plan is None:
        return R.fail("not_found", "That plan expired; ask me to read the screenshot again.", provenance="screenshot")
    if not approved or approval_id != plan_id:
        return R.receipt(False, needs_confirm=True, provenance="screenshot", skill="screen.apply",
                         err=R.error("confirm_required", "I need your confirmation to do this.", retryable=True),
                         confirm={"title": plan["title"], "rows": plan["rows"], "risk": "confirm", "plan_id": plan_id,
                                  "physical": bool(plan.get("physical")), "expires_s": 300},
                         say="I need your confirmation first.")
    run = {"terminal_layout": _apply_terminal, "browser_tabs": _apply_tabs,
           "window_arrangement": _apply_windows, "settings_toggles": _apply_toggles}[plan["target"]]
    return run(ctx, plan)


# --------------------------------------------------------------- terminal ------

def _plan_terminal(ctx: Ctx, path: Path, hint: str | None) -> dict[str, Any]:
    plan = tr.analyze(path, rt=ctx.rt, home=ctx.home, ocr_fn=ctx.ocr_fn, vision_fn=ctx.vision_fn,
                      index=session_index(ctx))
    card = tr.confirm_card(plan)
    return {"title": card["title"], "rows": card["rows"], "layout_ascii": card["layout_ascii"], "raw": plan,
            "steps": [{"pane": p["id"], "command": p["command"], "cwd": p["cwd"], "will_run": p["executable"]}
                      for p in plan["panes"]],
            "flags": plan["flags"], "physical": any(shell_risk(p["command"] or "") for p in plan["panes"]),
            "say": f"I read {plan['pane_count']} panes and can resume {plan['executable_panes']} of them. Please confirm."}


def _apply_terminal(ctx: Ctx, plan: dict[str, Any]) -> dict[str, Any]:
    from .packs.skills_terminal import _gerr
    from .packs.ghostty import GhosttyError

    rec = Recorder("screen.apply", provenance="screenshot", clock=ctx.rt.monotonic)
    g = ghostty(ctx)
    try:
        win = g.front_window()
        out = tr.apply(plan["raw"], driver=g, window_id=win, new_window=not win, rt=ctx.rt, home=ctx.home)
    except GhosttyError as exc:
        return _gerr(exc)
    ok = all(r["ok"] for r in out["panes"])
    rec.add("act", "build tab", True, channel="applescript")
    rec.add("verify", "panes read back", ok, channel="applescript", evidence={"panes": out["panes"]})
    return rec.finish(ok, evidence={"panes": out["panes"]}, undo=out["undo"],
                      say="The layout is back and the sessions are resumed." if ok else None)


# ---------------------------------------------------------------- browser tabs --

def history_lookup(ctx: Ctx, title: str) -> list[dict[str, Any]]:
    """Titles -> URLs from the browsers' own history. Only when the owner allowed it."""
    rows: list[dict[str, Any]] = []
    home = Path(ctx.home) if ctx.home else ctx.rt.home()
    paths = {"chrome": home / "Library/Application Support/Google/Chrome/Default/History",
             "safari": home / "Library/Safari/History.db"}
    for kind, p in ctx.extra.get("history_paths", paths).items():
        if not Path(p).exists():
            continue
        tmp = Path(tempfile.mkdtemp()) / "h.db"
        try:
            shutil.copy2(p, tmp)  # the live DB is locked; read a copy
            con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
            if kind == "chrome":
                q = ("select url, title, last_visit_time from urls where title like ? "
                     "order by last_visit_time desc limit 8")
            else:
                q = ("select i.url, v.title, v.visit_time from history_items i join history_visits v on v.history_item=i.id "
                     "where v.title like ? order by v.visit_time desc limit 8")
            for url, ttl, ts in con.execute(q, (f"%{title[:40]}%",)):
                rows.append({"browser": kind, "url": url, "title": ttl or "", "ts": ts})
            con.close()
        except (sqlite3.Error, OSError):
            continue
    return rows


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def ground_title(ctx: Ctx, title: str) -> dict[str, Any]:
    t = _norm(title.rstrip("…. "))
    if not t:
        return {"status": "unreadable"}
    # 1. already open: the answer is a real tab we can read
    for b in browsers(ctx):
        try:
            if not b.running():
                continue
            for tab in b.tabs(window_ids=(ctx.window_ids or {}).get(b.kind)):
                nt = _norm(tab.title)
                if nt == t or (len(t) >= 8 and (nt.startswith(t) or t.startswith(nt))):
                    return {"status": "open", "url": tab.url, "title": tab.title, "source": "open tab"}
        except BrowserError:
            continue
    if not _allow_history(ctx):
        return {"status": "needs_history", "note": "history access is off"}
    hits = ctx.extra.get("history_fn", lambda c, x: history_lookup(c, x))(ctx, title)
    scored = []
    for h in hits:
        nt = _norm(h["title"])
        if nt == t:
            scored.append((1.0, h))
        elif nt.startswith(t) or t.startswith(nt):
            scored.append((0.85, h))
    scored.sort(key=lambda x: -x[0])
    urls = []
    for sc, h in scored:
        if h["url"] not in [u[1]["url"] for u in urls]:
            urls.append((sc, h))
    if not urls:
        return {"status": "unmatched"}
    if len(urls) > 1 and urls[0][0] - urls[1][0] < 0.1 and urlsplit(urls[0][1]["url"]).hostname != urlsplit(urls[1][1]["url"]).hostname:
        return {"status": "ambiguous", "candidates": [u[1] for u in urls[:4]]}
    sc, h = urls[0]
    return {"status": "exact" if sc == 1.0 else "fuzzy", "url": h["url"], "title": h["title"], "source": f"{h['browser']} history"}


def _tab_titles(lines: list[dict[str, Any]]) -> list[str]:
    """Tab titles from OCR lines of a tab-bar screenshot: the top band, left to right."""
    boxes = []
    for ln in lines:
        b = ln.get("bounding_box") or {}
        boxes.append((float(b.get("y", 0)), float(b.get("x", 0)), str(ln.get("text") or "").strip()))
    if not boxes:
        return []
    top = min(y for y, _, _ in boxes)
    band = [(x, t) for y, x, t in boxes if y - top < 0.12 and t and len(t) > 1]
    band.sort()
    out = []
    for _, t in band:
        t = re.sub(r"^\W+", "", t)
        if t.lower() in ("new tab", "+") or len(t) < 2:
            continue
        out.append(t)
    return out


def _plan_tabs(ctx: Ctx, path: Path, hint: str | None) -> dict[str, Any]:
    res = _ocr(ctx, path)
    if not res.get("ok"):
        return {"error": {"code": "unsupported", "message": "I couldn't read that screenshot's text.",
                          "fix": "Build the OCR helper: swift build -c release in helpers/evvision."}}
    titles = (ctx.extra.get("tab_titles_override") or _tab_titles(res.get("lines", [])))
    rows, steps, flags = [], [], []
    for i, title in enumerate(titles, start=1):
        g = ground_title(ctx, title)
        host = (urlsplit(g.get("url", "")).hostname or "").lower() if g.get("url") else ""
        excluded = bool(host) and host_excluded(host)
        will = g["status"] in ("exact", "fuzzy", "open") and not excluded
        warn = []
        if g["status"] == "needs_history":
            warn.append("I can't map this title to an address (browser history access is off)")
        elif g["status"] in ("ambiguous", "unmatched", "unreadable"):
            warn.append(f"title {g['status']}: not opened")
        if excluded:
            warn.append("protected site: not opened")
        rows.append({"label": f"tab {i}", "title": title, "url": g.get("url"), "grounded": g.get("source", ""),
                     "will_run": will, "warnings": warn, "untrusted_source": True, "status": g["status"]})
        steps.append({"order": i, "title": title, "url": g.get("url") if will else None, "status": g["status"]})
        flags += [f"tab{i}:{g['status']}"] if not will else []
    n = sum(1 for r in rows if r["will_run"])
    return {"title": f"Reopen {n} of {len(rows)} tabs in order", "rows": rows, "steps": steps, "flags": flags,
            "titles": titles, "browser": hint or "chrome",
            "say": f"I read {len(rows)} tab titles and can reopen {n}. Please confirm."}


def browser_tabs_plan(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    path = resolve_image(ctx, args.get("image"))
    if path is None:
        return {"error": {"code": "not_found", "message": "I couldn't find that screenshot."}}
    plan = _plan_tabs(ctx, path, args.get("browser"))
    if plan.get("error"):
        return plan
    ctx.extra["_tabs_plan"] = plan
    return {"steps": plan["steps"], "confirm": {"title": plan["title"], "rows": plan["rows"]}}


def browser_tabs_apply(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    plan = ctx.extra.get("_tabs_plan")
    if plan is None:
        path = resolve_image(ctx, args.get("image"))
        if path is None:
            return R.fail("not_found", "I couldn't find that screenshot.")
        plan = _plan_tabs(ctx, path, args.get("browser"))
    return _apply_tabs(ctx, plan)


def _apply_tabs(ctx: Ctx, plan: dict[str, Any]) -> dict[str, Any]:
    from .packs.skills_media import tab_open

    rec = Recorder("screen.apply", provenance="screenshot", clock=ctx.rt.monotonic)
    opened, failed = [], []
    for step in plan["steps"]:
        if not step.get("url"):
            continue
        r = tab_open(ctx, {"url": step["url"], "browser": plan.get("browser") if plan.get("browser") in ("chrome", "safari") else None})
        (opened if r["ok"] and r["verified"] else failed).append({"order": step["order"], "url": step["url"], "ok": r["ok"]})
    rec.add("act", "open tabs", bool(opened), channel="applescript", evidence={"opened": opened})
    ok = bool(opened) and not failed
    rec.add("verify", "each tab present", ok, channel="applescript", evidence={"failed": failed})
    skipped = len([s for s in plan["steps"] if not s.get("url")])
    say = (f"Reopened {len(opened)} tab{'s' if len(opened) != 1 else ''}"
           + (f"; {skipped} I couldn't identify." if skipped else ".")) if ok else None
    return rec.finish(ok, evidence={"opened": opened, "failed": failed, "skipped": skipped}, say=say)


# ----------------------------------------------------------- window arrangement --

def _plan_windows(ctx: Ctx, path: Path, hint: str | None) -> dict[str, Any]:
    if ctx.vision_fn is None:
        return {"error": {"code": "unsupported", "message": "Reading a window arrangement needs the brain's vision, which isn't connected here."}}
    vis = ctx.vision_fn(path.read_bytes(),
                        "List each window in this screenshot as {app,title,x,y,w,h} with x,y,w,h as fractions (0-1) of the screen.",
                        json_schema={"type": "object"})
    items = vis.get("windows") if isinstance(vis, dict) else None
    if not items:
        return {"error": {"code": "not_found", "message": "I couldn't make out any windows in that screenshot."}}
    from .world import World

    w = ctx.extra.setdefault("_world", World(ctx))
    live = w.query("windows", refresh=True)["evidence"]["items"]
    disp = w.query("displays")["evidence"]["items"]
    screen = disp[0]["frame"] if disp else {"x": 0, "y": 0, "w": 1710, "h": 1112}
    rows, steps = [], []
    for i, it in enumerate(items, start=1):
        cands = [x for x in live if str(it.get("app", "")).lower() == x["app"].lower()
                 and (not it.get("title") or _norm(str(it["title"])) in _norm(x["title"]) or not x["title"])]
        match = cands[0] if len(cands) == 1 else None
        frame = {"x": round(screen["x"] + float(it["x"]) * screen["w"]), "y": round(screen["y"] + float(it["y"]) * screen["h"]),
                 "w": round(float(it["w"]) * screen["w"]), "h": round(float(it["h"]) * screen["h"])}
        status = "matched" if match else ("ambiguous" if cands else "not_open")
        rows.append({"label": f"window {i}", "title": f"{it.get('app')} {it.get('title') or ''}".strip(),
                     "frame": frame, "grounded": f"{match['app']} window {match['id']}" if match else status,
                     "will_run": bool(match), "warnings": [] if match else [f"{status}: not moved"],
                     "untrusted_source": True})
        steps.append({"window": match, "frame": frame, "status": status, "app": it.get("app"),
                      "title": (match or {}).get("title")})
    n = sum(1 for r in rows if r["will_run"])
    return {"title": f"Arrange {n} window{'s' if n != 1 else ''} like the screenshot", "rows": rows, "steps": steps,
            "flags": [], "say": f"I can place {n} of {len(rows)} windows. Please confirm."}


def _apply_windows(ctx: Ctx, plan: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("screen.apply", provenance="screenshot", clock=ctx.rt.monotonic)
    ax = UI(ctx).ax
    moved, bad = [], []
    for s in plan["steps"]:
        if not s["window"]:
            continue
        f = s["frame"]
        res = ax.call("setframe", s["window"]["app"], "--title", s["window"]["title"] or "", "--x", str(f["x"]),
                      "--y", str(f["y"]), "--w", str(f["w"]), "--h", str(f["h"]))
        ok = bool(res.get("ok"))
        # read back from the window list
        back = ax.call("cgwindows", "--owner", s["window"]["app"])
        got = next((w for w in back.get("windows", []) if w["number"] == s["window"]["id"]), None)
        near = bool(got) and all(abs(got[k] - v) <= 12 for k, v in (("x", f["x"]), ("y", f["y"]), ("w", f["w"]), ("h", f["h"])))
        (moved if ok and near else bad).append({"app": s["app"], "frame": f, "now": got})
    rec.add("act", "set window frames", bool(moved), channel="ax")
    ok = bool(moved) and not bad
    rec.add("verify", "frames read back", ok, channel="ax", evidence={"bad": bad})
    return rec.finish(ok, evidence={"moved": moved, "failed": bad},
                      say=f"Arranged {len(moved)} window{'s' if len(moved) != 1 else ''}." if ok else None)


# --------------------------------------------------------------- settings toggles --

def _plan_toggles(ctx: Ctx, path: Path, hint: str | None) -> dict[str, Any]:
    items: list[dict[str, Any]] | None = ctx.extra.get("toggle_override")
    if items is None:
        if ctx.vision_fn is None:
            return {"error": {"code": "unsupported", "message": "Reading toggle states from a screenshot needs the brain's vision, which isn't connected here."}}
        vis = ctx.vision_fn(path.read_bytes(), "List each switch or checkbox as {label, on(boolean)}.",
                            json_schema={"type": "object"})
        items = vis.get("toggles") if isinstance(vis, dict) else None
    if not items:
        return {"error": {"code": "not_found", "message": "I couldn't find any toggles in that screenshot."}}
    scope = ctx.extra.get("toggle_scope") or {"app": hint}
    ui = UI(ctx)
    rows, steps = [], []
    for it in items:
        label, want = str(it["label"]), bool(it["on"])
        sel = {**scope, "role": "checkbox", "name": label, "exact": True}
        found = ui.find(sel, limit=2)
        if not found["ok"]:
            rows.append({"label": label, "will_run": False, "warnings": ["not found on this screen"], "untrusted_source": True,
                         "grounded": ""})
            steps.append({"label": label, "status": "not_found"})
            continue
        m = found["evidence"]["matches"][0]
        have = bool(m.get("checked")) if m.get("checked") is not None else str(m.get("value")) in ("1", "true", "True")
        todo = have != want
        rows.append({"label": label, "will_run": todo, "warnings": [] if todo else ["already as in the screenshot"],
                     "grounded": f"now {'on' if have else 'off'}, screenshot {'on' if want else 'off'}", "untrusted_source": True})
        steps.append({"label": label, "want": want, "have": have, "selector": sel, "status": "change" if todo else "same"})
    n = sum(1 for r in rows if r["will_run"])
    return {"title": f"Change {n} setting{'s' if n != 1 else ''} to match the screenshot", "rows": rows, "steps": steps,
            "flags": [], "scope": scope, "say": f"{n} setting{'s differ' if n != 1 else ' differs'}. Please confirm."}


def _apply_toggles(ctx: Ctx, plan: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("screen.apply", provenance="screenshot", clock=ctx.rt.monotonic)
    ui = UI(ctx)
    done, bad = [], []
    for s in plan["steps"]:
        if s["status"] != "change":
            continue
        r = ui.act(s["selector"], "press", restore_focus=False)
        ctx.rt.sleep(0.2)
        f = ui.find(s["selector"], limit=1)
        m = f["evidence"]["matches"][0] if f["ok"] else {}
        now = bool(m.get("checked")) if m.get("checked") is not None else str(m.get("value")) in ("1", "true", "True")
        (done if r["ok"] and now == s["want"] else bad).append({"label": s["label"], "now": now, "want": s["want"]})
    rec.add("act", "press toggles", bool(done), channel="dom")
    ok = bool(done) and not bad
    rec.add("verify", "states read back", ok, evidence={"bad": bad})
    return rec.finish(ok, evidence={"changed": done, "failed": bad},
                      say=f"Changed {len(done)} setting{'s' if len(done) != 1 else ''}." if ok else None)
