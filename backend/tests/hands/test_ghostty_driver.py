"""Ghostty driver against a scripted runtime: scripts generated, ids parsed, loop converges."""

from __future__ import annotations

import json

import pytest

from app.fabric.skills import layout_tree as lt
from app.fabric.skills import synth
from app.fabric.skills.packs.ghostty import GhosttyDriver, GhosttyError, SEP, probe
from app.fabric.skills.runtime import Proc
from app.fabric.skills.testing import FakeRuntime


def _drv():
    rt = FakeRuntime()
    rt.when(r"^ps ", Proc(True, out="/Applications/Ghostty.app/Contents/MacOS/ghostty\n"), kind="run")
    return GhosttyDriver(rt), rt


def test_inventory_parses_unit_separator_rows_and_titles_with_commas():
    d, rt = _drv()
    rows = [SEP.join(["tab-group-1", "tab-9", "2", "true", "T-UUID", "/Users/x/Code/ev", "a, b: ✳ title"]),
            SEP.join(["tab-group-1", "tab-9", "2", "true", "T-UUID2", "/Users/x", "plain"])]
    rt.when(r"every window|repeat with w in windows", "\n".join(rows) + "\n")
    inv = d.inventory()
    assert [t.terminal_id for t in inv] == ["T-UUID", "T-UUID2"]
    assert inv[0].title == "a, b: ✳ title" and inv[0].selected and inv[0].tab_index == 2
    assert d.inventory(only_windows=["other"]) == []


def test_not_running_is_an_honest_error():
    rt = FakeRuntime()
    rt.when(r"^ps ", Proc(True, out=""), kind="run")
    with pytest.raises(GhosttyError) as e:
        GhosttyDriver(rt).inventory()
    assert e.value.code == "not_running"


def test_new_tab_targets_the_window_by_id_and_carries_cwd_and_input():
    d, rt = _drv()
    rt.when(r"new tab in window id", f"tab-77{SEP}TERM-1")
    out = d.new_tab("tab-group-9", cwd='/Users/x/My "Dir"', initial_input="echo hi")
    assert out == {"window_id": "tab-group-9", "tab_id": "tab-77", "terminal_id": "TERM-1"}
    script = rt.scripts("new tab")[0]
    assert 'new tab in window id "tab-group-9" with configuration cfg' in script
    assert 'initial working directory of cfg to "/Users/x/My \\"Dir\\""' in script
    assert 'initial input of cfg to "echo hi"' in script


def test_split_rejects_bad_direction_and_names_the_terminal():
    d, rt = _drv()
    with pytest.raises(GhosttyError):
        d.split("T", "diagonal")
    rt.when(r"split terminal id", "NEW-TERM")
    assert d.split("T-1", "down", cwd="/tmp") == "NEW-TERM"
    assert 'split terminal id "T-1" direction down with configuration cfg' in rt.scripts("split")[0]


def test_input_text_appends_a_newline_once_and_escapes():
    d, rt = _drv()
    rt.when(r"input text", "")
    d.input_text("T", 'echo "a\\b"')
    d.input_text("T", "ls\n")
    first, second = rt.scripts("input text")
    assert first.count("\n") >= 1 and 'echo \\"a\\\\b\\"' in first
    assert second.count('ls\n"') == 1  # not doubled


def test_permission_error_names_the_setting():
    d, rt = _drv()
    rt.when(r"new window", Proc(False, err="Not authorized to send Apple events to Ghostty. (-1743)"))
    with pytest.raises(GhosttyError) as e:
        d.new_window()
    assert e.value.code == "permission_missing" and "Automation" in (e.value.fix or "")


def test_canonicalize_keeps_partition_and_makes_same_axis_chains():
    balanced = synth.true_tree(("R", 0.5, ("R", 0.5, "P", "P"), ("R", 0.5, "P", "P")))
    canon = lt.canonicalize(balanced)
    assert lt.same_partition(balanced, canon, tol=0.002)
    assert lt.structure_signature(canon) == "R(P,R(P,R(P,P)))"
    ratios = []
    n = canon
    while n["kind"] == "split":
        ratios.append(n["ratio"])
        n = n["b"]
    assert ratios == [0.25, 0.3333, 0.5]
    mixed = synth.true_tree(synth.LAYOUTS["five_panes"])
    assert lt.same_partition(mixed, lt.canonicalize(mixed), tol=0.002)
    assert [p["id"] for p in lt.panes(canon)] == [1, 2, 3, 4]


def test_direct_leaf_never_sits_under_a_same_axis_split():
    from app.fabric.skills.packs.ghostty import _direct_leaf

    tree = lt.canonicalize(synth.true_tree(("D", 0.4, ("R", 0.5, "P", "P"), "P")))
    leaf = _direct_leaf(tree["a"], "down")
    assert leaf["kind"] == "pane"


