"""Skills: Ghostty and coding-CLI sessions (T1)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .. import layout_tree as lt
from .. import receipts as R
from ..receipts import Recorder
from ..registry import Ctx, skill
from . import coding_sessions as cs
from . import terminal_restore as tr
from .common import cache_plan, cached_plan, ghostty, session_index, shell_risk, state_dir
from .ghostty import GhosttyError

SCHEMA_STR = {"type": "string"}


def _gerr(exc: GhosttyError, rec: Recorder | None = None) -> dict[str, Any]:
    return R.fail(exc.code, exc.message, fix=exc.fix, retryable=exc.code in ("timeout", "not_found"))


# ---------------------------------------------------------------- ghostty ----

@skill("ghostty.windows_list", "List Ghostty windows, tabs and panes.",
       ["list ghostty windows", "what terminals are open", "list my terminal tabs", "show terminal panes",
        "which terminals do i have open"], risk="read", effect=False, channels=["applescript"],
       permissions=["automation:Ghostty"])
def windows_list(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("ghostty.windows_list", clock=ctx.rt.monotonic)
    try:
        terms = ghostty(ctx).inventory(only_windows=(ctx.window_ids or {}).get("ghostty"))
    except GhosttyError as exc:
        return _gerr(exc)
    rec.add("perceive", "inventory", True, channel="applescript", evidence={"terminals": len(terms)})
    wins = {t.window_id for t in terms}
    tabs = {(t.window_id, t.tab_id) for t in terms}
    rows = [t.to_dict() for t in terms]
    return rec.finish(True, verified=True, evidence={"terminals": rows, "windows": len(wins), "tabs": len(tabs)},
                      say=f"Ghostty has {len(wins)} window{'s' if len(wins) != 1 else ''}, "
                          f"{len(tabs)} tab{'s' if len(tabs) != 1 else ''} and {len(terms)} pane"
                          f"{'s' if len(terms) != 1 else ''}.")


def _cmd_risk(args: dict[str, Any], ctx: Any = None) -> tuple[str, str | None]:
    cmd = str(args.get("command") or "")
    if not cmd:
        return "write", None
    return "run", shell_risk(cmd)


@skill("ghostty.new_tab", "Open a new Ghostty tab (new window if none), optionally in a folder with a command.",
       ["open a new ghostty tab", "new terminal tab", "open a terminal tab", "new tab in ghostty",
        "open a new terminal tab in"], risk="dynamic", risk_fn=_cmd_risk, channels=["applescript"],
       permissions=["automation:Ghostty"],
       args_schema={"type": "object", "properties": {"window_id": SCHEMA_STR, "cwd": SCHEMA_STR,
                                                      "command": SCHEMA_STR}})
def new_tab(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("ghostty.new_tab", clock=ctx.rt.monotonic)
    g = ghostty(ctx)
    cwd = str(args.get("cwd") or "")
    try:
        win = args.get("window_id") or g.front_window()
        if win:
            made = g.new_tab(win, cwd=cwd)
            kind = "tab"
        else:
            made = g.new_window(cwd=cwd)
            win, kind = made["window_id"], "window"
        rec.add("act", f"new {kind}", True, channel="applescript", evidence=made)
        cmd = str(args.get("command") or "")
        if cmd:
            g.input_text(made["terminal_id"], cmd)
            rec.add("act", "type command", True, channel="applescript")
        ctx.rt.sleep(0.4)
        inv = [t for t in g.inventory(only_windows=[win]) if t.terminal_id == made["terminal_id"]]
    except GhosttyError as exc:
        return _gerr(exc)
    ok = bool(inv) and (not cwd or Path(inv[0].cwd).resolve() == Path(cwd).expanduser().resolve())
    rec.add("verify", "terminal exists with cwd", ok, channel="applescript",
            evidence={"cwd": inv[0].cwd if inv else None})
    return rec.finish(ok, evidence={"window_id": win, "tab_id": made["tab_id"], "terminal_id": made["terminal_id"],
                                    "opened": kind, "cwd": inv[0].cwd if inv else None},
                      undo={"skill": "ghostty.close", "args": {"tab_id": made["tab_id"], "window_id": win,
                                                              "created_by_hands": True},
                            "description": f"close the {kind} that was opened"},
                      say=f"Opened a new Ghostty {kind}." if ok else None,
                      err=None if ok else R.error("unverified", "the new terminal was not found afterwards"))


@skill("ghostty.new_window", "Open a new Ghostty window.",
       ["open a new ghostty window", "new terminal window", "open a terminal window"],
       risk="low", channels=["applescript"], permissions=["automation:Ghostty"],
       args_schema={"type": "object", "properties": {"cwd": SCHEMA_STR}})
def new_window(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("ghostty.new_window", clock=ctx.rt.monotonic)
    g = ghostty(ctx)
    try:
        made = g.new_window(cwd=str(args.get("cwd") or ""))
        ctx.rt.sleep(0.3)
        ok = made["window_id"] in g.window_ids()
    except GhosttyError as exc:
        return _gerr(exc)
    rec.add("act", "new window", True, channel="applescript", evidence=made)
    rec.add("verify", "window listed", ok, channel="applescript")
    return rec.finish(ok, evidence=made, say="Opened a new Ghostty window." if ok else None,
                      undo={"skill": "ghostty.close", "args": {"window_id": made["window_id"],
                                                              "created_by_hands": True},
                            "description": "close the window that was opened"})


@skill("ghostty.split", "Split a Ghostty pane right, left, down or up.",
       ["split the terminal", "split this pane", "split ghostty right", "split terminal down"],
       risk="low", channels=["applescript"], permissions=["automation:Ghostty"],
       args_schema={"type": "object", "required": ["terminal_id"],
                    "properties": {"terminal_id": SCHEMA_STR, "direction": {"type": "string",
                                   "enum": ["right", "left", "down", "up"]}, "cwd": SCHEMA_STR}})
def split(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("ghostty.split", clock=ctx.rt.monotonic)
    g = ghostty(ctx)
    try:
        before = {t.terminal_id for t in g.inventory()}
        new = g.split(args["terminal_id"], args.get("direction", "right"), cwd=str(args.get("cwd") or ""))
        after = {t.terminal_id for t in g.inventory()}
    except GhosttyError as exc:
        return _gerr(exc)
    ok = new in after and new not in before
    rec.add("act", "split", True, channel="applescript", evidence={"terminal_id": new})
    rec.add("verify", "new pane listed", ok, channel="applescript")
    return rec.finish(ok, evidence={"terminal_id": new}, say="Split the pane." if ok else None,
                      undo={"skill": "ghostty.close", "args": {"terminal_id": new, "created_by_hands": True},
                            "description": "close the new pane"})


def _send_risk(args: dict[str, Any], ctx: Any = None) -> tuple[str, str | None]:
    text = str(args.get("text") or "")
    if args.get("enter", True) is False:
        return "write", None
    return "run", shell_risk(text)


@skill("ghostty.send_input", "Type text (and Enter) into a Ghostty pane.",
       ["type into the terminal", "run this in the terminal", "send input to the pane", "type in ghostty"],
       risk="dynamic", risk_fn=_send_risk, channels=["applescript"], permissions=["automation:Ghostty"],
       args_schema={"type": "object", "required": ["terminal_id", "text"],
                    "properties": {"terminal_id": SCHEMA_STR, "text": SCHEMA_STR, "enter": {"type": "boolean"}}})
def send_input(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("ghostty.send_input", clock=ctx.rt.monotonic)
    g = ghostty(ctx)
    text = str(args["text"])
    try:
        g.input_text(args["terminal_id"], text, enter=bool(args.get("enter", True)))
        rec.add("act", "input text", True, channel="applescript", evidence={"chars": len(text)})
        ctx.rt.sleep(0.35)
        screen = g.read_text(args["terminal_id"])
    except GhosttyError as exc:
        return _gerr(exc)
    flat = re.sub(r"\s+", "", screen)
    ok = re.sub(r"\s+", "", text.strip()) in flat
    rec.add("verify", "text visible in pane", ok, channel="applescript")
    return rec.finish(ok, evidence={"tail": [ln for ln in screen.splitlines() if ln.strip()][-3:], "untrusted": True},
                      say="Done." if ok else None,
                      err=None if ok else R.error("unverified", "the text did not show up in the pane"))


@skill("ghostty.read_pane", "Read the visible text of a Ghostty pane.",
       ["read the terminal", "what does the pane say", "read terminal output", "what's in that terminal"],
       risk="read", effect=False, channels=["applescript"], permissions=["automation:Ghostty"],
       args_schema={"type": "object", "required": ["terminal_id"], "properties": {"terminal_id": SCHEMA_STR}})
def read_pane(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("ghostty.read_pane", clock=ctx.rt.monotonic)
    try:
        text = ghostty(ctx).read_text(args["terminal_id"])
    except GhosttyError as exc:
        return _gerr(exc)
    rec.add("perceive", "read screen", True, channel="applescript")
    return rec.finish(True, verified=True, evidence={"text": text[-6000:], "untrusted": True},
                      say="I read the pane.")


@skill("ghostty.focus", "Bring a Ghostty pane or window to the front.",
       ["focus the terminal", "switch to that pane", "bring ghostty forward"], risk="low",
       channels=["applescript"], permissions=["automation:Ghostty"],
       args_schema={"type": "object", "properties": {"terminal_id": SCHEMA_STR}})
def focus(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("ghostty.focus", clock=ctx.rt.monotonic)
    try:
        ghostty(ctx).focus(args["terminal_id"])
    except (GhosttyError, KeyError) as exc:
        return _gerr(exc) if isinstance(exc, GhosttyError) else R.fail("invalid_args", "terminal_id required")
    rec.add("act", "focus", True, channel="applescript")
    return rec.finish(True, verified=False, say=None)


def _close_risk(args: dict[str, Any], ctx: Any = None) -> tuple[str, str | None]:
    return ("write", None) if args.get("created_by_hands") else ("run", "delete")


@skill("ghostty.close", "Close a Ghostty tab, window or pane.",
       ["close the terminal tab", "close that pane", "close ghostty window", "close this terminal"],
       risk="dynamic", risk_fn=_close_risk, channels=["applescript"], permissions=["automation:Ghostty"],
       args_schema={"type": "object", "properties": {"window_id": SCHEMA_STR, "tab_id": SCHEMA_STR,
                                                      "terminal_id": SCHEMA_STR,
                                                      "created_by_hands": {"type": "boolean"}}})
def close(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("ghostty.close", clock=ctx.rt.monotonic)
    g = ghostty(ctx)
    try:
        if args.get("tab_id") and args.get("window_id"):
            before = {t.tab_id for t in g.inventory()}
            g.close_tab(args["window_id"], args["tab_id"])
            ctx.rt.sleep(0.4)
            after = {t.tab_id for t in g.inventory()} if g.window_ids() else set()
            ok = args["tab_id"] in before and args["tab_id"] not in after
        elif args.get("window_id"):
            g.close_window(args["window_id"])
            ctx.rt.sleep(0.4)
            ok = args["window_id"] not in g.window_ids()
        elif args.get("terminal_id"):
            g.close_terminal(args["terminal_id"])
            ctx.rt.sleep(0.4)
            ok = args["terminal_id"] not in {t.terminal_id for t in g.inventory()}
        else:
            return R.fail("invalid_args", "close needs tab_id+window_id, window_id or terminal_id")
    except GhosttyError as exc:
        return _gerr(exc)
    rec.add("act", "close", True, channel="applescript")
    rec.add("verify", "gone", ok, channel="applescript")
    return rec.finish(ok, say="Closed it." if ok else None)


def _layouts_dir(ctx: Ctx) -> Path:
    d = state_dir(ctx) / "layouts"
    d.mkdir(parents=True, exist_ok=True)
    return d


@skill("ghostty.layout_save", "Save the current Ghostty tab's split layout, folders and commands by name.",
       ["save this terminal layout", "remember my terminal layout", "save my ghostty layout"],
       risk="low", channels=["applescript", "ax"], permissions=["automation:Ghostty", "accessibility"],
       args_schema={"type": "object", "required": ["name", "window_id"],
                    "properties": {"name": SCHEMA_STR, "window_id": SCHEMA_STR, "tab_id": SCHEMA_STR}})
def layout_save(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("ghostty.layout_save", clock=ctx.rt.monotonic)
    name = str(args["name"]).strip()
    if not name or "/" in name or len(name) > 80:
        return R.fail("invalid_args", "That layout name isn't usable.")
    g = ghostty(ctx)
    try:
        terms = [t for t in g.inventory(only_windows=[args["window_id"]])
                 if not args.get("tab_id") or t.tab_id == args["tab_id"]]
        sel = [t for t in terms if t.selected] or terms
        tab = sel[0].tab_id if sel else None
        terms = [t for t in terms if t.tab_id == tab]
        from .ghostty import _tree_from_rects

        m = g.measure(g.window_title(args["window_id"]), expect_panes=len(terms))
        tree = _tree_from_rects(m["panes"]) if m else None
    except GhosttyError as exc:
        return _gerr(exc)
    if tree is None or lt.count_panes(tree) != len(terms):
        return R.fail("unsupported", "I couldn't read that tab's pane geometry, so I won't save a guess.",
                      fix="Grant Accessibility to Evie (System Settings > Privacy & Security > Accessibility).")
    lt.renumber(tree)
    spec = {"name": name, "tree": tree, "panes": [{"cwd": t.cwd, "command": ""} for t in terms],
            "saved_at": ctx.rt.now(), "source": "live"}
    path = _layouts_dir(ctx) / f"{name}.json"
    path.write_text(json.dumps(spec))
    back = json.loads(path.read_text())
    ok = back["panes"] == spec["panes"] and lt.same_partition(back["tree"], tree, tol=0.001)
    rec.add("act", "write layout", True, channel="fs", evidence={"path": str(path)})
    rec.add("verify", "read back layout", ok, channel="fs")
    return rec.finish(ok, evidence={"name": name, "panes": len(terms), "path": str(path)},
                      say=f"Saved the layout as {name}." if ok else None,
                      undo={"skill": "ghostty.layout_delete", "args": {"name": name}, "description": "delete the saved layout"})


@skill("ghostty.layout_restore", "Open a saved Ghostty layout in a new tab.",
       ["restore my terminal layout", "open my saved layout", "restore ghostty layout", "bring back my layout"],
       risk="confirm", channels=["applescript", "ax"], permissions=["automation:Ghostty", "accessibility"],
       args_schema={"type": "object", "required": ["name"],
                    "properties": {"name": SCHEMA_STR, "window_id": SCHEMA_STR, "mode": {"type": "string", "enum": ["new_tab", "new_window"]}}},
       plan=lambda ctx, args: _layout_plan(ctx, args))
def layout_restore(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    spec = _load_layout(ctx, args["name"])
    if spec is None:
        return R.fail("not_found", f"I don't have a saved layout called {args['name']}.")
    return _build_layout(ctx, "ghostty.layout_restore", spec["tree"], spec["panes"], args)


def _load_layout(ctx: Ctx, name: str) -> dict[str, Any] | None:
    p = _layouts_dir(ctx) / f"{str(name).strip()}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _layout_plan(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    spec = _load_layout(ctx, args.get("name", ""))
    if spec is None:
        return {"error": {"code": "not_found", "message": f"I don't have a saved layout called {args.get('name')}."}}
    rows = [{"label": f"pane {i + 1}", "cwd": p.get("cwd"), "command": p.get("command") or None,
             "will_run": bool(p.get("command")), "warnings": [], "untrusted_source": False}
            for i, p in enumerate(spec["panes"])]
    return {"steps": [{"op": "ghostty.new_tab_with_layout", "panes": len(rows)}],
            "confirm": {"title": f"Restore layout {spec['name']} ({len(rows)} panes)", "rows": rows,
                        "layout_ascii": lt.render_ascii(spec["tree"])}}


def _build_layout(ctx: Ctx, skill_name: str, tree: dict[str, Any], panes: list[dict[str, Any]],
                  args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder(skill_name, clock=ctx.rt.monotonic)
    g = ghostty(ctx)
    leaves = [{"cwd": p.get("cwd") or str(ctx.rt.home()), "command": p.get("command") or ""} for p in panes]
    try:
        win = args.get("window_id") or (None if args.get("mode") == "new_window" else g.front_window())
        built = g.build(tree, leaves, window_id=win, new_window=args.get("mode") == "new_window" or not win)
        ctx.rt.sleep(1.2)
        checks = tr.verify_panes(g, built, [{"pane": p["id"], "cli": None, "session_id": None, "cwd": leaves[i]["cwd"],
                                             "command": leaves[i]["command"] or None, "skipped": None}
                                            for i, p in enumerate(lt.panes(lt.canonicalize(tree)))],
                                 rt=ctx.rt, home=ctx.rt.home())
    except GhosttyError as exc:
        return _gerr(exc)
    audit = built.get("audit") or {}
    rec.add("act", "build layout", True, channel="applescript", evidence={"timings_ms": built.get("timings_ms")})
    ratio_ok = audit.get("ok")
    rec.add("verify", "ratios", bool(ratio_ok), channel="ax", evidence=audit)
    panes_ok = all(c["ok"] for c in checks)
    rec.add("verify", "panes read back", panes_ok, channel="applescript", evidence={"panes": checks})
    ok = panes_ok
    caveat = "" if ratio_ok else " (I couldn't verify the split sizes)"
    return rec.finish(ok, verified=ok and panes_ok, evidence={"built": {k: v for k, v in built.items() if k != "terminals"},
                                                            "panes": checks, "terminals": built["terminals"]},
                      say=(f"Opened a new tab with {len(checks)} panes{caveat}." if ok else None),
                      err=None if ok else R.error("unverified", "some panes did not read back as expected"),
                      undo={"skill": "ghostty.close", "args": {"tab_id": built["tab_id"],
                                                              "window_id": built["window_id"],
                                                              "created_by_hands": True},
                            "description": "close the tab that was opened"})


@skill("ghostty.new_tab_with_layout", "Open a new Ghostty tab with an explicit split layout, folders and commands.",
       ["open a tab with this layout", "build this terminal layout", "new tab with splits"],
       risk="confirm", channels=["applescript", "ax"], permissions=["automation:Ghostty", "accessibility"],
       args_schema={"type": "object", "required": ["layout", "panes"],
                    "properties": {"layout": {"type": "object"}, "panes": {"type": "array"},
                                   "window_id": SCHEMA_STR, "mode": {"type": "string", "enum": ["new_tab", "new_window"]}}},
       plan=lambda ctx, args: {
           "steps": [{"op": "build", "panes": len(args.get("panes", []))}],
           "confirm": {"title": f"Open a new tab with {len(args.get('panes', []))} panes",
                       "rows": [{"label": f"pane {i + 1}", "cwd": p.get("cwd"), "command": p.get("command") or None,
                                 "will_run": bool(p.get("command")), "warnings": [], "untrusted_source": False}
                                for i, p in enumerate(args.get("panes", []))],
                       "layout_ascii": lt.render_ascii(args["layout"]) if args.get("layout") else ""},
           "physical": any(shell_risk(str(p.get("command") or "")) for p in args.get("panes", []))})
def new_tab_with_layout(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    tree = args["layout"]
    if lt.count_panes(tree) != len(args["panes"]):
        return R.fail("invalid_args", "The layout and the pane list don't have the same number of panes.")
    lt.renumber(tree)
    return _build_layout(ctx, "ghostty.new_tab_with_layout", tree, args["panes"], args)


# ---------------------------------------------------------- coding sessions ---

def _providers(ctx: Ctx) -> dict[str, cs.Provider]:
    return cs.providers(ctx.rt, ctx.home)


@skill("coding_sessions.discover", "Which coding CLIs are installed, their resume syntax and session stores.",
       ["which coding clis do i have", "discover coding sessions", "what coding agents are installed",
        "list coding cli tools"], risk="read", effect=False, channels=["cli", "fs"])
def discover(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    info = cs.discover(ctx.rt, ctx.home)
    names = [n for n, i in info.items() if i["installed"]]
    return R.receipt(True, verified=True, skill="coding_sessions.discover", channel="cli",
                     evidence={"clis": info},
                     say=("I found " + ", ".join(names) + ".") if names else "I didn't find any coding CLIs.")


@skill("coding_sessions.list_recent", "List recent coding-CLI sessions with folder and title.",
       ["list my recent coding sessions", "recent claude sessions", "show my codex sessions", "which sessions did i have",
        "recent coding agent sessions"], risk="read", effect=False, channels=["fs"],
       args_schema={"type": "object", "properties": {"cli": SCHEMA_STR, "cwd": SCHEMA_STR, "limit": {"type": "integer"}}})
def list_recent(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rows = session_index(ctx).rows(force=True)
    if args.get("cli"):
        rows = [r for r in rows if r.cli == args["cli"]]
    if args.get("cwd"):
        want = str(Path(args["cwd"]).expanduser())
        rows = [r for r in rows if r.cwd == want]
    limit = int(args.get("limit") or 10)
    out = [r.to_dict() for r in rows[:limit]]
    return R.receipt(True, verified=True, skill="coding_sessions.list_recent", channel="fs",
                     evidence={"sessions": out, "total": len(rows), "untrusted": True},
                     say=f"I found {len(rows)} session{'s' if len(rows) != 1 else ''}.")


@skill("coding_sessions.validate_id", "Check a session id against the CLI's own store (corrects OCR look-alikes).",
       ["is this session id real", "validate session id", "check that session id exists"], risk="read",
       effect=False, channels=["fs"],
       args_schema={"type": "object", "required": ["id"], "properties": {"id": SCHEMA_STR, "cli": SCHEMA_STR, "cwd": SCHEMA_STR}})
def validate_id(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    g = cs.ground_id(args["id"], session_index(ctx).rows(), cwd=args.get("cwd"), cli=args.get("cli"))
    say = {"exact": "That session exists.", "corrected": "That session exists after fixing look-alike characters.",
           "completed": "That is the start of exactly one real session.",
           "ambiguous": "More than one real session matches that id, so I won't pick one.",
           "unmatched": "No session matches that id.", "unreadable": "That id isn't readable."}[g["status"]]
    return R.receipt(True, verified=True, skill="coding_sessions.validate_id", channel="fs", evidence=g, say=say)


@skill("coding_sessions.resume_command", "The verified command that resumes a session (does not run it).",
       ["what is the resume command for", "how do i resume that session", "resume command"], risk="read",
       effect=False, channels=["cli", "fs"],
       args_schema={"type": "object", "required": ["id"], "properties": {"id": SCHEMA_STR, "cli": SCHEMA_STR}})
def resume_command(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    g = cs.ground_id(args["id"], session_index(ctx).rows(), cli=args.get("cli"))
    if not g["executable"]:
        return R.fail("not_found", f"I can't confirm that id ({g['status']}).", evidence=g)
    s = g["session"]
    cmd = _providers(ctx)[s["cli"]].resume_command(s["id"])
    if cmd is None:
        return R.fail("unsupported", f"I can't verify how {s['cli']} resumes sessions.")
    return R.receipt(True, verified=True, skill="coding_sessions.resume_command", channel="cli",
                     evidence={"command": cmd, "cwd": s["cwd"], "session": s}, say=cmd)


@skill("coding_sessions.detect_running", "Is this coding session already open in some terminal?",
       ["is that session already running", "is this session open somewhere", "is the session already open"],
       risk="read", effect=False, channels=["cli"],
       args_schema={"type": "object", "required": ["id"], "properties": {"id": SCHEMA_STR, "cli": SCHEMA_STR}})
def detect_running(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    g = cs.ground_id(args["id"], session_index(ctx).rows(), cli=args.get("cli"))
    if not g["executable"]:
        return R.fail("not_found", f"I can't confirm that id ({g['status']}).", evidence=g)
    s = g["session"]
    hit = cs.running_elsewhere(cs.CliSession(**{k: s[k] for k in ("cli", "id", "cwd", "title", "updated_at", "store")}),
                               ctx.rt, ctx.home)
    say = "It isn't open anywhere I can see." if not hit else (
        f"Yes, it's already open (pid {hit['pid']}{', ' + hit['tty'] if hit['tty'] else ''})."
        if hit["level"] == "strong" else "It may already be open; the same tool is running in that folder.")
    return R.receipt(True, verified=True, skill="coding_sessions.detect_running", channel="cli",
                     evidence={"running": hit, "session": s}, say=say)


def _last_image(ctx: Ctx, image: str) -> Path | None:
    if image and image != "last":
        p = Path(image).expanduser()
        return p if p.is_file() else None
    forced = ctx.extra.get("last_image")
    if forced:
        return Path(forced)
    try:
        from app.fabric.screen_pipeline import resolve_last

        found = resolve_last()
        if found and found.get("path"):
            return Path(found["path"])
    except Exception:
        pass
    return None


def _restore_plan(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    img = _last_image(ctx, str(args.get("image") or "last"))
    if img is None:
        return {"error": {"code": "not_found", "message": "I couldn't find a screenshot to read.",
                          "fix": "Take a screenshot of the terminal layout, then ask again."}}
    key = f"restore:{img}:{img.stat().st_mtime_ns}:{args.get('window_id')}:{args.get('mode')}"
    plan = cached_plan(key)
    if plan is None:
        plan = tr.analyze(img, rt=ctx.rt, home=ctx.home, ocr_fn=ctx.ocr_fn, vision_fn=ctx.vision_fn,
                          index=session_index(ctx))
        cache_plan(key, plan)
    ctx.extra["plan_key"] = key
    prelude = str(args.get("prelude") or "")
    card = tr.confirm_card(plan)
    if prelude:
        for row in card["rows"]:
            if row.get("command"):
                row["command"] = f"{prelude}; {row['command']}"
    steps = [{"pane": p["id"], "cli": p["cli"], "command": (f"{prelude}; {p['command']}" if prelude and p["command"] else p["command"]),
              "cwd": p["cwd"], "will_run": p["executable"], "flags": p["flags"]} for p in plan["panes"]]
    physical = any(shell_risk(((prelude + "; ") if prelude else "") + (p["command"] or "")) for p in plan["panes"])
    return {"steps": steps, "confirm": card, "physical": physical,
            "evidence": {"layout_ascii": plan["layout_ascii"], "flags": plan["flags"], "pane_count": plan["pane_count"]}}


@skill("coding_sessions.restore_from_screenshot",
       "From a screenshot of a Ghostty layout: open a new tab with the same splits and resume the coding sessions shown.",
       ["open a new tab with the same layout and resume those coding sessions", "restore my terminal layout from the screenshot",
        "look at my last screenshot and resume those sessions", "rebuild this layout from the screenshot",
        "resume the sessions in my screenshot", "same layout as the screenshot"],
       risk="confirm", channels=["ocr", "fs", "applescript", "ax"],
       permissions=["automation:Ghostty", "accessibility", "screen_recording"],
       args_schema={"type": "object", "properties": {"image": SCHEMA_STR, "window_id": SCHEMA_STR,
                                                      "mode": {"type": "string", "enum": ["new_tab", "new_window"]},
                                                      "allow_duplicates": {"type": "array"},
                                                      # shell setup typed before each resume command, e.g. exporting a
                                                      # variable; classified by the same shell-risk rules as the command
                                                      "prelude": SCHEMA_STR}},
       plan=_restore_plan)
def restore_from_screenshot(ctx: Ctx, args: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder("coding_sessions.restore_from_screenshot", provenance="screenshot", clock=ctx.rt.monotonic)
    img = _last_image(ctx, str(args.get("image") or "last"))
    if img is None:
        return R.fail("not_found", "I couldn't find a screenshot to read.")
    key = f"restore:{img}:{img.stat().st_mtime_ns}:{args.get('window_id')}:{args.get('mode')}"
    plan = cached_plan(key) or tr.analyze(img, rt=ctx.rt, home=ctx.home, ocr_fn=ctx.ocr_fn,
                                          vision_fn=ctx.vision_fn, index=session_index(ctx))
    rec.add("resolve", "plan from screenshot", True, channel="ocr",
            evidence={"panes": plan["pane_count"], "executable": plan["executable_panes"], "flags": plan["flags"]})
    g = ghostty(ctx)
    try:
        win = args.get("window_id") or (None if args.get("mode") == "new_window" else g.front_window())
        out = tr.apply(plan, driver=g, window_id=win, new_window=args.get("mode") == "new_window" or not win,
                       allow_duplicates=args.get("allow_duplicates"), rt=ctx.rt, home=ctx.home,
                       pane_prelude=str(args.get("prelude") or "") or None)
    except GhosttyError as exc:
        return _gerr(exc)
    rows = out["panes"]
    ran = [r for r in rows if r.get("expected_command")]
    skipped = [r for r in rows if r.get("skipped")]
    audit = out["built"].get("audit") or {}
    rec.add("act", "build tab and resume", True, channel="applescript",
            evidence={"timings_ms": out["built"].get("timings_ms")})
    rec.add("verify", "split sizes", bool(audit.get("ok")), channel="ax", evidence=audit)
    all_ok = all(r["ok"] for r in rows)
    rec.add("verify", "panes read back", all_ok, channel="applescript", evidence={"panes": rows})
    notes = []
    if skipped:
        notes.append(f"{len(skipped)} already running")
    unexec = plan["pane_count"] - plan["executable_panes"]
    if unexec:
        notes.append(f"{unexec} left as plain shells because I couldn't confirm their session")
    if not audit.get("ok"):
        notes.append("split sizes unverified")
    say = f"Opened a new tab with {plan['pane_count']} panes and resumed {len(ran)} session{'s' if len(ran) != 1 else ''}"
    say += f" ({'; '.join(notes)})." if notes else "."
    return rec.finish(all_ok, verified=all_ok, say=say if all_ok else None,
                      evidence={"plan": {"panes": [{k: p[k] for k in ("id", "cli", "id_status", "cwd", "command", "flags")}
                                                   for p in plan["panes"]], "layout_ascii": plan["layout_ascii"]},
                                "result": rows, "audit": audit},
                      err=None if all_ok else R.error("unverified", "some panes did not read back correctly"),
                      undo=out["undo"])


_ = time
