"""Small, dependency-free raster access for layout inference.

No Pillow/numpy on the required path. On macOS `sips` converts any image the
owner can screenshot (PNG/JPEG/HEIC/TIFF) to a downscaled BMP in ~0.2 s; when
`sips` is absent a pure-Python PNG decoder handles 8-bit RGB/RGBA PNGs.
Pillow is used when it happens to be installed.
"""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Raster:
    """Top-down RGB rows. `alpha` rows are optional (0 = transparent)."""
    w: int
    h: int
    rows: list[bytes]  # each len == w*3
    alpha: list[bytes] | None = None
    scale: float = 1.0  # original width / w

    def px(self, x: int, y: int) -> tuple[int, int, int]:
        i = x * 3
        r = self.rows[y]
        return r[i], r[i + 1], r[i + 2]

    def opaque(self, x: int, y: int) -> bool:
        return self.alpha is None or self.alpha[y][x] > 24

    @staticmethod
    def from_fn(w: int, h: int, fn: Callable[[int, int], tuple[int, int, int]]) -> Raster:
        rows = []
        for y in range(h):
            buf = bytearray(w * 3)
            for x in range(w):
                buf[x * 3:x * 3 + 3] = bytes(fn(x, y))
            rows.append(bytes(buf))
        return Raster(w, h, rows)

    def to_png(self) -> bytes:
        raw = b"".join(b"\x00" + r for r in self.rows)

        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (struct.pack(">I", len(payload)) + kind + payload
                    + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# --------------------------------------------------------------- loading ----

def load_raster(src: str | Path | bytes, *, max_dim: int = 1000) -> Raster:
    """Load an image, downscaled so its longest side is <= max_dim."""
    tmp_in: str | None = None
    try:
        if isinstance(src, bytes | bytearray):
            fd, tmp_in = tempfile.mkstemp(suffix=".img")
            with os.fdopen(fd, "wb") as fh:
                fh.write(bytes(src))
            path = tmp_in
        else:
            path = str(src)
        for loader in (_load_pillow, _load_sips, _load_png_python):
            try:
                out = loader(path, max_dim)
            except (OSError, ValueError, subprocess.SubprocessError, ImportError):
                out = None
            if out is not None:
                return out
        raise ValueError("unreadable image")
    finally:
        if tmp_in:
            try:
                os.unlink(tmp_in)
            except OSError:
                pass


def _load_pillow(path: str, max_dim: int) -> Raster | None:
    from PIL import Image  # type: ignore[import-not-found]

    im = Image.open(path)
    ow = im.width
    im = im.convert("RGBA")
    if max(im.size) > max_dim:
        f = max_dim / max(im.size)
        im = im.resize((max(1, int(im.width * f)), max(1, int(im.height * f))))
    w, h = im.size
    data = im.tobytes()
    rows, alpha = [], []
    for y in range(h):
        row = data[y * w * 4:(y + 1) * w * 4]
        rows.append(bytes(row[i] for i in range(len(row)) if i % 4 != 3))
        alpha.append(bytes(row[3::4]))
    return Raster(w, h, rows, alpha, scale=ow / w)


def _load_sips(path: str, max_dim: int) -> Raster | None:
    if not Path("/usr/bin/sips").exists():
        return None
    ow, oh = _image_size(path)
    fd, out = tempfile.mkstemp(suffix=".bmp")
    os.close(fd)
    try:
        cmd = ["/usr/bin/sips", "-s", "format", "bmp"]
        if max(ow, oh) > max_dim:  # never upscale
            cmd += ["-Z", str(max_dim)]
        p = subprocess.run([*cmd, path, "--out", out], capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            return None
        r = _parse_bmp(Path(out).read_bytes())
        if r is not None and ow:
            r.scale = ow / r.w
        return r
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def _image_size(path: str) -> tuple[int, int]:
    p = subprocess.run(["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
                       capture_output=True, text=True, timeout=15)
    dims = {"pixelWidth": 0, "pixelHeight": 0}
    for line in (p.stdout or "").splitlines():
        for key in dims:
            if key in line:
                try:
                    dims[key] = int(line.split(":")[1])
                except ValueError:
                    pass
    return dims["pixelWidth"], dims["pixelHeight"]


def _parse_bmp(data: bytes) -> Raster | None:
    if data[:2] != b"BM":
        return None
    off = struct.unpack_from("<I", data, 10)[0]
    w, h = struct.unpack_from("<ii", data, 18)
    bpp = struct.unpack_from("<H", data, 28)[0]
    top_down = h < 0
    h = abs(h)
    if bpp not in (24, 32) or w <= 0 or h <= 0:
        return None
    bytes_pp = bpp // 8
    stride = (w * bytes_pp + 3) // 4 * 4
    rows: list[bytes] = []
    alpha: list[bytes] | None = [] if bpp == 32 else None
    for y in range(h):
        sy = y if top_down else h - 1 - y
        line = data[off + sy * stride: off + sy * stride + w * bytes_pp]
        if len(line) < w * bytes_pp:
            return None
        rgb = bytearray(w * 3)
        rgb[0::3] = line[2::bytes_pp]
        rgb[1::3] = line[1::bytes_pp]
        rgb[2::3] = line[0::bytes_pp]
        rows.append(bytes(rgb))
        if alpha is not None:
            alpha.append(bytes(line[3::bytes_pp]))
    return Raster(w, h, rows, alpha)


def _load_png_python(path: str, max_dim: int) -> Raster | None:
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos, width, height, color, raw = 8, 0, 0, 2, b""
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", chunk[:10])
            if depth != 8 or color not in (2, 6):
                return None
        elif kind == b"IDAT":
            raw += chunk
        elif kind == b"IEND":
            break
    bpp = 4 if color == 6 else 3
    decoded = zlib.decompress(raw)
    stride = width * bpp
    step = max(1, -(-max(width, height) // max_dim))
    prev = bytearray(stride)
    rows, alpha = [], ([] if bpp == 4 else None)
    i = 0
    for y in range(height):
        ft = decoded[i]
        cur = bytearray(decoded[i + 1:i + 1 + stride])
        i += 1 + stride
        _unfilter_row(ft, cur, prev, bpp)
        prev = cur
        if y % step:
            continue
        sel = cur if step == 1 else b"".join(cur[x * bpp:(x + 1) * bpp] for x in range(0, width, step))
        n = len(sel) // bpp
        rgb = bytearray(n * 3)
        rgb[0::3] = sel[0::bpp]
        rgb[1::3] = sel[1::bpp]
        rgb[2::3] = sel[2::bpp]
        rows.append(bytes(rgb))
        if alpha is not None:
            alpha.append(bytes(sel[3::bpp]))
    return Raster(len(rows[0]) // 3, len(rows), rows, alpha, scale=float(step))


def _unfilter_row(ft: int, cur: bytearray, prev: bytearray, bpp: int) -> None:
    n = len(cur)
    if ft == 0:
        return
    if ft == 1:
        for x in range(bpp, n):
            cur[x] = (cur[x] + cur[x - bpp]) & 255
    elif ft == 2:
        for x in range(n):
            cur[x] = (cur[x] + prev[x]) & 255
    elif ft == 3:
        for x in range(n):
            left = cur[x - bpp] if x >= bpp else 0
            cur[x] = (cur[x] + ((left + prev[x]) >> 1)) & 255
    elif ft == 4:
        for x in range(n):
            a = cur[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            p = a + b - c
            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
            pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            cur[x] = (cur[x] + pred) & 255
    else:
        raise ValueError("unsupported png filter")