class _FakeAx:
    """A tiny model of AX pane geometry that resize_split actions mutate."""

    def __init__(self, tree, W=1000, H=600):
        self.tree, self.W, self.H = tree, W, H
        self.ratios = {id(n): 0.5 for n in _splits(tree)}

    def apply(self, node, direction, px):
        horizontal = node["dir"] == "right"
        extent = self.W if horizontal else self.H
        delta = px / extent
        self.ratios[id(node)] += delta if direction in ("right", "down") else -delta

    def panes(self):
        rects = []

        def walk(n, rect):
            x, y, w, h = rect
            if n["kind"] == "pane":
                rects.append({"pos": [x * self.W, y * self.H], "size": [w * self.W, h * self.H],
                              "focused": False, "text": ""})
                return
            r = self.ratios[id(n)]
            if n["dir"] == "right":
                walk(n["a"], (x, y, w * r, h))
                walk(n["b"], (x + w * r, y, w * (1 - r), h))
            else:
                walk(n["a"], (x, y, w, h * r))
                walk(n["b"], (x, y + h * r, w, h * (1 - r)))

        walk(self.tree, (0, 0, 1, 1))
        return rects


def _splits(n):
    return [] if n["kind"] == "pane" else [n, *_splits(n["a"]), *_splits(n["b"])]


def test_build_converges_the_ratio_loop_against_a_geometry_model():
    """The closed loop must reach the planned partition using only resize_split."""
    d, rt = _drv()
    tree = lt.canonicalize(synth.true_tree(synth.LAYOUTS["five_panes"]))
    model = _FakeAx(tree)
    term_to_node: dict[str, tuple[dict, str]] = {}

    class Ax:
        def available(self):
            return True

        def panes(self, app, title=None):
            return {"ok": True, "windows": [{"title": "T", "panes": model.panes()}]}

    d.ax = Ax()
    rt.when(r"name of window id", "T")
    counter = {"n": 0}

    def new_tab(_rt, script):
        return f"tab-1{SEP}T0"

    rt.when(r"new tab in window id", new_tab)

    def split(_rt, script):
        counter["n"] += 1
        return f"T{counter['n']}"

    rt.when(r"split terminal id", split)

    def resize(_rt, script):
        import re

        m = re.search(r'resize_split:(\w+),(\d+)" on terminal id "(T\d+)"', script)
        direction, px, term = m.group(1), int(m.group(2)), m.group(3)
        node = _nearest(tree, term_to_leaf[term], direction)
        model.apply(node, direction, px)
        return "true"

    rt.when(r"perform action", resize)
    rt.when(r"input text", "")
    leaves_order = lt.panes(tree)
    # terminal ids follow creation order in build(): T0 for the first leaf, then T1.. per split
    term_to_leaf: dict[str, int] = {}

    def _nearest(root, leaf_id, direction):
        axis = "right" if direction in ("left", "right") else "down"
        path = _path(root, leaf_id)
        for node in reversed(path):
            if node["kind"] == "split" and node["dir"] == axis:
                return node
        raise AssertionError("no ancestor")

    def _path(node, leaf_id):
        if node["kind"] == "pane":
            return [node] if node["id"] == leaf_id else None
        for child in (node["a"], node["b"]):
            sub = _path(child, leaf_id)
            if sub is not None:
                return [node, *sub]
        return None

    # replay build() creation order to learn which terminal id is which leaf
    created = {}

    def grow(node, term):
        if node["kind"] == "pane":
            created[node["id"]] = term
            return
        idx = [0]

        def first_leaf(n):
            return n if n["kind"] == "pane" else first_leaf(n["a"])

        new_term = f"T{len(created_splits) + 1}"
        created_splits.append(node)
        grow(node["a"], term)
        grow(node["b"], new_term)

    created_splits: list = []
    grow(tree, "T0")
    term_to_leaf.update({t: leaf for leaf, t in created.items()})
    leaves = [{"cwd": "/tmp", "command": ""} for _ in leaves_order]
    out = d.build(tree, leaves, window_id="W", ratio_tol=0.01)
    assert out["audit"]["ok"] is True, out["audit"]
    got = model.panes()
    assert len(got) == 5
    final = [[p["pos"][0] / 1000, p["pos"][1] / 600, p["size"][0] / 1000, p["size"][1] / 600] for p in got]
    for planned, real in zip(lt.partition(tree), final, strict=True):
        assert all(abs(a - b) < 0.01 for a, b in zip(planned, real, strict=True))


def test_probe_reports_without_running_ghostty():
    rt = FakeRuntime()
    rt.when(r"^test -d", Proc(True), kind="run")
    rt.when(r"ghostty --version", Proc(True, out="Ghostty 1.3.1\n"), kind="run")
    rt.when(r"^ps ", Proc(True, out=""), kind="run")
    info = probe(rt)
    assert info["installed"] and info["version"] == "1.3.1" and info["running"] is False
    assert info["scriptable"] is False
    assert json.dumps(info)
