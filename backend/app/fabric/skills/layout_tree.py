"""Split-tree inference from a terminal screenshot (T1).

Terminal splits are guillotine cuts: every divider spans its whole region. The
detector looks for such dividers directly in pixels, with no model call:

* a LINE divider is a thin band whose pixels differ from both neighbours while
  the two neighbours agree with each other, continuing for most of the region;
* a STEP divider is where two flat pane backgrounds meet (dimmed unfocused panes).

Text, TUI chrome and window decorations are rejected because they do not form
an unbroken edge-to-edge run. Regions are split recursively; ratios come from
the measured pane extents. Output nodes use Ghostty's vocabulary:

  {"kind":"split","dir":"right"|"down","ratio":a_share,"a":node,"b":node}
  {"kind":"pane","id":n,"rect":[x,y,w,h]}   (rect normalised to the content area)

`dir:"right"` means panes sit side by side (a is left); `"down"` stacked (a is top).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .imaging import Raster

SIM = 10    # L1 colour distance below which two pixels are "the same"
SIM_LINE = 24  # neighbours of a thin line may differ this much (dimmed unfocused pane)
DIFF = 14   # ... and above which they are clearly different
K = 4       # neighbour offset: supports dividers up to 4 px wide at analysis scale
MAX_BAND = 6


def _cd(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


@dataclass
class Band:
    axis: str            # "v" (vertical divider, splits left/right) or "h"
    a: int               # first pixel of the band
    b: int               # last pixel of the band (inclusive)
    run_start: int
    run_end: int
    strength: float      # run length / region extent along the divider
    kind: str            # line | step

    @property
    def width(self) -> int:
        return self.b - self.a + 1


# ------------------------------------------------------------- primitives ---

def _is_line(r: Raster, x: int, y: int, axis: str) -> bool:
    if axis == "v":
        p, lo, hi = r.px(x, y), r.px(x - K, y), r.px(x + K, y)
    else:
        p, lo, hi = r.px(x, y), r.px(x, y - K), r.px(x, y + K)
    return _cd(lo, hi) <= SIM_LINE and _cd(p, lo) >= DIFF and _cd(p, hi) >= DIFF \
        and _cd(p, lo) + _cd(p, hi) >= _cd(lo, hi) + 2 * DIFF


def _is_step(r: Raster, x: int, y: int, axis: str) -> bool:
    if axis == "v":
        before, here, far_b, far_a = r.px(x - 1, y), r.px(x, y), r.px(x - K, y), r.px(x + K - 1, y)
    else:
        before, here, far_b, far_a = r.px(x, y - 1), r.px(x, y), r.px(x, y - K), r.px(x, y + K - 1)
    return _cd(before, here) >= DIFF and _cd(before, far_b) <= SIM and _cd(here, far_a) <= SIM


def _best_run(flags: list[bool], extent: int, *, tol_gap: int = 2, occl_frac: float = 0.06,
              occl_min: float = 0.3) -> tuple[int, int, int]:
    """Longest unbroken run (start, end exclusive, covered pixels).

    A divider is continuous: gaps of at most `tol_gap` pixels are forgiven. Two
    long runs separated by one short gap (a notification or another window over
    the divider) are joined; the many short runs of a text row never are.
    """
    runs: list[list[int]] = []
    start = last = None
    for i, f in enumerate(flags):
        if not f:
            continue
        if start is None:
            start = last = i
        elif i - last <= tol_gap + 1:
            last = i
        else:
            runs.append([start, last + 1])
            start = last = i
    if start is not None:
        runs.append([start, last + 1])
    merged: list[list[int]] = []
    for run in runs:
        if merged:
            prev = merged[-1]
            gap = run[0] - prev[1]
            if (gap <= occl_frac * extent and prev[1] - prev[0] >= occl_min * extent
                    and run[1] - run[0] >= occl_min * extent):
                prev[1] = run[1]
                prev.append(gap)
                continue
        merged.append(run)
    if not merged:
        return (0, 0, 0)
    best = max(merged, key=lambda r: r[1] - r[0] - sum(r[2:]))
    return best[0], best[1], best[1] - best[0] - sum(best[2:])


def _bands(r: Raster, x0: int, y0: int, x1: int, y1: int, axis: str, kind: str,
           min_strength: float) -> list[Band]:
    """Dividers of one axis inside the region [x0,x1)x[y0,y1)."""
    if axis == "v":
        lo, hi, a0, a1 = x0 + K, x1 - K, y0, y1
    else:
        lo, hi, a0, a1 = y0 + K, y1 - K, x0, x1
    extent = a1 - a0
    if hi - lo < 2 or extent < 16:
        return []
    test = _is_line if kind == "line" else _is_step
    n_samples = min(48, extent)
    samples = [a0 + int((i + 0.5) * extent / n_samples) for i in range(n_samples)]
    hits_idx: list[tuple[int, int, int]] = []
    for pos in range(lo, hi):
        if axis == "v":
            quick = sum(1 for s in samples if test(r, pos, s, "v"))
        else:
            quick = sum(1 for s in samples if test(r, s, pos, "h"))
        if quick < 0.55 * n_samples:
            continue
        if axis == "v":
            flags = [test(r, pos, y, "v") for y in range(a0, a1)]
        else:
            flags = [test(r, x, pos, "h") for x in range(a0, a1)]
        s, e, covered = _best_run(flags, extent)
        if covered / extent >= min_strength:
            hits_idx.append((pos, a0 + s, a0 + e))
    bands: list[Band] = []
    for pos, rs, re_ in hits_idx:
        if bands and pos - bands[-1].b <= 1:
            b = bands[-1]
            b.b = pos
            b.run_start = max(b.run_start, rs)
            b.run_end = min(b.run_end, re_)
            b.strength = (b.run_end - b.run_start) / extent
        else:
            bands.append(Band(axis, pos, pos, rs, re_, (re_ - rs) / extent, kind))
    return [b for b in bands if b.width <= MAX_BAND and b.strength >= min_strength]


# ------------------------------------------------------------ tree builder ---

def _chrome_top(r: Raster) -> int | None:
    """Row where a window's title bar ends, from the traffic-light buttons."""
    ys: list[int] = []
    xs: list[int] = []
    for y in range(min(r.h, int(r.h * 0.12))):
        for x in range(min(r.w, int(r.w * 0.12))):
            pr, pg, pb = r.px(x, y)
            if pr > 200 and pg < 115 and pb < 110:
                ys.append(y)
                xs.append(x)
    if len(ys) < 6:
        return None
    cy = sum(ys) / len(ys)
    return int(2 * cy + 1)


