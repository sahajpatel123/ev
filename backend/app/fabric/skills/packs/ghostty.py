"""Ghostty driver: windows, tabs, native splits, input, read-back, closed-loop ratios.

Ghostty 1.3.x has a real AppleScript dictionary (window > tab > terminal, stable
ids, `working directory`, `new tab in window`, `split`, `input text`,
`perform action`). Pane geometry is read from the Accessibility tree
(System Events: one AXTextArea per terminal, with its frame in points), which
lets us set a split ratio closed-loop: `resize_split:<dir>,<points>` moves a
divider by exactly that many AX points.

Nothing here targets "the owner's" windows implicitly: every call takes the ids
it should touch, and `front_window()` is only used when the caller asks for it.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .. import layout_tree as lt
from ..runtime import Proc, Runtime, applescript_quote, default_runtime

SEP = "\x1f"
BUNDLE = "/Applications/Ghostty.app"


@dataclass
class Term:
    window_id: str
    tab_id: str
    tab_index: int
    selected: bool
    terminal_id: str
    cwd: str
    title: str

    def to_dict(self) -> dict[str, Any]:
        return {"app": "Ghostty", "window_id": self.window_id, "tab_id": self.tab_id,
                "tab_index": self.tab_index, "selected": self.selected,
                "terminal_id": self.terminal_id, "cwd": self.cwd, "title": self.title}


class GhosttyError(RuntimeError):
    def __init__(self, code: str, message: str, fix: str | None = None):
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def _classify(p: Proc, what: str) -> GhosttyError:
    if p.permission_denied:
        return GhosttyError("permission_missing", f"{what}: macOS blocked Apple Events to Ghostty",
                            "System Settings > Privacy & Security > Automation: allow this app to control Ghostty.")
    if p.app_not_running:
        return GhosttyError("not_running", "Ghostty is not running")
    if p.timed_out:
        return GhosttyError("timeout", f"{what}: osascript timed out")
    return GhosttyError("internal", f"{what}: {(p.err or p.out)[:200]}")


class GhosttyDriver:
    def __init__(self, rt: Runtime | None = None):
        self.rt = rt or default_runtime()

    # -- plumbing -----------------------------------------------------------
    def _as(self, script: str, what: str, timeout: float = 20.0) -> str:
        p = self.rt.osascript(script, timeout=timeout)
        if not p.ok:
            raise _classify(p, what)
        return p.out

    def running(self) -> bool:
        p = self.rt.run(["pgrep", "-x", "ghostty"], timeout=5)
        return bool((p.out or "").strip())

    def _cfg(self, cwd: str = "", command: str = "", initial_input: str = "",
             font_size: float | None = None) -> tuple[str, str]:
        sets = []
        if cwd:
            sets.append(f"set initial working directory of cfg to {applescript_quote(cwd)}")
        if command:
            sets.append(f"set command of cfg to {applescript_quote(command)}")
            sets.append("set wait after command of cfg to true")
        if initial_input:
            sets.append(f"set initial input of cfg to {applescript_quote(initial_input)}")
        if font_size:
            sets.append(f"set font size of cfg to {font_size}")
        if not sets:
            return "", "missing value"
        return "set cfg to new surface configuration\n    " + "\n    ".join(sets), "cfg"

    # -- inventory ----------------------------------------------------------
    _INVENTORY = (
        'tell application "Ghostty"\n'
        "  set sep to character id 31\n"
        '  set out to ""\n'
        "  repeat with w in windows\n"
        "    set wid to id of w\n"
        "    repeat with tb in tabs of w\n"
        "      repeat with t in terminals of tb\n"
        "        set out to out & wid & sep & (id of tb) & sep & (index of tb) & sep & "
        "((selected of tb) as text) & sep & (id of t) & sep & (working directory of t) & sep & "
        "(name of t) & linefeed\n"
        "      end repeat\n"
        "    end repeat\n"
        "  end repeat\n"
        "  return out\n"
        "end tell")

    def inventory(self, *, only_windows: list[str] | None = None) -> list[Term]:
        if not self.running():
            raise GhosttyError("not_running", "Ghostty is not running")
        out = self._as(self._INVENTORY, "inventory", timeout=30)
        terms: list[Term] = []
        for line in out.splitlines():
            parts = line.split(SEP)
            if len(parts) < 7:
                continue
            t = Term(parts[0], parts[1], int(parts[2] or 0), parts[3].strip().lower() == "true",
                     parts[4], parts[5], parts[6])
            if only_windows is None or t.window_id in only_windows:
                terms.append(t)
        return terms

    def window_ids(self) -> list[str]:
        out = self._as('tell application "Ghostty" to return id of every window', "window list")
        return [w.strip() for w in out.split(",") if w.strip()]

    def front_window(self) -> str | None:
        p = self.rt.osascript('tell application "Ghostty" to return id of front window')
        return p.out.strip() if p.ok and p.out.strip() else None

    # -- create -------------------------------------------------------------
    def new_window(self, *, cwd: str = "", initial_input: str = "") -> dict[str, str]:
        setup, expr = self._cfg(cwd=cwd, initial_input=initial_input)
        body = f"  {setup}\n  set w to new window with configuration {expr}\n" if setup else \
            "  set w to new window\n"
        out = self._as('tell application "Ghostty"\n' + body +
                       "  set tb to selected tab of w\n"
                       "  return (id of w) & character id 31 & (id of tb) & character id 31 & "
                       "(id of focused terminal of tb)\nend tell", "new window")
        wid, tab, term = (out.split(SEP) + ["", "", ""])[:3]
        return {"window_id": wid, "tab_id": tab, "terminal_id": term}

    def new_tab(self, window_id: str, *, cwd: str = "", initial_input: str = "") -> dict[str, str]:
        setup, expr = self._cfg(cwd=cwd, initial_input=initial_input)
        cfg_line = f"  {setup}\n" if setup else ""
        with_cfg = f" with configuration {expr}" if setup else ""
        out = self._as('tell application "Ghostty"\n' + cfg_line +
                       f"  set tb to new tab in window id {applescript_quote(window_id)}{with_cfg}\n"
                       "  return (id of tb) & character id 31 & (id of focused terminal of tb)\n"
                       "end tell", "new tab")
        tab, term = (out.split(SEP) + ["", ""])[:2]
        return {"window_id": window_id, "tab_id": tab, "terminal_id": term}

    def split(self, terminal_id: str, direction: str, *, cwd: str = "",
              initial_input: str = "") -> str:
        if direction not in ("right", "left", "down", "up"):
            raise GhosttyError("invalid_args", f"bad split direction {direction!r}")
        setup, expr = self._cfg(cwd=cwd, initial_input=initial_input)
        cfg_line = f"  {setup}\n" if setup else ""
        with_cfg = f" with configuration {expr}" if setup else ""
        return self._as('tell application "Ghostty"\n' + cfg_line +
                        f"  set nt to split terminal id {applescript_quote(terminal_id)} "
                        f"direction {direction}{with_cfg}\n  return id of nt\nend tell", "split")

    # -- act ------------------------------------------------------------------
    def input_text(self, terminal_id: str, text: str, *, enter: bool = True) -> None:
        payload = text + ("\n" if enter and not text.endswith("\n") else "")
        self._as('tell application "Ghostty" to input text '
                 f"{applescript_quote(payload)} to terminal id {applescript_quote(terminal_id)}",
                 "input text")

    def send_key(self, terminal_id: str, key: str, modifiers: str = "") -> None:
        mod = f" modifiers {applescript_quote(modifiers)}" if modifiers else ""
        self._as(f'tell application "Ghostty" to send key {applescript_quote(key)}{mod} '
                 f"to terminal id {applescript_quote(terminal_id)}", "send key")

    def perform(self, terminal_id: str, action: str) -> bool:
        out = self._as('tell application "Ghostty" to perform action '
                       f"{applescript_quote(action)} on terminal id {applescript_quote(terminal_id)}",
                       f"perform {action}")
        return out.strip().lower() == "true"

    def focus(self, terminal_id: str) -> None:
        self._as(f'tell application "Ghostty" to focus terminal id {applescript_quote(terminal_id)}',
                 "focus")

    def select_tab(self, window_id: str, tab_id: str) -> None:
        self._as('tell application "Ghostty"\n'
                 f"  select tab (first tab of window id {applescript_quote(window_id)} whose id is "
                 f"{applescript_quote(tab_id)})\nend tell", "select tab")

    def close_tab(self, window_id: str, tab_id: str) -> None:
        self._as('tell application "Ghostty"\n'
                 f"  close tab (first tab of window id {applescript_quote(window_id)} whose id is "
                 f"{applescript_quote(tab_id)})\nend tell", "close tab")

    def close_window(self, window_id: str) -> None:
        self._as(f'tell application "Ghostty" to close window id {applescript_quote(window_id)}',
                 "close window")

    def close_terminal(self, terminal_id: str) -> None:
        self._as(f'tell application "Ghostty" to close terminal id {applescript_quote(terminal_id)}',
                 "close terminal")

    # -- read-back ----------------------------------------------------------
    def read_text(self, terminal_id: str) -> str:
        """Visible screen text of one terminal (clipboard saved and restored)."""
        saved = self.rt.run(["pbpaste"], timeout=5)
        try:
            self.perform(terminal_id, "write_screen_file:copy,plain")
            self.rt.sleep(0.35)
            path = (self.rt.run(["pbpaste"], timeout=5).out or "").strip().splitlines()
            if not path:
                raise GhosttyError("internal", "screen dump path missing")
            got = self.rt.run(["cat", path[0]], timeout=5)
            if not got.ok:
                raise GhosttyError("internal", "screen dump unreadable")
            return got.out
        finally:
            with contextlib.suppress(Exception):
                self.rt.run(["pbcopy"], input=saved.out or "", timeout=5)

    # -- geometry (Accessibility) -----------------------------------------------
    _AX_PANES = r"""
