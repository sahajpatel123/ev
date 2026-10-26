"""T1 evaluation over the real screenshot corpus: layout, ratios, grounding, never-a-wrong-id."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from app.fabric.skills import layout_tree as lt
from app.fabric.skills import synth
from app.fabric.skills.imaging import load_raster
from app.fabric.skills.packs import terminal_restore as tr
from app.fabric.skills.packs.terminal_restore import evvision_ocr
from app.fabric.skills.runtime import Proc, SystemRuntime
from app.fabric.skills.testing import fake_cli_runtime, make_fake_home

from .stats import wilson

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "hands" / "screens" / "real"
EVV = Path("/Users/sahajpatel/Code/ev/helpers/evvision/.build/release/evvision")


def _tup(spec: Any) -> Any:
    return spec if isinstance(spec, str) else (spec[0], spec[1], _tup(spec[2]), _tup(spec[3]))


def ratio_error(got: dict[str, Any], truth: dict[str, Any]) -> float | None:
    a, b = lt.partition(got), lt.partition(truth)
    if len(a) != len(b):
        return None
    worst, used = 0.0, set()
    for p in a:
        best, be = None, 9.0
        for i, q in enumerate(b):
            if i in used:
                continue
            e = max(abs(p[k] - q[k]) for k in range(4))
            if e < be:
                best, be = i, e
        used.add(best)
        worst = max(worst, be)
    return worst


def evaluate(level: str = "fast", names: list[str] | None = None, *, tiered: bool = False,
             corpus: Path | None = None, seed: int = 11) -> dict[str, Any]:
    home = Path(tempfile.mkdtemp(prefix="hands-eval-home-"))
    truth_home = make_fake_home(home, seed=seed, per_cli=12, work_root=home / "work")
    rt = fake_cli_runtime(home)
    rt.when(r"^ps ", Proc(True, out=""), kind="run")
    real = SystemRuntime(route="direct")

    def ocr_for(lvl: str):
        def fn(path: Path) -> dict[str, Any]:
            p = real.run([str(EVV), "ocr", str(path), "--level", lvl], timeout=300)
            try:
                d = json.loads(p.out)
            except ValueError:
                return {"ok": False, "lines": [], "error": p.err[:100]}
            return {"ok": True, "lines": d.get("lines", [])}
        return fn

    rows = []
    for meta_path in sorted((corpus or CORPUS).glob("*.json")):
        meta = json.loads(meta_path.read_text())
        if names and meta["name"] not in names:
            continue
        png = meta_path.with_suffix(".png")
        t0 = time.time()
        plan = tr.analyze(png, rt=rt, home=home, ocr_fn=ocr_for(level),
                          ocr_accurate_fn=ocr_for("accurate") if tiered else None)
        dt = time.time() - t0
        truth_tree = synth.true_tree(_tup(meta["spec"]))
        n_ok = plan["pane_count"] == meta["panes"]
        part_ok = n_ok and lt.same_partition(plan["layout"], truth_tree, tol=0.08)
        err = ratio_error(plan["layout"], truth_tree) if n_ok else None
        pane_rows = []
        wrong = 0
        # Intended outcome (safety rule, decided before the run): one session is never resumed in
        # two panes of a layout, so panes that show the same session must be flagged, not executed.
        from collections import Counter

        shown = Counter(t["session_id"] for t in meta["truth"] if t["session_id"])
        for tp in meta["truth"]:
            if tp["session_id"] and shown[tp["session_id"]] > 1 and tp["expect"] == "execute":
                tp["expect"] = "flag"
        for tp, pp in zip(meta["truth"], plan["panes"], strict=False):
            exp_id = tp["session_id"]
            got_id = (pp["session"] or {}).get("id")
            if tp["expect"] == "execute":
                ok = pp["executable"] and got_id == exp_id
            elif tp["expect"] == "flag":
                ok = not pp["executable"]
            else:
                ok = not pp["executable"] and pp["cli"] is None
            if pp["executable"] and got_id != exp_id and tp["expect"] != "none":
                wrong += 1
            if pp["executable"] and tp["expect"] == "none":
                wrong += 1
            pane_rows.append({"pane": tp["pane"], "expect": tp["expect"], "ok": ok, "status": pp["id_status"],
                              "executed": pp["executable"], "wrong": pp["executable"] and got_id != exp_id})
        rows.append({"name": meta["name"], "panes": meta["panes"], "layout_ok": part_ok, "ratio_err": err,
                     "panes_ok": all(r["ok"] for r in pane_rows), "wrong_ids_executed": wrong,
                     "pane_rows": pane_rows, "seconds": round(dt, 1), "theme": meta["theme"], "font": meta["font"]})
    n = len(rows)
    layout_ok = sum(r["layout_ok"] for r in rows)
    ratios = [r["ratio_err"] for r in rows if r["ratio_err"] is not None]
    pane_total = sum(len(r["pane_rows"]) for r in rows)
    pane_ok = sum(1 for r in rows for p in r["pane_rows"] if p["ok"])
    return {"level": level, "tiered": tiered, "n_screens": n, "layout_ok": layout_ok,
            "layout_wilson": wilson(layout_ok, n), "ratio_err_max": max(ratios) if ratios else None,
            "ratio_within_8pct": sum(1 for e in ratios if e <= 0.08), "ratio_n": len(ratios),
            "panes": pane_total, "panes_ok": pane_ok, "panes_wilson": wilson(pane_ok, pane_total),
            "wrong_ids_executed": sum(r["wrong_ids_executed"] for r in rows), "rows": rows}


if __name__ == "__main__":
    level = sys.argv[1] if len(sys.argv) > 1 else "fast"
    held = "--heldout" in sys.argv
    res = evaluate(level, tiered="--tiered" in sys.argv,
                   corpus=CORPUS.parent / "heldout" if held else None, seed=23 if held else 11)
    for r in res["rows"]:
        bad = [p for p in r["pane_rows"] if not p["ok"]]
        print(f"{r['name']:34s} layout={'OK ' if r['layout_ok'] else 'BAD'} err={r['ratio_err'] if r['ratio_err'] is None else round(r['ratio_err'], 3)} "
              f"panes={'OK ' if r['panes_ok'] else 'BAD'} wrong={r['wrong_ids_executed']} {r['seconds']}s {bad if bad else ''}")
    print({k: v for k, v in res.items() if k != "rows"})
