"""Real Ghostty screenshot corpus (T1): build real layouts in OUR OWN window, print resume
banners with ids from a deterministic fake session store, capture the window, record truth.

Run under the GUI lock:  python -m hands_cug.screens <out_dir> [name ...]
Nothing here touches the owner's windows: every window is created, captured and closed here.
"""

from __future__ import annotations

import contextlib
import json
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from app.fabric.skills import layout_tree as lt
from app.fabric.skills import synth
from app.fabric.skills.packs.ghostty import GhosttyDriver, GhosttyError
from app.fabric.skills.runtime import SystemRuntime
from app.fabric.skills.testing import make_fake_home

SEED = 11
THEMES = {  # OSC 10 (foreground) / OSC 11 (background)
    "dark": ("#c0c5ce", "#1f2329"), "light": ("#24292f", "#fafafa"), "solarized": ("#93a1a1", "#002b36"),
    "paper": ("#586e75", "#fdf6e3"), "black": ("#d0d0d0", "#000000"),
}
BANNER = {
    "claude": ("Resume this conversation with:", "claude --resume {id}"),
    "codex": ("To continue this session, run", "codex resume {id}"),
    "opencode": ("Continue", "opencode -s {id}"),
    "muse": ("Resume with:", "muse resume {id}"),
}
CLIS = ["claude", "codex", "opencode", "muse"]

# name, layout, theme, font, frame(w,h), special
SPECS: list[tuple[str, str, str, float, tuple[int, int], str]] = [
    ("s01_single_dark", "single", "dark", 13, (900, 600), ""),
    ("s02_two_cols_dark", "two_cols", "dark", 13, (1000, 620), ""),
    ("s03_two_rows_dark", "two_rows", "dark", 13, (900, 700), ""),
    ("s04_cols_60_40_dark", "cols_60_40", "dark", 13, (1000, 620), ""),
    ("s05_cols_30_70_dark", "cols_30_70", "dark", 13, (1000, 620), ""),
    ("s06_three_cols_dark", "three_cols", "dark", 12, (1100, 620), ""),
    ("s07_left_plus_two_dark", "left_plus_two", "dark", 13, (1000, 700), ""),
    ("s08_two_plus_right_light", "two_plus_right", "light", 13, (1000, 700), ""),
    ("s09_grid_2x2_light", "grid_2x2", "light", 13, (1000, 700), ""),
    ("s10_top_plus_three_dark_big", "top_plus_three", "dark", 16, (1100, 760), ""),
    ("s11_five_panes_dark_small", "five_panes", "dark", 11, (1060, 740), ""),
    ("s12_six_grid_dark_tiny", "six_panes_grid", "dark", 10, (1100, 740), ""),
    ("s13_uneven_nested_dark", "uneven_nested", "dark", 13, (1000, 740), ""),
    ("s14_four_cols_dark_wrap", "four_cols", "dark", 12, (1100, 600), ""),
    ("s15_grid_2x2_solarized", "grid_2x2", "solarized", 13, (1000, 700), ""),
    ("s16_left_plus_two_paper", "left_plus_two", "paper", 14, (1000, 700), ""),
    ("s17_five_panes_black", "five_panes", "black", 12, (1060, 740), ""),
    ("s18_six_grid_light", "six_panes_grid", "light", 12, (1100, 740), ""),
    ("s19_two_cols_garbled_id", "two_cols", "dark", 13, (1000, 620), "garble:2"),
    ("s20_grid_truncated_id", "grid_2x2", "dark", 13, (1000, 700), "truncate:3"),
    ("s21_left_plus_two_unreadable_id", "left_plus_two", "dark", 13, (1000, 700), "dots:2"),
    ("s22_three_cols_shell_pane", "three_cols", "dark", 13, (1100, 620), "shell:2"),
]


# Held-out set: written AFTER the detector and grounding were tuned on SPECS, different seed,
# layouts/themes/fonts combined differently. Evaluated with no code changes between capture and score.
HELDOUT_SEED = 23
HELDOUT: list[tuple[str, str, str, float, tuple[int, int], str]] = [
    ("h01_three_cols_light_big", "three_cols", "light", 15, (1100, 640), ""),
    ("h02_grid_2x2_dark_tiny", "grid_2x2", "dark", 9, (1000, 700), ""),
    ("h03_left_plus_two_solarized", "left_plus_two", "solarized", 12, (1000, 720), ""),
    ("h04_two_plus_right_black", "two_plus_right", "black", 14, (1000, 700), ""),
    ("h05_five_panes_paper", "five_panes", "paper", 13, (1100, 760), ""),
    ("h06_four_cols_light_wrap", "four_cols", "light", 11, (1080, 600), ""),
    ("h07_uneven_nested_light", "uneven_nested", "light", 13, (1000, 740), ""),
    ("h08_top_plus_three_solarized", "top_plus_three", "solarized", 12, (1100, 740), ""),
    ("h09_six_grid_black", "six_panes_grid", "black", 11, (1100, 740), ""),
    ("h10_cols_60_40_paper_garbled", "cols_60_40", "paper", 16, (1000, 640), "garble:1"),
    ("h11_two_rows_dark_dots", "two_rows", "dark", 13, (900, 700), "dots:2"),
    ("h12_left_plus_two_light_shell", "left_plus_two", "light", 13, (1000, 700), "shell:3"),
]


def short(p: str) -> str:
    h = str(Path.home())
    return "~" + p[len(h):] if p.startswith(h) else p