def infer_layout(r: Raster, *, min_pane_frac: float = 0.07,
                 min_strength: float = 0.8, top_hint: int | None = None) -> dict[str, Any]:
    """Infer the split tree of a terminal window screenshot."""
    W, H = r.w, r.h
    x0, y0, x1, y1 = 0, 0, W, H
    notes: list[str] = []
    # Strip transparent margins (window captures have rounded transparent corners).
    if r.alpha is not None:
        x0, y0, x1, y1 = _opaque_box(r)
    # Title-bar / menu-bar: vertical dividers start below it.
    root_v = _bands(r, x0, y0, x1, y1, "v", "line", 0.62) or _bands(r, x0, y0, x1, y1, "v", "step", 0.62)
    top = y0
    if root_v:
        top = max(y0, min(b.run_start for b in root_v) - 0)
    else:
        guess = top_hint if top_hint is not None else _chrome_top(r)
        if guess is not None:
            top = max(y0, min(guess, y1 - 8))
        else:
            notes.append("no vertical dividers and no window chrome found; vertical extents are uncertain")
    counter = {"n": 0}
    min_w, min_h = max(12, int(W * min_pane_frac)), max(12, int(H * min_pane_frac))
    content = (x0, top, x1, y1)

    def leaf(rx0: int, ry0: int, rx1: int, ry1: int) -> dict[str, Any]:
        counter["n"] += 1
        cw, ch = max(1, content[2] - content[0]), max(1, content[3] - content[1])
        return {"kind": "pane", "id": counter["n"],
                "rect": [round((rx0 - content[0]) / cw, 4), round((ry0 - content[1]) / ch, 4),
                         round((rx1 - rx0) / cw, 4), round((ry1 - ry0) / ch, 4)],
                "px": [rx0, ry0, rx1 - rx0, ry1 - ry0]}

    def build(rx0: int, ry0: int, rx1: int, ry1: int, depth: int) -> dict[str, Any]:
        if depth > 8 or rx1 - rx0 < 2 * min_w or ry1 - ry0 < 2 * min_h:
            return leaf(rx0, ry0, rx1, ry1)
        found = None
        for kind in ("line", "step"):
            vb = [b for b in _bands(r, rx0, ry0, rx1, ry1, "v", kind, min_strength)
                  if b.a - rx0 >= min_w and rx1 - b.b - 1 >= min_w]
            hb = [b for b in _bands(r, rx0, ry0, rx1, ry1, "h", kind, min_strength)
                  if b.a - ry0 >= min_h and ry1 - b.b - 1 >= min_h]
            if vb or hb:
                if vb and (not hb or max(b.strength for b in vb) >= max(b.strength for b in hb) - 0.02):
                    found = ("right", max(vb, key=lambda b: b.strength))
                else:
                    found = ("down", max(hb, key=lambda b: b.strength))
                break
        if found is None:
            return leaf(rx0, ry0, rx1, ry1)
        direction, band = found
        # choose the cut closest to the region's start so chains nest left/top-first
        cands = [b for b in (vb if direction == "right" else hb) if abs(b.strength - band.strength) < 0.06]
        band = min(cands, key=lambda b: b.a) if cands else band
        if direction == "right":
            first, second = band.a - rx0, rx1 - (band.b + 1)
            ratio = first / (first + second)
            node_a = build(rx0, ry0, band.a, ry1, depth + 1)
            node_b = build(band.b + 1, ry0, rx1, ry1, depth + 1)
        else:
            first, second = band.a - ry0, ry1 - (band.b + 1)
            ratio = first / (first + second)
            node_a = build(rx0, ry0, rx1, band.a, depth + 1)
            node_b = build(rx0, band.b + 1, rx1, ry1, depth + 1)
        return {"kind": "split", "dir": direction, "ratio": round(ratio, 4),
                "kind_detail": band.kind, "strength": round(band.strength, 3),
                "a": node_a, "b": node_b}

    tree = build(x0, top, x1, y1, 0)
    renumber(tree)
    n = count_panes(tree)
    return {"width": W, "height": H, "scale": r.scale, "content_px": [x0, top, x1 - x0, y1 - top],
            "tree": tree, "pane_count": n, "notes": notes,
            "ratio_uncertain": bool(notes) and n > 1}


