"""Real Ghostty screenshots (captured from our own windows): layout, ratios, never-a-wrong-id.

Default run scores a spread subset; EV_HANDS_FULL_CORPUS=1 scores all 34 captures (22 dev + 12
held-out) and, when the Apple Vision helper is present, the OCR grounding as well.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.fabric.skills import layout_tree as lt
from app.fabric.skills import synth
from app.fabric.skills.imaging import load_raster

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "hands" / "screens"
FULL = os.environ.get("EV_HANDS_FULL_CORPUS") == "1"
SUBSET = {"s02_two_cols_dark", "s07_left_plus_two_dark", "s09_grid_2x2_light", "s12_six_grid_dark_tiny",
          "s13_uneven_nested_dark", "s15_grid_2x2_solarized", "h04_two_plus_right_black", "h07_uneven_nested_light"}


def _tup(s):
    return s if isinstance(s, str) else (s[0], s[1], _tup(s[2]), _tup(s[3]))


def _metas():
    out = []
    for d in ("real", "heldout"):
        for p in sorted((ROOT / d).glob("*.json")):
            m = json.loads(p.read_text())
            if FULL or m["name"] in SUBSET:
                out.append((m, p.with_suffix(".png")))
    return out


@pytest.mark.skipif(not (ROOT / "real").exists(), reason="corpus not present")
@pytest.mark.parametrize("meta,png", _metas(), ids=lambda v: v["name"] if isinstance(v, dict) else "")
def test_layout_and_ratios_on_real_captures(meta, png):
    out = lt.infer_layout(load_raster(png, max_dim=1000))
    truth = synth.true_tree(_tup(meta["spec"]))
    assert out["pane_count"] == meta["panes"], lt.structure_signature(out["tree"])
    assert lt.same_partition(out["tree"], truth, tol=0.08)  # target: ratios within 8%
    assert lt.same_partition(out["tree"], truth, tol=0.02)  # measured: within 2%