def pane_text(i: int, cli: str, sid: str, cwd: str, theme: str, token: str, special: str) -> str:
    fg, bg = THEMES[theme]
    head = f"\033]11;{bg}\007\033]10;{fg}\007\033]2;{token}\007\033[2J\033[H"
    kind, _, arg = special.partition(":")
    shown = sid
    if kind == "garble" and int(arg) == i:
        shown = sid[:14] + "??" + sid[16:24] + "~~~~" + sid[28:]       # not repairable by confusables
    elif kind == "truncate" and int(arg) == i:
        shown = sid[:13] + "…"
    elif kind == "dots" and int(arg) == i:
        shown = sid[:9] + "·" * 18 + sid[27:]
    if kind == "shell" and int(arg) == i:
        return head + f"user@mac {short(cwd)} % ls\nREADME.md  src  tests\nuser@mac {short(cwd)} % "
    lead, cmd = BANNER[cli]
    return (head + f"user@mac {short(cwd)} % {cli}\n\n  ...session ended...\n\n{lead}\n  "
            + cmd.format(id=shown) + f"\n\nuser@mac {short(cwd)} % ")


def capture_one(spec: tuple[str, str, str, float, tuple[int, int], str], out_dir: Path, truth_home: dict[str, Any],
                rt: SystemRuntime, g: GhosttyDriver, seed: int = SEED) -> dict[str, Any]:
    name, layout, theme, font, (w, h), special = spec
    tree = synth.true_tree(synth.LAYOUTS[layout])
    n = lt.count_panes(tree)
    rng = random.Random(f"{seed}-{name}")
    token = f"HANDS-CAP-{name}"
    work = Path(tempfile.mkdtemp(prefix="hands-shot-"))
    leaves, panes_truth = [], []
    for i in range(1, n + 1):
        cli = CLIS[(i + rng.randint(0, 3)) % 4]
        row = rng.choice(truth_home[cli][:-2])  # avoid the deliberately confusable pair
        text = pane_text(i, cli, row["id"], row["cwd"], theme, token, special)
        f = work / f"pane{i}.txt"
        f.write_text(text)
        leaves.append({"cwd": "/private/tmp", "launch": f"/bin/sh -c 'cat {f}; exec sleep 600'", "font_size": font})
        kind, _, arg = special.partition(":")
        expect = "execute"
        if kind in ("garble", "dots") and int(arg) == i:
            expect = "flag"
        if kind == "shell" and int(arg) == i:
            expect = "none"
            cli = None
        panes_truth.append({"pane": i, "cli": cli, "session_id": row["id"] if cli else None,
                            "cwd": row["cwd"] if cli else None, "expect": expect})
    built = g.build(tree, leaves, new_window=True, frame=(560, 280, w, h), ratio_tol=0.01)
    win = built["window_id"]
    try:
        time.sleep(1.4)
        wins = rt.run([str(Path.home() / ".ev/hands/bin/evhands"), "cgwindows", "--owner", "Ghostty"], timeout=15)
        rows = json.loads(wins.out).get("windows", [])
        mine = [r for r in rows if token in r["title"]]
        if not mine:
            raise RuntimeError(f"capture window for {name} not found")
        out = out_dir / f"{name}.png"
        p = rt.run(["screencapture", "-x", "-o", "-l", str(mine[0]["number"]), str(out)], timeout=20)
        if not p.ok or not out.exists():
            raise RuntimeError("screencapture failed")
        audit = built.get("audit") or {}
        info = {"name": name, "layout": layout, "spec": _spec_of(layout), "panes": n, "theme": theme, "font": font,
                "frame": [w, h], "special": special, "truth": panes_truth, "seed": seed,
                "real_capture": True, "ratio_audit": {"ok": audit.get("ok"), "max_err": audit.get("max_err")}}
        (out_dir / f"{name}.json").write_text(json.dumps(info, indent=1))
        return info
    finally:
        with contextlib.suppress(GhosttyError):
            g.close_window(win)
        shutil.rmtree(work, ignore_errors=True)


def _spec_of(layout: str) -> Any:
    def tup(x):
        return x if isinstance(x, str) else [x[0], x[1], tup(x[2]), tup(x[3])]
    return tup(synth.LAYOUTS[layout])


def main(argv: list[str]) -> int:
    heldout = "--heldout" in argv
    argv = [a for a in argv if a != "--heldout"]
    out_dir = Path(argv[0])
    out_dir.mkdir(parents=True, exist_ok=True)
    want = set(argv[1:])
    seed, specs = (HELDOUT_SEED, HELDOUT) if heldout else (SEED, SPECS)
    rt = SystemRuntime(route="direct")
    g = GhosttyDriver(rt)
    tmp = Path(tempfile.mkdtemp(prefix="hands-home-"))
    truth = make_fake_home(tmp, seed=seed, per_cli=12)
    done = 0
    for spec in specs:
        if want and spec[0] not in want:
            continue
        t0 = time.time()
        try:
            info = capture_one(spec, out_dir, truth, rt, g, seed)
            print(f"{spec[0]:34s} ok panes={info['panes']} ratio_ok={info['ratio_audit']['ok']} {time.time() - t0:4.1f}s", flush=True)
            done += 1
        except Exception as exc:  # keep going; report what failed
            print(f"{spec[0]:34s} FAILED {type(exc).__name__}: {exc}", flush=True)
        time.sleep(0.5)
    print("captured", done)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