def _opaque_box(r: Raster) -> tuple[int, int, int, int]:
    assert r.alpha is not None
    xs0, ys0, xs1, ys1 = r.w, r.h, 0, 0
    for y in range(0, r.h, 2):
        row = r.alpha[y]
        first = next((x for x in range(r.w) if row[x] > 200), None)
        if first is None:
            continue
        last = max(x for x in range(r.w - 1, -1, -1) if row[x] > 200)
        xs0, xs1 = min(xs0, first), max(xs1, last + 1)
        ys0, ys1 = min(ys0, y), max(ys1, y + 1)
    if xs1 <= xs0 or ys1 <= ys0:
        return 0, 0, r.w, r.h
    # rounded window corners are transparent; inset a little so they never read as edges
    inset = max(2, int(0.004 * r.w))
    return xs0 + inset, ys0 + inset, xs1 - inset, ys1 - inset


# ------------------------------------------------------------ tree helpers ---

def renumber(tree: dict[str, Any]) -> None:
    n = [0]

    def walk(node: dict[str, Any]) -> None:
        if node["kind"] == "pane":
            n[0] += 1
            node["id"] = n[0]
        else:
            walk(node["a"])
            walk(node["b"])

    walk(tree)


def count_panes(node: dict[str, Any]) -> int:
    return 1 if node["kind"] == "pane" else count_panes(node["a"]) + count_panes(node["b"])


