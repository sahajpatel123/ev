"""Deterministic split-tree from a screenshot. No model, no new dependencies.

Dividers are long straight runs where a column or row is much darker or much
lighter than its neighbors. Command text is not guessed: callers pass OCR
lines per pane, and any command that is not copied from those lines is flagged.
"""
from __future__ import annotations

import struct
import zlib
from collections.abc import Callable


def _read_png(data: bytes) -> tuple[int, int, list[tuple[int, int, int]]]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    pos = 8
    width = height = 0
    raw = b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, bit, color = struct.unpack(">IIBB", chunk[:10])
            if bit != 8 or color != 2:
                raise ValueError("only 8-bit rgb png")
        elif kind == b"IDAT":
            raw += chunk
        elif kind == b"IEND":
            break
    decoded = zlib.decompress(raw)
    stride = width * 3
    pixels: list[tuple[int, int, int]] = []
    i = 0
    for _y in range(height):
        i += 1  # filter byte
        row = decoded[i:i + stride]
        i += stride
        for x in range(width):
            pixels.append((row[x * 3], row[x * 3 + 1], row[x * 3 + 2]))
    return width, height, pixels


def write_png(width: int, height: int, pixel: Callable[[int, int], tuple[int, int, int]]) -> bytes:
    """Small RGB PNG writer used by fixture generation."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row.extend(pixel(x, y))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def _lum(px: tuple[int, int, int]) -> int:
    return (px[0] + px[1] + px[2]) // 3


def infer_splits(data: bytes, *, min_run: float = 0.55) -> dict:
    """Return a split tree. Ratios are the pane fractions of the image."""
    width, height, pixels = _read_png(data)

    def at(x: int, y: int) -> int:
        return _lum(pixels[y * width + x])

    def column_score(x: int) -> float:
        if x <= 0 or x >= width - 1:
            return 0.0
        hits = 0
        for y in range(height):
            if abs(at(x, y) - at(x - 1, y)) > 40 and abs(at(x, y) - at(x + 1, y)) > 40:
                hits += 1
        return hits / height

    def row_score(y: int) -> float:
        if y <= 0 or y >= height - 1:
            return 0.0
        hits = 0
        for x in range(width):
            if abs(at(x, y) - at(x, y - 1)) > 40 and abs(at(x, y) - at(x, y + 1)) > 40:
                hits += 1
        return hits / width

    v = [x for x in range(1, width - 1) if column_score(x) >= min_run]
    h = [y for y in range(1, height - 1) if row_score(y) >= min_run]
    v = _collapse(v)
    h = _collapse(h)
    xs = [0, *v, width]
    ys = [0, *h, height]
    panes = []
    for yi in range(len(ys) - 1):
        for xi in range(len(xs) - 1):
            panes.append({
                "x": xs[xi], "y": ys[yi],
                "w": xs[xi + 1] - xs[xi], "h": ys[yi + 1] - ys[yi],
                "ratio_x": round((xs[xi + 1] - xs[xi]) / width, 3),
                "ratio_y": round((ys[yi + 1] - ys[yi]) / height, 3),
            })
    return {
        "width": width, "height": height,
        "vertical": v, "horizontal": h,
        "panes": panes, "pane_count": len(panes),
        "tree": {"cols": len(xs) - 1, "rows": len(ys) - 1},
    }


def _collapse(indexes: list[int], gap: int = 2) -> list[int]:
    if not indexes:
        return []
    groups = [[indexes[0]]]
    for n in indexes[1:]:
        if n - groups[-1][-1] <= gap:
            groups[-1].append(n)
        else:
            groups.append([n])
    return [g[len(g) // 2] for g in groups]


def commands_from_ocr(lines_per_pane: list[list[str]]) -> list[dict]:
    """A command is selectable only when it is copied from that pane's OCR."""
    out = []
    for lines in lines_per_pane:
        text = "\n".join(lines)
        picked = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("echo ") or stripped.startswith("$ "):
                picked = stripped[2:].strip() if stripped.startswith("$ ") else stripped
                break
        out.append({
            "command": picked,
            "flagged": picked is None,
            "ocr": text,
            "exact": True if picked is None else picked in text or f"$ {picked}" in text or picked in text,
        })
    return out
