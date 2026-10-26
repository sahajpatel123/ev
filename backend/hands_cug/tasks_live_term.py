"""CUG tasks that run LIVE against real Ghostty windows we create ourselves (T1 end to end)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from app.fabric.skills import layout_tree as lt
from app.fabric.skills import synth
from app.fabric.skills.packs.skills_terminal import _gerr  # noqa: F401
from app.fabric.skills.testing import make_fake_home

from .core import Verdict, task
from .live import LiveEnv
from .tasks_live import approve

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "hands" / "screens"
EVV = Path("/Users/sahajpatel/Code/ev/helpers/evvision/.build/release/evvision")


def _mine(env: LiveEnv, cwd: str = "/private/tmp") -> dict[str, str]:
    made = env.ghostty.new_window(cwd=cwd)
    env.created_ghostty.append(made["window_id"])
    env.ghostty.set_frame(made["window_id"], (560, 300, 1000, 680))
    time.sleep(0.5)
    return made


def _inv(env: LiveEnv, win: str):
    return env.ghostty.inventory(only_windows=[win])


@task("live.gh.windows_list", "ghostty_live", "live_term", ["list ghostty windows", "what terminals are open", "list my terminal tabs", "which terminals do i have open"], "ghostty.windows_list")
def gh_list(env):
    m = _mine(env)
    r = env.hands.skill_run("ghostty.windows_list")
    ids = {t["terminal_id"] for t in r["evidence"]["terminals"]}
    return Verdict(r, r["ok"] and m["terminal_id"] in ids, "")


@task("live.gh.new_tab_with_cwd_and_undo", "ghostty_live", "live_term", ["open a new ghostty tab", "new terminal tab", "open a terminal tab in tmp", "new tab in ghostty"], "ghostty.new_tab")
def gh_tab(env):
    m = _mine(env)
    r = env.hands.skill_run("ghostty.new_tab", {"window_id": m["window_id"], "cwd": "/private/tmp"})
    n_after = len({t.tab_id for t in _inv(env, m["window_id"])})
    undo = env.hands.skill_run(r["undo"]["skill"], r["undo"]["args"])
    n_back = len({t.tab_id for t in _inv(env, m["window_id"])})
    return Verdict(r, r["ok"] and r["verified"] and n_after == 2 and undo["ok"] and n_back == 1, "opened, verified, closed by its own undo")


@task("live.gh.split_pane", "ghostty_live", "live_term", ["split the terminal", "split this pane", "split ghostty right", "split terminal down"], "ghostty.split")
def gh_split(env):
    m = _mine(env)
    r = env.hands.skill_run("ghostty.split", {"terminal_id": m["terminal_id"], "direction": "right"})
    return Verdict(r, r["ok"] and r["verified"] and len(_inv(env, m["window_id"])) == 2, "")


@task("live.gh.send_input_runs_the_command", "ghostty_live", "live_term", ["run this in the terminal", "type into the terminal", "send input to the pane", "type in ghostty"], "ghostty.send_input")
def gh_send(env):
    m = _mine(env)
    marker = f"HANDS_OK_{os.getpid()}"
    held = env.hands.skill_run("ghostty.send_input", {"terminal_id": m["terminal_id"], "text": f"echo {marker}"})
    r = approve(env.hands, "ghostty.send_input", {"terminal_id": m["terminal_id"], "text": f"echo {marker}"})
    time.sleep(0.8)
    screen = env.ghostty.read_text(m["terminal_id"])
    return Verdict(r, held["needs_confirm"] and r["ok"] and screen.count(marker) >= 2,
                   "typed AND executed (the marker appears as the echoed command and as its output)")


@task("live.gh.read_pane_untrusted", "ghostty_live", "live_term", ["read the terminal", "what does the pane say", "read terminal output", "what's in that terminal"], "ghostty.read_pane")
def gh_read(env):
    m = _mine(env)
    r = env.hands.skill_run("ghostty.read_pane", {"terminal_id": m["terminal_id"]})
    return Verdict(r, r["ok"] and r["evidence"]["untrusted"] is True and "%" in r["evidence"]["text"], "")


@task("live.gh.close_foreign_asks_and_does_nothing", "ghostty_live", "live_term", ["close the terminal tab", "close that pane", "close ghostty window", "close this terminal"], "ghostty.close", difficulty="hard")
def gh_close_foreign(env):
    m = _mine(env)
    foreign = [t for t in env.ghostty.inventory() if t.window_id not in env.created_ghostty]
    if not foreign:
        return Verdict(None, True, "no foreign window to test against (nothing to protect)")
    f = foreign[0]
    r = env.hands.skill_run("ghostty.close", {"window_id": f.window_id, "tab_id": f.tab_id})
    return Verdict(r, r["needs_confirm"] and not r["ok"] and not env.scope_violation(),
                   "closing something we did not create asks first; the owner's tab is untouched")


def _tree_of(env: LiveEnv, win: str, n: int):
    from app.fabric.skills.packs.ghostty import _tree_from_rects

    title = env.ghostty.window_title(win)
    m = env.ghostty.measure(title, expect_panes=n)
    return _tree_from_rects(m["panes"]) if m else None


@task("live.gh.layout_five_panes_ratios_verified", "ghostty_live", "live_term", ["open a tab with this layout", "build this terminal layout", "new tab with splits", "make a tab with those panes"], "ghostty.new_tab_with_layout", difficulty="hard")
def gh_layout(env):
    m = _mine(env)
    tree = synth.true_tree(synth.LAYOUTS["five_panes"])
    panes = [{"cwd": "/private/tmp", "command": f"echo PANE_{i}_UP"} for i in range(5)]
    args = {"layout": tree, "panes": panes, "window_id": m["window_id"]}
    r = approve(env.hands, "ghostty.new_tab_with_layout", args)
    got = _tree_of(env, m["window_id"], 5)
    ok = r["ok"] and r["verified"] and got is not None and lt.same_partition(got, tree, tol=0.02)
    return Verdict(r, bool(ok), f"read back from the live window; split-size audit {r['evidence']['built'].get('audit')}")


@task("live.gh.layout_save_and_restore", "ghostty_live", "live_term", ["save this terminal layout", "restore my terminal layout", "bring back my layout", "restore ghostty layout"], "ghostty.layout_restore", difficulty="hard")
def gh_save_restore(env):
    m = _mine(env)
    tree = synth.true_tree(synth.LAYOUTS["left_plus_two"])
    panes = [{"cwd": "/private/tmp", "command": ""}, {"cwd": "/private/tmp", "command": ""}, {"cwd": "/private/tmp", "command": ""}]
    built = approve(env.hands, "ghostty.new_tab_with_layout", {"layout": tree, "panes": panes, "window_id": m["window_id"]})
    saved = env.hands.skill_run("ghostty.layout_save", {"name": "cug-left-plus-two", "window_id": m["window_id"]})
    restored = approve(env.hands, "ghostty.layout_restore", {"name": "cug-left-plus-two", "window_id": m["window_id"]})
    got = _tree_of(env, m["window_id"], 3)
    ok = built["ok"] and saved["ok"] and restored["ok"] and got is not None and lt.same_partition(got, tree, tol=0.03)
    return Verdict(restored, bool(ok), "saved a live layout and rebuilt it; partition within 3%")


# --------------------------------------------------- T1 end to end on real screenshots ----

T1_SAY = ["open a new tab with the same layout and resume those coding sessions",
          "look at my last screenshot and resume those sessions",
          "rebuild this terminal layout from the screenshot and resume the sessions",
          "open a new tab like my screenshot n resume them"]


def _t1_live(name: str, *, ocr_ok: bool = True):
    @task(f"live.t1.apply_{name}", "terminal_t1", "live_term", T1_SAY, "coding_sessions.restore_from_screenshot", difficulty="hard")
    def run(env: LiveEnv) -> Verdict:
        if not EVV.exists():
            return Verdict(None, False, "the Apple Vision helper is not built")
        meta_p = CORPUS / "real" / f"{name}.json"
        meta = json.loads(meta_p.read_text())
        work = Path(tempfile.mkdtemp(prefix="cug-t1-"))
        truth_home = make_fake_home(work / "home", seed=meta["seed"], per_cli=12, work_root=work / "w")
        shims = work / "shims"
        shims.mkdir()
        for cli in ("claude", "codex", "muse", "opencode"):
            f = shims / cli
            f.write_text('#!/bin/sh\necho "FAKE-CLI $0 $*"\nsleep 900\n')
            f.chmod(0o755)
        from app.config import settings

        settings.hands_ocr_binary = str(EVV)  # the product's own OCR path: fast first, accurate if needed
        m = _mine(env)
        h = env.make_hands(home=work / "home", extra={"last_image": str(meta_p.with_suffix(".png"))})
        # real CLIs are only asked for --help (resume syntax); the commands that run are shims (prelude)
        args = {"window_id": m["window_id"], "prelude": f"export PATH={shims}:$PATH"}
        r = approve(h, "coding_sessions.restore_from_screenshot", args)
        from collections import Counter

        shown = Counter(t["session_id"] for t in meta["truth"] if t["session_id"])
        # intended outcome: one session is never resumed in two panes, so duplicates are flagged, not run
        want = [t for t in meta["truth"] if t["expect"] == "execute" and shown[t["session_id"]] == 1]
        panes = r.get("evidence", {}).get("result") or []
        ran = [p for p in panes if p.get("expected_command")]
        ok_ids = sorted(p["session_id"] for p in ran if p["ok"]) == sorted(t["session_id"] for t in want if t["session_id"])
        return Verdict(r, bool(r["ok"] and r["verified"] and ok_ids and len(ran) == len(want)),
                       f"{len(ran)}/{len(want)} sessions resumed, each read back (cwd, echoed, process started with the id)",
                       {"audit": (r.get("evidence") or {}).get("audit")})

    return run


for _n in ("s02_two_cols_dark", "s07_left_plus_two_dark", "s09_grid_2x2_light", "s14_four_cols_dark_wrap"):
    _t1_live(_n)

_ = (Any,)