def panes(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Leaves in tree order (a before b), i.e. reading order for nested cuts."""
    if node["kind"] == "pane":
        return [node]
    return panes(node["a"]) + panes(node["b"])


def partition(node: dict[str, Any], rect: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
              ) -> list[tuple[float, float, float, float]]:
    """Leaf rectangles (x,y,w,h in 0..1) implied by splits and ratios; the
    canonical form for comparing two layouts irrespective of cut order."""
    if node["kind"] == "pane":
        return [rect]
    x, y, w, h = rect
    ra = float(node["ratio"])
    if node["dir"] == "right":
        return partition(node["a"], (x, y, w * ra, h)) + partition(node["b"], (x + w * ra, y, w * (1 - ra), h))
    return partition(node["a"], (x, y, w, h * ra)) + partition(node["b"], (x, y + h * ra, w, h * (1 - ra)))


def same_partition(a: dict[str, Any], b: dict[str, Any], tol: float = 0.08) -> bool:
    """Two layouts are the same when their leaf rectangles match within `tol`."""
    ra, rb = partition(a), partition(b)
    if len(ra) != len(rb):
        return False
    used: set[int] = set()
    for p in ra:
        hit = None
        for i, q in enumerate(rb):
            if i in used:
                continue
            if all(abs(p[k] - q[k]) <= tol for k in range(4)):
                hit = i
                break
        if hit is None:
            return False
        used.add(hit)
    return True


def structure_signature(node: dict[str, Any]) -> str:
    """Cut-order-sensitive shape string, e.g. R(P,D(P,P))."""
    if node["kind"] == "pane":
        return "P"
    return f"{'R' if node['dir'] == 'right' else 'D'}({structure_signature(node['a'])},{structure_signature(node['b'])})"


def render_ascii(node: dict[str, Any], cols: int = 56, rows: int = 12,
                 labels: dict[int, str] | None = None) -> str:
    """Small picture of the layout for the confirm card."""
    grid = [[" "] * cols for _ in range(rows)]

    def paint(rect: tuple[float, float, float, float], pane: dict[str, Any]) -> None:
        x, y, w, h = rect
        c0, r0 = int(round(x * (cols - 1))), int(round(y * (rows - 1)))
        c1, r1 = int(round((x + w) * (cols - 1))), int(round((y + h) * (rows - 1)))
        for c in range(c0, c1 + 1):
            grid[r0][c] = grid[r1][c] = "-" if grid[r0][c] == " " else grid[r0][c]
        for rr in range(r0, r1 + 1):
            grid[rr][c0] = grid[rr][c1] = "|" if grid[rr][c0] in " |" else "+"
        for cc in (c0, c1):
            for rr in (r0, r1):
                grid[rr][cc] = "+"
        label = (labels or {}).get(pane["id"], str(pane["id"]))[:max(1, c1 - c0 - 1)]
        mid_r, mid_c = (r0 + r1) // 2, c0 + 1
        for i, ch in enumerate(label):
            if mid_c + i < c1:
                grid[mid_r][mid_c + i] = ch

    def walk(n: dict[str, Any], rect: tuple[float, float, float, float]) -> None:
        if n["kind"] == "pane":
            paint(rect, n)
            return
        x, y, w, h = rect
        ra = float(n["ratio"])
        if n["dir"] == "right":
            walk(n["a"], (x, y, w * ra, h))
            walk(n["b"], (x + w * ra, y, w * (1 - ra), h))
        else:
            walk(n["a"], (x, y, w, h * ra))
            walk(n["b"], (x, y + h * ra, w, h * (1 - ra)))

    walk(node, (0.0, 0.0, 1.0, 1.0))
    return "\n".join("".join(row).rstrip() for row in grid)


def tree_from_partition_spec(spec: Any) -> dict[str, Any]:
    """Build a tree from a compact spec: 'P' | ('R'|'D', ratio, a, b)."""
    if spec == "P":
        return {"kind": "pane", "id": 0}
    kind, ratio, a, b = spec
    return {"kind": "split", "dir": "right" if kind == "R" else "down", "ratio": ratio,
            "a": tree_from_partition_spec(a), "b": tree_from_partition_spec(b)}


def canonicalize(node: dict[str, Any]) -> dict[str, Any]:
    """Equivalent tree where consecutive same-direction splits form a right-leaning chain.

    Terminals move a divider via the NEAREST same-axis ancestor, so every divider
    needs a leaf that reaches it directly; flattening guarantees one. The leaf
    order and the partition are unchanged.
    """
    if node["kind"] == "pane":
        return {"kind": "pane", "id": node.get("id", 0)}
    direction = node["dir"]

    def runs(n: dict[str, Any], weight: float) -> list[tuple[float, dict[str, Any]]]:
        if n["kind"] == "split" and n["dir"] == direction:
            r = float(n["ratio"])
            return runs(n["a"], weight * r) + runs(n["b"], weight * (1 - r))
        return [(weight, n)]

    parts = runs(node, 1.0)

    def chain(items: list[tuple[float, dict[str, Any]]]) -> dict[str, Any]:
        if len(items) == 1:
            return canonicalize(items[0][1])
        total = sum(w for w, _ in items)
        first = items[0][0] / total
        return {"kind": "split", "dir": direction, "ratio": round(first, 4),
                "a": canonicalize(items[0][1]), "b": chain(items[1:])}

    out = chain(parts)
    renumber(out)
    return out
