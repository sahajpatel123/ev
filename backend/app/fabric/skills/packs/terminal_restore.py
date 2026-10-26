"""T1: "open a new tab with the layout from my last screenshot and resume those sessions".

screenshot -> split tree (pixels) -> per-pane text (OCR) -> resume commands ->
GROUNDED against each coding CLI's own session store -> a plan the owner
confirms -> a new Ghostty tab built and read back.

Safety rules baked in:
* Commands run in the new panes are REGENERATED from the verified resume syntax
  and the grounded id. Nothing typed is copied from the screenshot.
* Only ids that exist in a local store (exactly, or after unambiguous
  confusable correction) are executable. Ambiguous/unmatched/unreadable ids
  leave a plain shell pane and a flag.
* cwd comes from the session's own record; the screenshot's path is only a
  cross-check.
* A session already running elsewhere is not started a second time unless the
  caller names it in `allow_duplicates`.
* Everything derived from the image carries provenance "screenshot" (R7).
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import layout_tree as lt
from ..imaging import Raster, load_raster
from ..runtime import Runtime, default_runtime
from . import coding_sessions as cs

OcrFn = Callable[[Path], dict[str, Any]]
VisionFn = Callable[..., dict[str, Any]]

ERROR_WORDS = re.compile(
    r"(command not found|no such file or directory|no conversation found|session .{0,40}not found|"
    r"could not find|unknown (option|command)|invalid session|traceback \(most recent)", re.I)


# ------------------------------------------------------------------ OCR -----

def evvision_ocr(rt: Runtime | None = None, binary: str | None = None) -> OcrFn:
    """OCR through the Apple Vision helper (`evvision ocr <path>`)."""
    rt = rt or default_runtime()

    def run(path: Path) -> dict[str, Any]:
        exe = binary or _ocr_binary()
        if not exe:
            return {"ok": False, "error": "evvision helper not found", "lines": []}
        p = rt.run([exe, "ocr", str(path), "--level", "accurate"], timeout=60)
        if not p.ok:
            return {"ok": False, "error": (p.err or p.out)[:200], "lines": []}
        try:
            data = json.loads(p.out)
        except ValueError:
            return {"ok": False, "error": "evvision returned non-JSON", "lines": []}
        return {"ok": True, "lines": data.get("lines") or [], "text": data.get("text", "")}

    return run


def _ocr_binary() -> str | None:
    try:
        from app.config import settings

        if settings.hands_ocr_binary:
            return settings.hands_ocr_binary
    except Exception:
        pass
    try:
        from app.vision.providers import find_evvision_binary

        found = find_evvision_binary()
        if found and (Path(found).exists() or "/" not in found):
            return found
    except Exception:
        pass
    return None


# ------------------------------------------------------------ pane content --

def assign_lines(lines: list[dict[str, Any]], tree: dict[str, Any], analysis: dict[str, Any],
                 image_w: int, image_h: int) -> dict[int, list[dict[str, Any]]]:
    """Give each OCR line to the pane containing its centre. Boxes are normalised
    to the whole image (top-left origin); panes are in analysis pixels."""
    sx = analysis["width"] / max(1, image_w) if image_w else 1.0
    out: dict[int, list[dict[str, Any]]] = {p["id"]: [] for p in lt.panes(tree)}
    for line in lines:
        box = line.get("bounding_box") or line.get("box") or {}
        try:
            cx = (float(box.get("x", 0)) + float(box.get("width", 0)) / 2) * analysis["width"]
            cy = (float(box.get("y", 0)) + float(box.get("height", 0)) / 2) * analysis["height"]
        except (TypeError, ValueError):
            continue
        _ = sx
        for pane in lt.panes(tree):
            px, py, pw, ph = pane["px"]
            if px <= cx < px + pw and py <= cy < py + ph:
                out[pane["id"]].append({"text": str(line.get("text") or ""), "y": cy, "x": cx,
                                        "conf": float(line.get("confidence", 0.0))})
                break
    for items in out.values():
        items.sort(key=lambda r: (round(r["y"] / 6), r["x"]))
    return out


def pane_text(items: list[dict[str, Any]]) -> str:
    return "\n".join(i["text"] for i in items)


def position_label(rect: list[float]) -> str:
    x, y, w, h = rect
    cx, cy = x + w / 2, y + h / 2
    col = "left" if cx < 0.34 else "right" if cx > 0.66 else "middle"
    row = "top" if cy < 0.34 else "bottom" if cy > 0.66 else "middle"
    if w > 0.9:
        col = ""
    if h > 0.9:
        row = ""
    return "-".join(p for p in (row, col) if p) or "whole window"


# ---------------------------------------------------------------- analysis --

def analyze(image: str | Path | bytes, *, rt: Runtime | None = None, home: Path | None = None,
            ocr_fn: OcrFn | None = None, vision_fn: VisionFn | None = None,
            index: cs.SessionIndex | None = None, ocr_lines: list[dict[str, Any]] | None = None
            ) -> dict[str, Any]:
    """Build the grounded plan for a terminal screenshot. Performs no effect."""
    rt = rt or default_runtime()
    home = home or rt.home()
    raster = load_raster(image, max_dim=1000)
    analysis = lt.infer_layout(raster)
    tree = analysis["tree"]
    flags: list[str] = []
    if analysis.get("ratio_uncertain"):
        flags.append("ratios_uncertain")
    path: Path | None = Path(image) if isinstance(image, str | Path) else None
    lines = ocr_lines
    ocr_ok = lines is not None
    if lines is None and path is not None:
        got = (ocr_fn or evvision_ocr(rt))(path)
        ocr_ok, lines = bool(got.get("ok")), got.get("lines") or []
    if not ocr_ok:
        flags.append("ocr_unavailable")
    lines = lines or []
    vlm: dict[str, Any] | None = None
    if vision_fn is not None and path is not None:
        try:
            vlm = vision_fn(path.read_bytes(), _VISION_PROMPT, json_schema=_VISION_SCHEMA)
        except Exception as exc:  # never fatal: pixels + OCR still stand
            flags.append(f"vision_failed:{type(exc).__name__}")
    if vlm and isinstance(vlm.get("panes"), list) and len(vlm["panes"]) != analysis["pane_count"]:
        flags.append(f"layout_disagreement:pixels={analysis['pane_count']},vision={len(vlm['panes'])}")
    per_pane = assign_lines(lines, tree, analysis, raster.w, raster.h)
    idx = index or cs.SessionIndex(rt, home)
    sessions = idx.rows()
    panes_out: list[dict[str, Any]] = []
    leaves = lt.panes(tree)
    vlm_by_pos = _vlm_panes(vlm, leaves)
    for pane in leaves:
        text = pane_text(per_pane.get(pane["id"], []))
        if not text.strip() and vlm_by_pos.get(pane["id"]):
            text = str(vlm_by_pos[pane["id"]].get("text") or "")
        panes_out.append(_ground_pane(pane, text, sessions, rt, home))
    destination = {"app": "Ghostty", "mode": "new_tab", "window": "front"}
    executable = [p for p in panes_out if p["executable"]]
    return {"kind": "terminal_layout", "provenance": "screenshot", "untrusted": True,
            "image": {"path": str(path) if path else "", "w": raster.w, "h": raster.h},
            "layout": tree, "layout_ascii": lt.render_ascii(
                tree, labels={p["id"]: (p["cli"] or "shell") for p in panes_out}),
            "pane_count": analysis["pane_count"], "panes": panes_out, "destination": destination,
            "flags": flags, "executable_panes": len(executable),
            "ocr": {"ok": ocr_ok, "lines": len(lines)}, "vision": bool(vlm)}


def _ground_pane(pane: dict[str, Any], text: str, sessions: list[cs.CliSession],
                 rt: Runtime, home: Path) -> dict[str, Any]:
    cmds = cs.find_resume_commands(text)
    paths = [cs.expand_path(p, home) for p in cs.find_paths(text)]
    rec: dict[str, Any] = {
        "id": pane["id"], "rect": pane["rect"], "label": position_label(pane["rect"]),
        "ocr_text": text[:1200], "cli": None, "ocr_id": None, "id_status": "none",
        "session": None, "changes": [], "cwd": None, "ocr_cwd": paths[0] if paths else None,
        "command": None, "executable": False, "running_elsewhere": None, "flags": [],
        "untrusted_source": True}
    if not cmds:
        rec["cwd"] = _existing(paths[0]) if paths else None
        rec["flags"].append("no_resume_command_found")
        return rec
    # the most recent command in a pane is the last one printed
    found = cmds[-1]
    rec.update(cli=found["cli"], ocr_id=found["id_raw"])
    prov = cs.providers(rt, home)[found["cli"]]
    syntax = prov.syntax()
    if not syntax.verified:
        rec["flags"].append(f"resume_syntax_unverified:{syntax.reason}")
        rec["id_status"] = "unverifiable"
        return rec
    if syntax.mode == "index":
        rec["flags"].append("index_based_resume_not_automatable")
        rec["id_status"] = "unverifiable"
        return rec
    g = cs.ground_id(found["id_raw"], sessions, cwd=paths[0] if paths else None, cli=found["cli"])
    rec["id_status"], rec["changes"] = g["status"], g["changes"]
    if g["status"] in ("ambiguous",):
        rec["flags"].append("ambiguous_id:" + ",".join(c["id"][:13] for c in g["candidates"]))
        rec["candidates"] = g["candidates"]
        return rec
    if not g["executable"]:
        rec["flags"].append(f"id_{g['status']}")
        return rec
    sess = g["session"]
    rec["session"] = sess
    rec["command"] = prov.resume_command(sess["id"])
    cwd = sess["cwd"] or None
    if cwd and not Path(cwd).is_dir():
        rec["flags"].append("session_cwd_missing")
        rec["cwd"] = None
        rec["command"] = None
        return rec
    if paths and cwd and not _same(paths[0], cwd):
        rec["flags"].append(f"screenshot_cwd_differs:{paths[0]}")
    rec["cwd"] = cwd
    if cwd is None:
        rec["flags"].append("session_has_no_cwd")
        return rec
    hit = cs.running_elsewhere(cs.CliSession(**{k: sess[k] for k in
                                                ("cli", "id", "cwd", "title", "updated_at", "store")}),
                               rt, home)
    if hit:
        rec["running_elsewhere"] = hit
        rec["flags"].append(f"running_elsewhere:{hit['level']}")
    rec["executable"] = True
    return rec


def _existing(p: str) -> str | None:
    return p if Path(p).is_dir() else None


def _same(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b


_VISION_PROMPT = (
    "This is a screenshot of a terminal window split into panes. For each pane (reading order) "
    "return its fractional box within the terminal area (x,y,w,h in 0..1), the coding CLI shown "
    "(claude, codex, opencode, muse, gemini or none), the visible working directory, and the "
    "visible resume command text exactly as printed. Return JSON only.")
_VISION_SCHEMA = {"type": "object", "properties": {"panes": {"type": "array", "items": {
    "type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                                     "w": {"type": "number"}, "h": {"type": "number"},
                                     "cli": {"type": "string"}, "cwd": {"type": "string"},
                                     "text": {"type": "string"}}}}}}


def _vlm_panes(vlm: dict[str, Any] | None, leaves: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    if not vlm or not isinstance(vlm.get("panes"), list):
        return out
    for item in vlm["panes"]:
        try:
            cx = float(item["x"]) + float(item["w"]) / 2
            cy = float(item["y"]) + float(item["h"]) / 2
        except (KeyError, TypeError, ValueError):
            continue
        best = min(leaves, key=lambda p: abs(p["rect"][0] + p["rect"][2] / 2 - cx)
                   + abs(p["rect"][1] + p["rect"][3] / 2 - cy))
        out.setdefault(best["id"], item)
    return out


# ---------------------------------------------------------------- confirm ---

def confirm_card(plan: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for p in plan["panes"]:
        status = p["id_status"]
        grounded = ""
        if p["session"]:
            s = p["session"]
            grounded = f"{p['cli']} session {s['id'][:13]}… found ({status})"
            if s.get("title"):
                grounded += f' "{s["title"][:40]}"'
            if p["changes"]:
                grounded += "; read " + ", ".join(
                    f"{c.get('from', '?')}→{c.get('to', '?')}" if c["kind"] != "prefix" else "prefix completed"
                    for c in p["changes"][:4])
        elif p["cli"]:
            grounded = f"{p['cli']} id not trusted: {status}"
        warn = [f for f in p["flags"] if f.startswith(("running_elsewhere", "ambiguous", "id_", "session_cwd",
                                                       "screenshot_cwd", "resume_syntax", "index_based"))]
        rows.append({"label": f"pane {p['id']} ({p['label']})", "cwd": p["cwd"],
                     "command": p["command"] if p["executable"] else None,
                     "grounded": grounded, "will_run": bool(p["executable"]),
                     "warnings": warn, "untrusted_source": True})
    n = plan["executable_panes"]
    return {"title": f"Open a new Ghostty tab with {plan['pane_count']} panes and resume {n} session"
                     f"{'s' if n != 1 else ''}",
            "rows": rows, "layout_ascii": plan["layout_ascii"], "risk": "confirm",
            "physical": False, "expires_s": 300}


# ------------------------------------------------------------------ apply ---

def apply(plan: dict[str, Any], *, driver: Any, window_id: str | None = None,
          new_window: bool = False, allow_duplicates: list[str] | None = None,
          rt: Runtime | None = None, home: Path | None = None,
          settle_s: float = 2.0, ratio_tol: float = 0.03) -> dict[str, Any]:
    """Build the layout and resume the sessions; read every pane back."""
    rt = rt or driver.rt
    home = home or rt.home()
    allow = set(allow_duplicates or [])
    leaves_plan = {p["id"]: p for p in plan["panes"]}
    order = lt.panes(plan["layout"])
    leaves: list[dict[str, Any]] = []
    started: list[dict[str, Any]] = []
    for pane in order:
        info = leaves_plan[pane["id"]]
        cwd = info["cwd"] or str(home)
        cmd = None
        skipped = None
        if info["executable"]:
            sid = info["session"]["id"]
            running = info.get("running_elsewhere")
            if running and running["level"] == "strong" and sid not in allow:
                skipped = f"already running (pid {running.get('pid')}, {running.get('tty') or 'no tty'})"
            else:
                prov = cs.providers(rt, home)[info["cli"]]
                cmd = prov.resume_command(sid)
                if cmd is None:
                    skipped = "resume syntax no longer verifies"
        leaves.append({"cwd": cwd, "command": cmd})
        started.append({"pane": pane["id"], "cli": info["cli"], "session_id": (info["session"] or {}).get("id"),
                        "cwd": cwd, "command": cmd, "skipped": skipped})
    built = driver.build(plan["layout"], leaves, window_id=window_id, new_window=new_window,
                         ratio_tol=ratio_tol)
    rt.sleep(settle_s)
    checks = verify_panes(driver, built, started, rt=rt, home=home)
    return {"built": {k: v for k, v in built.items() if k != "terminals"},
            "terminals": built["terminals"], "panes": checks,
            "undo": {"skill": "ghostty.close", "args": {"tab_id": built["tab_id"],
                                                        "window_id": built["window_id"],
                                                        "opened": built.get("opened")},
                     "description": "close the tab/window that was opened"}}


def verify_panes(driver: Any, built: dict[str, Any], started: list[dict[str, Any]], *,
                 rt: Runtime, home: Path) -> list[dict[str, Any]]:
    """Read each pane back: command echoed, cwd correct, no error text, session process seen."""
    inv = {t.terminal_id: t for t in driver.inventory(only_windows=[built["window_id"]])}
    out: list[dict[str, Any]] = []
    for s in started:
        term_id = built["terminals"].get(str(s["pane"]), "")
        row: dict[str, Any] = {"pane": s["pane"], "terminal_id": term_id, "cli": s["cli"],
                               "session_id": s["session_id"], "expected_command": s["command"],
                               "skipped": s["skipped"]}
        t = inv.get(term_id)
        row["cwd_ok"] = bool(t) and _same(t.cwd, s["cwd"]) if t else False
        row["cwd"] = t.cwd if t else None
        try:
            text = driver.read_text(term_id) if term_id else ""
        except Exception as exc:
            text, row["read_error"] = "", str(exc)[:120]
        row["text_tail"] = [ln for ln in text.splitlines() if ln.strip()][-4:]
        if s["command"]:
            row["command_echoed"] = _echoed(s["command"], text)
            row["error_text"] = bool(ERROR_WORDS.search(text))
            seen = None
            if s["session_id"]:
                seen = cs.running_elsewhere(cs.CliSession(s["cli"], s["session_id"], s["cwd"]), rt, home)
            row["process_seen"] = bool(seen and seen["level"] == "strong")
            row["ok"] = bool(row["cwd_ok"] and row["command_echoed"] and not row["error_text"])
        else:
            row["ok"] = bool(row["cwd_ok"])
        out.append(row)
    return out


def _echoed(command: str, screen: str) -> bool:
    flat = re.sub(r"\s+", "", screen)
    return re.sub(r"\s+", "", command) in flat or shlex.split(command)[-1] in flat