function run(argv) {
  const want = argv[0] || "";
  const se = Application("System Events");
  const wins = se.processes["Ghostty"].windows();
  const out = [];
  for (let wi = 0; wi < wins.length; wi++) {
    const w = wins[wi];
    let title = ""; try { title = String(w.title()); } catch (e) {}
    if (want && title !== want) continue;
    const rec = {title: title, pos: w.position(), size: w.size(), panes: []};
    (function walk(el, depth) {
      if (depth > 9) return;
      let role = ""; try { role = String(el.role()); } catch (e) {}
      if (role === "AXTextArea") {
        let p = null, s = null, v = "";
        try { p = el.position(); s = el.size(); } catch (e) {}
        try { v = String(el.value()); } catch (e) {}
        let f = false; try { f = !!el.focused(); } catch (e) {}
        rec.panes.push({pos: p, size: s, focused: f, text: v.slice(0, 4000)});
        return;
      }
      let kids = []; try { kids = el.uiElements(); } catch (e) { return; }
      for (let i = 0; i < kids.length; i++) walk(kids[i], depth + 1);
    })(w, 0);
    out.push(rec);
  }
  return JSON.stringify(out);
}
"""

    def ax_windows(self, title: str = "") -> list[dict[str, Any]]:
        p = self.rt.osascript(self._AX_PANES, lang="javascript", timeout=30, argv=[title])
        if not p.ok:
            if p.permission_denied or "assistive" in (p.err or "").lower():
                raise GhosttyError("permission_missing", "Accessibility is not granted",
                                   "System Settings > Privacy & Security > Accessibility: allow this app.")
            raise _classify(p, "accessibility read")
        try:
            data = json.loads(p.out or "[]")
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    def measure(self, title: str = "") -> dict[str, Any] | None:
        """Pane rectangles of the (named) front tab, normalised to the content area."""
        wins = self.ax_windows(title)
        if not wins:
            return None
        win = wins[0]
        panes = [p for p in win["panes"] if p.get("size") and p.get("pos")]
        if not panes:
            return None
        x0 = min(p["pos"][0] for p in panes)
        y0 = min(p["pos"][1] for p in panes)
        x1 = max(p["pos"][0] + p["size"][0] for p in panes)
        y1 = max(p["pos"][1] + p["size"][1] for p in panes)
        cw, ch = max(1, x1 - x0), max(1, y1 - y0)
        rects = [{"px": [p["pos"][0], p["pos"][1], p["size"][0], p["size"][1]],
                  "rect": [(p["pos"][0] - x0) / cw, (p["pos"][1] - y0) / ch,
                           p["size"][0] / cw, p["size"][1] / ch],
                  "text": p.get("text", ""), "focused": bool(p.get("focused"))} for p in panes]
        return {"title": win["title"], "content_px": [x0, y0, cw, ch], "panes": rects}

    # -- build a layout ------------------------------------------------------------
    def build(self, tree: dict[str, Any], leaves: list[dict[str, Any]], *,
              window_id: str | None = None, new_window: bool = False,
              set_ratios: bool = True, ratio_tol: float = 0.02) -> dict[str, Any]:
        """Open a new tab (or window) and build `tree` with one terminal per leaf.

        `leaves[i]` is {cwd, command?} for the i-th leaf in tree order. Commands
        are typed after the ratios are final, so every pane shows its command
        at its real size. Returns ids, per-leaf terminal ids and the ratio audit.
        """
        order = lt.panes(tree)
        if len(order) != len(leaves):
            raise GhosttyError("invalid_args", f"{len(order)} panes in the layout but {len(leaves)} leaves")
        created: dict[str, Any] = {"terminals": {}}
        first = leaves[0]
        if new_window or not window_id:
            made = self.new_window(cwd=first.get("cwd", ""))
            created.update(window_id=made["window_id"], tab_id=made["tab_id"], opened="window")
        else:
            made = self.new_tab(window_id, cwd=first.get("cwd", ""))
            created.update(window_id=window_id, tab_id=made["tab_id"], opened="tab")
        leaf_term: dict[int, str] = {order[0]["id"]: made["terminal_id"]}
        split_terms: list[tuple[dict[str, Any], str, str]] = []  # node, a_terminal, b_terminal

        def first_leaf(n: dict[str, Any]) -> dict[str, Any]:
            return n if n["kind"] == "pane" else first_leaf(n["a"])

        def grow(node: dict[str, Any], term: str) -> None:
            if node["kind"] == "pane":
                leaf_term[node["id"]] = term
                return
            b_first = first_leaf(node["b"])
            idx = [p["id"] for p in order].index(b_first["id"])
            new_term = self.split(term, node["dir"], cwd=leaves[idx].get("cwd", ""))
            split_terms.append((node, term, new_term))
            grow(node["a"], term)
            grow(node["b"], new_term)

        grow(tree, made["terminal_id"])
        created["terminals"] = {str(k): v for k, v in leaf_term.items()}
        created["audit"] = self._set_ratios(tree, leaf_term, created, ratio_tol) if set_ratios else None
        for p in order:
            cmd = leaves[[q["id"] for q in order].index(p["id"])].get("command") or ""
            if cmd:
                self.input_text(leaf_term[p["id"]], cmd)
        return created

    def _set_ratios(self, tree: dict[str, Any], leaf_term: dict[int, str],
                    created: dict[str, Any], tol: float) -> dict[str, Any]:
        """Closed-loop: measure pane frames, move each divider, re-measure."""
        audit: dict[str, Any] = {"method": "ax", "rounds": 0, "max_err": None, "ok": False}
        title = ""
        want = [n for n in _splits(tree) if abs(n["ratio"] - 0.5) > 0.004 or True]
        for rnd in range(3):
            try:
                self.focus(next(iter(leaf_term.values())))
            except GhosttyError:
                pass
            self.rt.sleep(0.25)
            try:
                m = self.measure(title)
            except GhosttyError as exc:
                audit.update(method="unavailable", error=exc.message, fix=exc.fix)
                return audit
            if not m:
                audit.update(method="unavailable", error="no Ghostty window visible to Accessibility")
                return audit
            audit["rounds"] = rnd + 1
            errs = []
            for node in want:
                err = self._adjust(node, m, leaf_term, tree)
                errs.append(abs(err))
            self.rt.sleep(0.25)
            m2 = self.measure(title)
            if m2:
                measured = _tree_from_rects(m2["panes"])
                audit["measured_panes"] = len(m2["panes"])
                audit["partition_ok"] = lt.same_partition(tree, measured, tol=tol + 0.02) \
                    if measured is not None else False
                audit["max_err"] = _partition_error(tree, m2["panes"])
                if audit["max_err"] is not None and audit["max_err"] <= tol:
                    audit["ok"] = True
                    break
        return audit

    def _adjust(self, node: dict[str, Any], m: dict[str, Any], leaf_term: dict[int, str],
                root: dict[str, Any]) -> float:
        """Move one divider toward its target; returns the remaining error (fraction)."""
        a_rects = _subtree_rects(node["a"], m, root)
        b_rects = _subtree_rects(node["b"], m, root)
        if not a_rects or not b_rects:
            return 0.0
        horizontal = node["dir"] == "right"
        idx = 0 if horizontal else 1
        sz = 2 if horizontal else 3
        a_start = min(r["px"][idx] for r in a_rects)
        a_end = max(r["px"][idx] + r["px"][sz] for r in a_rects)
        b_start = min(r["px"][idx] for r in b_rects)
        b_end = max(r["px"][idx] + r["px"][sz] for r in b_rects)
        gap = b_start - a_end
        total = (b_end - a_start) - gap
        want_a = node["ratio"] * total
        have_a = a_end - a_start
        delta = round(want_a - have_a)
        if abs(delta) < 2:
            return (have_a - want_a) / max(1, total)
        if delta > 0:
            leaf = _edge_leaf(node["a"], last=True)
            direction = "right" if horizontal else "down"
        else:
            leaf = _edge_leaf(node["b"], last=False)
            direction = "left" if horizontal else "up"
        with contextlib.suppress(GhosttyError):
            self.perform(leaf_term[leaf["id"]], f"resize_split:{direction},{abs(delta)}")
        return delta / max(1, total)


# ------------------------------------------------------------ geometry helpers

def _splits(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Splits outermost first (pre-order), so inner regions are fixed after outer ones."""
    if node["kind"] == "pane":
        return []
    return [node, *_splits(node["a"]), *_splits(node["b"])]


