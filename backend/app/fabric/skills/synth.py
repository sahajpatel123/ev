"""Synthetic terminal screenshots with exact ground truth (tests and CUG corpus).

Renders a split layout the way a terminal draws it: flat pane backgrounds,
thin low-contrast dividers, text-like blocks, an optional title bar, dimmed
unfocused panes, partial occlusion and TUI-style partial rules. The renderer
knows the true tree, so inference can be scored exactly. No site or app
artwork is reproduced; this is geometry and flat colour only.
"""

from __future__ import annotations

import random
from typing import Any

from .imaging import Raster
from .layout_tree import renumber, tree_from_partition_spec

THEMES: dict[str, dict[str, tuple[int, int, int]]] = {
    "dark": {"bg": (40, 44, 52), "fg": (171, 178, 191), "div": (26, 28, 33), "bar": (33, 37, 43)},
    "light": {"bg": (250, 250, 250), "fg": (56, 58, 66), "div": (205, 205, 210), "bar": (236, 236, 238)},
    "solarized": {"bg": (0, 43, 54), "fg": (131, 148, 150), "div": (7, 54, 66), "bar": (0, 36, 46)},
    "black": {"bg": (0, 0, 0), "fg": (200, 200, 200), "div": (50, 50, 50), "bar": (14, 14, 14)},
    "paper": {"bg": (253, 246, 227), "fg": (101, 123, 131), "div": (225, 215, 190), "bar": (238, 232, 213)},
}

# name -> compact spec accepted by tree_from_partition_spec
LAYOUTS: dict[str, Any] = {
    "single": "P",
    "two_cols": ("R", 0.5, "P", "P"),
    "two_rows": ("D", 0.5, "P", "P"),
    "cols_60_40": ("R", 0.6, "P", "P"),
    "cols_30_70": ("R", 0.3, "P", "P"),
    "three_cols": ("R", 0.3333, "P", ("R", 0.5, "P", "P")),
    "left_plus_two": ("R", 0.5, "P", ("D", 0.5, "P", "P")),
    "two_plus_right": ("R", 0.5, ("D", 0.5, "P", "P"), "P"),
    "grid_2x2": ("R", 0.5, ("D", 0.5, "P", "P"), ("D", 0.5, "P", "P")),
    "top_plus_three": ("D", 0.4, "P", ("R", 0.3333, "P", ("R", 0.5, "P", "P"))),
    "five_panes": ("R", 0.4, ("D", 0.5, "P", "P"), ("D", 0.3333, "P", ("D", 0.5, "P", "P"))),
    "six_panes_grid": ("R", 0.3333, ("D", 0.5, "P", "P"),
                       ("R", 0.5, ("D", 0.5, "P", "P"), ("D", 0.5, "P", "P"))),
    "uneven_nested": ("D", 0.65, ("R", 0.7, "P", "P"), ("R", 0.4, "P", "P")),
    "four_cols": ("R", 0.25, "P", ("R", 0.3333, "P", ("R", 0.5, "P", "P"))),
}


def true_tree(spec: Any) -> dict[str, Any]:
    t = tree_from_partition_spec(spec)
    renumber(t)
    return t


def render(layout: str | Any, *, w: int = 1000, h: int = 680, theme: str = "dark",
           div_w: int = 2, titlebar: int = 44, text: str = "dense", seed: int = 0,
           unfocused_dim: bool = False, tui_rules: bool = False,
           occlusion: tuple[float, float, float, float] | None = None,
           traffic_lights: bool = True, noise: int = 0) -> tuple[Raster, dict[str, Any]]:
    """Render and return (raster, true_tree). `occlusion` is an (x,y,w,h) fraction box
    painted over the image (a notification, another window)."""
    spec = LAYOUTS[layout] if isinstance(layout, str) else layout
    tree = true_tree(spec)
    th = THEMES[theme]
    rng = random.Random(seed)
    img = [bytearray(bytes(th["bg"]) * w) for _ in range(h)]

    def rect(x0: int, y0: int, x1: int, y1: int, col: tuple[int, int, int]) -> None:
        x0, x1 = max(0, x0), min(w, x1)
        for y in range(max(0, y0), min(h, y1)):
            img[y][x0 * 3:x1 * 3] = bytes(col) * (x1 - x0)

    # title bar (same colour family as the terminal, like a blended titlebar)
    rect(0, 0, w, titlebar, th["bar"])
    if traffic_lights:
        for i, c in enumerate(((237, 106, 94), (245, 191, 79), (97, 197, 84))):
            cx, cy = 22 + i * 22, titlebar // 2
            for dy in range(-6, 7):
                for dx in range(-6, 7):
                    if dx * dx + dy * dy <= 36:
                        rect(cx + dx, cy + dy, cx + dx + 1, cy + dy + 1, c)
    panes_px: list[tuple[int, int, int, int]] = []
    dim = tuple(int(c * 0.88) for c in th["bg"])

    def walk(node: dict[str, Any], x0: int, y0: int, x1: int, y1: int, focused: list[bool]) -> None:
        if node["kind"] == "pane":
            panes_px.append((x0, y0, x1, y1))
            if unfocused_dim and len(panes_px) > 1:
                rect(x0, y0, x1, y1, dim)
            return
        ra = float(node["ratio"])
        if node["dir"] == "right":
            total = (x1 - x0) - div_w
            first = round(total * ra)
            rect(x0 + first, y0, x0 + first + div_w, y1, th["div"])
            walk(node["a"], x0, y0, x0 + first, y1, focused)
            walk(node["b"], x0 + first + div_w, y0, x1, y1, focused)
        else:
            total = (y1 - y0) - div_w
            first = round(total * ra)
            rect(x0, y0 + first, x1, y0 + first + div_w, th["div"])
            walk(node["a"], x0, y0, x1, y0 + first, focused)
            walk(node["b"], x0, y0 + first + div_w, x1, y1, focused)

    walk(tree, 0, titlebar, w, h, [True])
    if text != "none":
        density = {"sparse": 0.18, "dense": 0.9, "full": 1.0}[text]
        for (x0, y0, x1, y1) in panes_px:
            line_h = 14
            for ly in range(y0 + 8, y1 - 8, line_h):
                if rng.random() > density:
                    continue
                cx = x0 + 8
                end = x1 - 8 - (0 if text == "full" else rng.randint(0, max(1, (x1 - x0) // 3)))
                while cx < end:
                    run = rng.randint(2, 9) * 6
                    if rng.random() < 0.8:
                        rect(cx, ly, min(cx + run, end), ly + 8, th["fg"])
                    cx += run + 6
    if tui_rules:
        for (x0, y0, x1, y1) in panes_px:
            if x1 - x0 > 120 and y1 - y0 > 120:
                xr = x0 + (x1 - x0) * 2 // 3
                rect(xr, y0 + (y1 - y0) // 5, xr + 1, y0 + (y1 - y0) * 3 // 5, th["fg"])
    if occlusion:
        ox, oy, ow, oh = occlusion
        rect(int(ox * w), int(oy * h), int((ox + ow) * w), int((oy + oh) * h), (96, 99, 108))
    if noise:
        for y in range(h):
            row = img[y]
            for i in range(len(row)):
                row[i] = max(0, min(255, row[i] + rng.randint(-noise, noise)))
    r = Raster(w, h, [bytes(row) for row in img])
    # ground-truth rects normalised to the content area (below the titlebar)
    return r, tree
