"""Privacy-first image helpers: metadata stripping, decode, resize.

EXIF/GPS stripping is pure Python (works without Pillow): JPEG APP1/APP2/APP13
segments and the PNG ``eXIf`` chunk are removed so GPS and other location
metadata cannot reach storage. Pillow is used lazily when available for
decode/resize; its absence degrades those helpers to ``None`` instead of
raising.
"""

from __future__ import annotations

import io
import struct

_JPEG_APP_METADATA = {0xE1, 0xE2, 0xED}  # APP1 (EXIF), APP2 (ICC), APP13 (IPTC/Photoshop)


def strip_exif_gps(data: bytes) -> bytes:
    """Remove EXIF/metadata segments from JPEG/PNG bytes.

    Returns the cleaned bytes. Non-JPEG/PNG or malformed input is returned
    unchanged (we never corrupt user data).
    """

    if data.startswith(b"\xff\xd8"):
        return _strip_jpeg_metadata(data)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _strip_png_metadata(data)
    return data


def _strip_jpeg_metadata(data: bytes) -> bytes:
    i = 2
    out = bytearray(b"\xff\xd8")
    while i + 1 < len(data):
        if data[i] != 0xFF:
            return data  # malformed marker stream; leave untouched
        marker = data[i + 1]
        if marker == 0xD8:  # SOI (should not repeat)
            i += 2
            continue
        if marker == 0xDA:  # SOS: copy the rest verbatim and stop parsing
            out += data[i:]
            return bytes(out)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # standalone markers
            out += data[i : i + 2]
            i += 2
            continue
        if marker == 0xD9:  # EOI
            out += data[i : i + 2]
            return bytes(out)
        if i + 4 > len(data):
            return data
        segment_len = int.from_bytes(data[i + 2 : i + 4], "big")
        if segment_len < 2 or i + 2 + segment_len > len(data):
            return data
        if marker in _JPEG_APP_METADATA:
            i += 2 + segment_len
            continue
        out += data[i : i + 2 + segment_len]
        i += 2 + segment_len
    return bytes(out)


def _strip_png_metadata(data: bytes) -> bytes:
    if len(data) < 8:
        return data
    out = bytearray(data[:8])
    i = 8
    while i + 12 <= len(data):
        (length,) = struct.unpack(">I", data[i : i + 4])
        chunk_type = data[i + 4 : i + 8]
        start = i
        end = i + 12 + length
        if end > len(data):
            return bytes(out) + data[i:]
        if chunk_type == b"eXIf":
            i = end
            continue
        out += data[start:end]
        i = end
    return bytes(out)


def _pil() -> tuple | None:
    try:
        from PIL import Image, ImageOps

        return Image, ImageOps
    except Exception:  # noqa: BLE001 - optional dependency
        return None


def decode_image(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) when Pillow can decode; None otherwise."""

    pil = _pil()
    if pil is None:
        return None
    try:
        with pil[0].open(io.BytesIO(data)) as image:
            return image.size
    except Exception:  # noqa: BLE001 - optional decode
        return None


def resize_image(data: bytes, max_dimension: int = 1280) -> bytes | None:
    """Downscale an image (EXIF-transposed) with Pillow; None when unavailable."""

    pil = _pil()
    if pil is None:
        return None
    try:
        with pil[0].open(io.BytesIO(data)) as image:
            image = pil[1].exif_transpose(image)
            image.thumbnail((max_dimension, max_dimension))
            fmt = image.format or "PNG"
            buffer = io.BytesIO()
            image.save(buffer, format=fmt)
            return buffer.getvalue()
    except Exception:  # noqa: BLE001 - optional resize
        return None