def _edge_leaf(node: dict[str, Any], *, last: bool) -> dict[str, Any]:
    """Last (right/bottom-most) or first leaf of a subtree, in tree order."""
    while node["kind"] != "pane":
        node = node["b"] if last else node["a"]
    return node


def _leaf_ids(node: dict[str, Any]) -> list[int]:
    return [p["id"] for p in lt.panes(node)]


def _subtree_rects(sub: dict[str, Any], m: dict[str, Any], root: dict[str, Any]) -> list[dict[str, Any]]:
    """Measured panes belonging to a subtree, matched by position to the planned partition."""
    plan = _planned_rects(root)
    mine = set(_leaf_ids(sub))
    out = []
    used: set[int] = set()
    for leaf_id, prect in plan.items():
        cx, cy = prect[0] + prect[2] / 2, prect[1] + prect[3] / 2
        best, best_d = None, 9.0
        for i, r in enumerate(m["panes"]):
            if i in used:
                continue
            rx, ry = r["rect"][0] + r["rect"][2] / 2, r["rect"][1] + r["rect"][3] / 2
            d = abs(rx - cx) + abs(ry - cy)
            if d < best_d:
                best, best_d = i, d
        if best is not None:
            used.add(best)
            if leaf_id in mine:
                out.append(m["panes"][best])
    return out


