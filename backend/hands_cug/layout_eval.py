"""Layout-only evaluation on the real screenshot corpus (no OCR)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from app.fabric.skills import layout_tree as lt
from app.fabric.skills import synth
from app.fabric.skills.imaging import load_raster

D = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "hands" / "screens" / "real"


def tup(s):
    return s if isinstance(s, str) else (s[0], s[1], tup(s[2]), tup(s[3]))


def main(names: list[str]) -> None:
    ok = n = 0
    for mp in sorted(D.glob("*.json")):
        m = json.loads(mp.read_text())
        if names and m["name"] not in names:
            continue
        t = time.time()
        out = lt.infer_layout(load_raster(mp.with_suffix(".png"), max_dim=1000))
        truth = synth.true_tree(tup(m["spec"]))
        good = out["pane_count"] == m["panes"] and lt.same_partition(out["tree"], truth, tol=0.08)
        ok += good
        n += 1
        print(f"{m['name']:34s} {'OK ' if good else 'BAD'} got={out['pane_count']} want={m['panes']} "
              f"{lt.structure_signature(out['tree'])} {time.time() - t:.1f}s", flush=True)
    print(ok, "/", n, flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
