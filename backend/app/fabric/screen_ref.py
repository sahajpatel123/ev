"""Screen lane: capture_region with fake-able screen; resolve_last_screenshot; pane inference."""
from __future__ import annotations


class ScreenArtifact(dict):
    pass

def capture_region(region: dict, *, screen=None) -> dict:
    redact = bool(region.get("redact"))
    pixels = None
    synthetic = screen is None
    if screen is not None:
        try:
            out = screen.capture()
            pixels = out.get("pixels", b"") if isinstance(out, dict) else b""
        except Exception as e:
            return {"ok": False, "verified": False, "evidence": {}, "error": str(e), "undo": None}
    else:
        pixels = b"\x00" * 8
    if redact:
        pixels = b"\x00" * len(pixels or b"\x00" * 8)
    art: dict = {"region": region, "pixels": pixels, "untrusted": True}
    if synthetic:
        art["synthetic"] = True
    # No screen backend means placeholder bytes, never a verified capture —
    # callers must not present this as real pixels.
    return {"ok": True, "verified": not synthetic,
            "evidence": {"artifact": art},
            "error": None if not synthetic else "no screen backend; synthetic pixels",
            "undo": None}

def resolve_last_screenshot(conversation: list | None = None, *, store: list | None = None) -> dict | None:
    """Prefer conversation images, fall back to store."""
    if conversation:
        for msg in reversed(conversation):
            imgs = msg.get("images") if isinstance(msg, dict) else None
            if imgs:
                return {"source": "conversation", "image": imgs[-1], "untrusted": True}
    if store:
        return {"source": "store", "image": store[-1], "untrusted": True}
    return None

def infer_panes(ocr: list[dict], gap: float = 40) -> list[dict]:
    """OCR-geometry pane inference: cluster words into rough horizontal bands.

    `gap` is the band threshold in the input's own units (pixels by
    default; pass ~0.04 for normalized 0-1 coordinates).
    """
    rows = sorted(ocr, key=lambda w: w.get("y", 0))
    panes, cur, last_y = [], [], None
    for w in rows:
        y = w.get("y", 0)
        if last_y is None or abs(y - last_y) < gap:
            cur.append(w)
        else:
            panes.append({"words": cur})
            cur = [w]
        last_y = y
    if cur:
        panes.append({"words": cur})
    return panes

def newest_screenshot(directory) -> dict | None:
    """Newest macOS-screenshot-named image in directory, or None.

    Matches Apple's `Screenshot YYYY-MM-DD at H.MM.SS ·AM.png` naming (prefix
    match on 'Screenshot' + image suffix) and picks max mtime.
    """
    from pathlib import Path
    d = Path(str(directory))
    try:
        cands = [p for p in d.iterdir()
                 if p.is_file() and p.name.startswith("Screenshot")
                 and p.suffix.lower() in (".png", ".jpg", ".jpeg")]
    except OSError:
        return None
    if not cands:
        return None
    best = max(cands, key=lambda p: p.stat().st_mtime)
    import hashlib
    digest = hashlib.sha256(best.read_bytes()).hexdigest()
    return {"source": "screenshot_dir", "path": str(best), "source_hash": digest, "untrusted": True}


def plan(goal: str) -> dict:
    return {"ok": True, "verified": True, "evidence": {"plan": goal}, "error": None, "undo": None}

def confirm(plan_: dict) -> dict:
    plan_ = dict(plan_)
    plan_["confirmed"] = True
    return plan_

def execute(plan_: dict, *, screen=None) -> dict:
    if not plan_.get("confirmed"):
        return {"ok": False, "verified": False, "evidence": {"needs_confirm": True}, "error": "confirm required", "undo": None}
    return capture_region({"x": 0, "y": 0, "w": 4, "h": 4}, screen=screen)

def verify(result: dict) -> dict:
    return {"ok": bool(result.get("ok")), "verified": True, "evidence": result, "error": None, "undo": None}
