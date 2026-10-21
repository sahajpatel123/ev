"""Deterministic split-tree from a screenshot. No model, no new dependencies.

Dividers are long straight runs where a column or row is much darker or much
lighter than its neighbors. Command text is not guessed: callers pass OCR
lines per pane, and any command that is not copied from those lines is flagged.
"""
from __future__ import annotations

import struct
import zlib
from collections.abc import Callable


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(decoded: bytes, width: int, height: int, bpp: int) -> list[bytes]:
    stride = width * bpp
    rows: list[bytes] = []
    prev = bytearray(stride)
    i = 0
    for _y in range(height):
        if i >= len(decoded):
            raise ValueError("truncated png")
        filt = decoded[i]
        i += 1
        raw = bytearray(decoded[i:i + stride])
        i += stride
        if len(raw) < stride:
            raise ValueError("truncated png")
        if filt == 1:
            for x in range(stride):
                left = raw[x - bpp] if x >= bpp else 0
                raw[x] = (raw[x] + left) & 255
        elif filt == 2:
            for x in range(stride):
                raw[x] = (raw[x] + prev[x]) & 255
        elif filt == 3:
            for x in range(stride):
                left = raw[x - bpp] if x >= bpp else 0
                raw[x] = (raw[x] + ((left + prev[x]) // 2)) & 255
        elif filt == 4:
            for x in range(stride):
                left = raw[x - bpp] if x >= bpp else 0
                up = prev[x]
                up_left = prev[x - bpp] if x >= bpp else 0
                raw[x] = (raw[x] + _paeth(left, up, up_left)) & 255
        elif filt != 0:
            raise ValueError("unsupported png filter")
        rows.append(bytes(raw))
        prev = raw
    return rows


def _read_png(data: bytes) -> tuple[int, int, list[tuple[int, int, int]]]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    pos = 8
    width = height = 0
    color = 2
    raw = b""
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, bit, color = struct.unpack(">IIBB", chunk[:10])
            if bit != 8 or color not in {2, 6}:
                raise ValueError("only 8-bit rgb or rgba png")
        elif kind == b"IDAT":
            raw += chunk
        elif kind == b"IEND":
            break
    bpp = 4 if color == 6 else 3
    rows = _unfilter(zlib.decompress(raw), width, height, bpp)
    pixels: list[tuple[int, int, int]] = []
    for row in rows:
        for x in range(width):
            i = x * bpp
            pixels.append((row[i], row[i + 1], row[i + 2]))
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