def _planned_rects(root: dict[str, Any]) -> dict[int, tuple[float, float, float, float]]:
    leaves = lt.panes(root)
    return {p["id"]: r for p, r in zip(leaves, lt.partition(root), strict=True)}


def _partition_error(tree: dict[str, Any], measured: list[dict[str, Any]]) -> float | None:
    plan = lt.partition(tree)
    if len(plan) != len(measured):
        return None
    worst = 0.0
    used: set[int] = set()
    for p in plan:
        best, best_e = None, 9.0
        for i, r in enumerate(measured):
            if i in used:
                continue
            e = max(abs(p[k] - r["rect"][k]) for k in range(4))
            if e < best_e:
                best, best_e = i, e
        if best is None:
            return None
        used.add(best)
        worst = max(worst, best_e)
    return round(worst, 4)


def _tree_from_rects(panes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Rebuild a guillotine tree from measured normalised rects (None if not guillotine)."""
    rects = [tuple(p["rect"]) for p in panes]
    return _cut(rects, (0.0, 0.0, 1.0, 1.0))


def _cut(rects: list[tuple[float, float, float, float]], box: tuple[float, float, float, float]
         ) -> dict[str, Any] | None:
    if len(rects) == 1:
        return {"kind": "pane", "id": 0}
    eps = 0.012
    x, y, w, h = box
    for axis in (0, 1):
        pos_i, size_i = (0, 2) if axis == 0 else (1, 3)
        cuts = sorted({round(r[pos_i] + r[size_i], 4) for r in rects})
        for c in cuts[:-1]:
            left = [r for r in rects if r[pos_i] + r[size_i] <= c + eps]
            right = [r for r in rects if r[pos_i] >= c - eps]
            if left and right and len(left) + len(right) == len(rects):
                lo = min(r[pos_i] for r in left)
                hi = max(r[pos_i] + r[size_i] for r in right)
                mid_a = max(r[pos_i] + r[size_i] for r in left)
                mid_b = min(r[pos_i] for r in right)
                span = hi - lo
                if span <= 0:
                    continue
                ra = (mid_a - lo) / (span - (mid_b - mid_a))
                if axis == 0:
                    a = _cut(left, (lo, y, mid_a - lo, h))
                    b = _cut(right, (mid_b, y, hi - mid_b, h))
                else:
                    a = _cut(left, (x, lo, w, mid_a - lo))
                    b = _cut(right, (x, mid_b, w, hi - mid_b))
                if a is None or b is None:
                    continue
                return {"kind": "split", "dir": "right" if axis == 0 else "down",
                        "ratio": round(ra, 4), "a": a, "b": b}
    return None


def probe(rt: Runtime | None = None) -> dict[str, Any]:
    """Prompt-free facts about Ghostty on this machine."""
    rt = rt or default_runtime()
    d = GhosttyDriver(rt)
    info: dict[str, Any] = {"installed": False, "running": False, "scriptable": False}
    info["installed"] = bool(rt.run(["test", "-d", BUNDLE]).ok)
    if not info["installed"]:
        return info
    v = rt.run([BUNDLE + "/Contents/MacOS/ghostty", "--version"], timeout=5)
    m = re.search(r"(\d+\.\d+\.\d+)", v.out or "")
    info["version"] = m.group(1) if m else (v.out or "").strip()[:40]
    info["running"] = d.running()
    if info["running"]:
        p = rt.osascript('tell application "Ghostty" to return (count of windows)', timeout=10)
        info["scriptable"] = p.ok
        if not p.ok:
            info["error"] = (p.err or "")[:160]
    return info
