"""Split-tree inference against rendered ground truth and a real Ghostty capture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.fabric.skills import layout_tree as lt
from app.fabric.skills import synth
from app.fabric.skills.imaging import Raster, load_raster

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "hands" / "screens"


def _ratios(tree):
    """All split ratios in tree order."""
    if tree["kind"] == "pane":
        return []
    return [tree["ratio"], *_ratios(tree["a"]), *_ratios(tree["b"])]


@pytest.mark.parametrize("layout", list(synth.LAYOUTS))
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_partition_matches_for_every_layout_and_theme(layout, theme):
    img, truth = synth.render(layout, theme=theme, seed=3)
    got = lt.infer_layout(img)
    assert got["pane_count"] == lt.count_panes(truth), (layout, theme, lt.structure_signature(got["tree"]))
    assert lt.same_partition(got["tree"], truth, tol=0.03), (layout, theme)


@pytest.mark.parametrize("layout", ["two_cols", "cols_60_40", "cols_30_70", "grid_2x2",
                                    "uneven_nested", "five_panes"])
def test_ratios_are_within_five_percent(layout):
    img, truth = synth.render(layout, seed=5)
    got = lt.infer_layout(img)
    assert lt.same_partition(got["tree"], truth, tol=0.05)
    if lt.structure_signature(got["tree"]) == lt.structure_signature(truth):
        for a, b in zip(_ratios(got["tree"]), _ratios(truth), strict=True):
            assert abs(a - b) < 0.05, (layout, a, b)


@pytest.mark.parametrize("text", ["none", "sparse", "dense", "full"])
def test_text_density_does_not_create_or_hide_dividers(text):
    img, truth = synth.render("left_plus_two", text=text, seed=11)
    assert lt.infer_layout(img)["pane_count"] == 3


def test_partial_tui_rules_are_not_dividers():
    img, truth = synth.render("two_cols", tui_rules=True, seed=2)
    got = lt.infer_layout(img)
    assert got["pane_count"] == 2


def test_dimmed_unfocused_panes_use_step_edges():
    img, truth = synth.render("grid_2x2", unfocused_dim=True, div_w=1, seed=4)
    got = lt.infer_layout(img)
    assert lt.same_partition(got["tree"], truth, tol=0.05)


@pytest.mark.parametrize("theme", ["solarized", "black", "paper"])
def test_other_themes(theme):
    img, truth = synth.render("five_panes", theme=theme, seed=8)
    got = lt.infer_layout(img)
    assert lt.same_partition(got["tree"], truth, tol=0.04), theme


def test_partial_occlusion_over_text_keeps_the_layout():
    img, truth = synth.render("grid_2x2", seed=9, occlusion=(0.55, 0.12, 0.3, 0.12))
    assert lt.infer_layout(img)["pane_count"] == 4


def test_occlusion_across_a_divider_is_tolerated_up_to_the_gap():
    # a small notification covering a few rows of the divider must not hide it
    img, truth = synth.render("two_cols", seed=9, occlusion=(0.45, 0.3, 0.1, 0.03))
    assert lt.infer_layout(img)["pane_count"] == 2


def test_sensor_noise_and_wide_dividers():
    img, truth = synth.render("three_cols", seed=1, noise=2, div_w=3)
    got = lt.infer_layout(img)
    assert lt.same_partition(got["tree"], truth, tol=0.04)


def test_no_split_is_one_pane():
    img, _ = synth.render("single", seed=1)
    assert lt.infer_layout(img)["pane_count"] == 1


def test_blank_flat_image_is_one_pane():
    img = Raster.from_fn(300, 200, lambda x, y: (30, 30, 30))
    assert lt.infer_layout(img)["pane_count"] == 1


def test_leaves_are_in_reading_order_and_numbered():
    img, truth = synth.render("left_plus_two", seed=2)
    tree = lt.infer_layout(img)["tree"]
    ids = [p["id"] for p in lt.panes(tree)]
    assert ids == [1, 2, 3]
    first = lt.panes(tree)[0]["rect"]
    assert first[0] == 0 and first[3] > 0.95  # the tall left pane comes first


def test_ascii_preview_shows_every_pane_label():
    tree = synth.true_tree(synth.LAYOUTS["left_plus_two"])
    art = lt.render_ascii(tree, labels={1: "codex", 2: "claude", 3: "muse"})
    for label in ("codex", "claude", "muse"):
        assert label in art


def test_same_partition_is_order_insensitive_for_grids():
    a = synth.true_tree(("R", 0.5, ("D", 0.5, "P", "P"), ("D", 0.5, "P", "P")))
    b = synth.true_tree(("D", 0.5, ("R", 0.5, "P", "P"), ("R", 0.5, "P", "P")))
    assert lt.same_partition(a, b)
    assert not lt.same_partition(a, synth.true_tree(synth.LAYOUTS["six_panes_grid"]))


def test_png_roundtrip_through_the_loader():
    img, truth = synth.render("cols_60_40", seed=6, w=600, h=400)
    loaded = load_raster(img.to_png(), max_dim=1000)
    assert (loaded.w, loaded.h) == (600, 400)
    assert lt.same_partition(lt.infer_layout(loaded)["tree"], truth, tol=0.04)


@pytest.mark.skipif(not (FIX / "real_ghostty_r_dd.png").exists(), reason="real fixture not captured")
def test_real_ghostty_capture():
    truth = json.loads((FIX / "real_ghostty_r_dd.json").read_text())
    img = load_raster(FIX / "real_ghostty_r_dd.png", max_dim=1000)
    got = lt.infer_layout(img)
    expected = synth.true_tree(tuple(_tupleize(truth["spec"])))
    assert got["pane_count"] == truth["panes"], lt.structure_signature(got["tree"])
    assert lt.same_partition(got["tree"], expected, tol=0.06)


def _tupleize(spec):
    return tuple(_tupleize(s) for s in spec) if isinstance(spec, list) else spec
