"""Screenshot-as-reference pipeline (WP5): capture → OCR → infer → artifact.

Capture runs inside Evie.app's identity (bridge screenshot op); OCR rides
the configured vision provider (Apple Vision via evvision on this Mac);
pane inference uses a VLM pass when a media-capable keyed provider exists
and OCR-geometry clustering otherwise (flagged lower-confidence). Every
artifact is marked untrusted (R8) and persisted as a typed Event.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .screen_ref import ScreenArtifact, infer_panes


def screenshot_dir() -> Path:
    """Real macOS screenshot location (custom default or ~/Desktop)."""
    try:
        p = subprocess.run(["defaults", "read", "com.apple.screencapture", "location"],
                           capture_output=True, text=True, timeout=5)
        loc = (p.stdout or "").strip()
        if loc:
            return Path(loc).expanduser()
    except Exception:
        pass
    return Path.home() / "Desktop"


def resolve_last(*, conversation: list | None = None,
                 store: list | None = None) -> dict | None:
    """Conversation image first, then store, then the real screenshot dir."""
    from .screen_ref import newest_screenshot, resolve_last_screenshot

    found = resolve_last_screenshot(conversation, store=store)
    if found is not None:
        return found
    return newest_screenshot(screenshot_dir())


async def ocr_path(path: str | Path) -> dict[str, Any]:
    """Real OCR via the configured vision provider. Never raises."""
    from app.vision.providers import VisionProviderError, get_vision_provider

    try:
        data = Path(str(path)).read_bytes()
    except OSError as exc:
        return {"ok": False, "verified": False, "evidence": {},
                "error": f"unreadable: {exc}", "undo": None}
    try:
        provider = get_vision_provider()
        result = await provider.analyze(data=data, filename=Path(str(path)).name)
    except VisionProviderError as exc:
        return {"ok": False, "verified": False, "evidence": {},
                "error": f"ocr failed: {exc}", "undo": None}
    lines = [dict(item) for item in (result.lines or []) if isinstance(item, dict)]
    degraded = bool(result.degraded)
    # A degraded double is not a verified read (behavioral-harness
    # contract: ok and verified stay False), but its evidence dict is
    # still returned so consumers can speak honestly about degraded
    # output instead of dying.
    return {"ok": not degraded, "verified": not degraded,
            "evidence": {"text": result.ocr_text or "",
                         "lines": lines, "n": len(lines),
                         "provider": result.provider,
                         "degraded": degraded},
            "error": "ocr degraded" if degraded else None, "undo": None}


def normalize_words(lines: list[dict]) -> list[dict]:
    """evvision lines -> [{text,x,y,w,h,conf}] in top-left normalized coords."""
    words: list[dict] = []
    for line in lines or []:
        if not isinstance(line, dict):
            continue
        box = line.get("bounding_box") or line.get("box") or {}
        try:
            words.append({
                "text": str(line.get("text") or ""),
                "x": float(box.get("x", 0.0)),
                "y": float(box.get("y", 0.0)),
                "w": float(box.get("width", 0.0)),
                "h": float(box.get("height", 0.0)),
                "conf": float(line.get("confidence", 0.0)),
            })
        except (TypeError, ValueError):
            continue
    return [w for w in words if w["text"].strip()]


def infer_geometry(words: list[dict]) -> tuple[list[dict], float]:
    """Banded pane inference (heuristic; capped confidence)."""
    bands = infer_panes(words, gap=0.04)
    panes: list[dict] = []
    confs: list[float] = []
    for i, band in enumerate(bands):
        items = band.get("words", []) or []
        if not items:
            continue
        xs = [w.get("x", 0.0) for w in items]
        ys = [w.get("y", 0.0) for w in items]
        xe = [w.get("x", 0.0) + w.get("w", 0.0) for w in items]
        ye = [w.get("y", 0.0) + w.get("h", 0.0) for w in items]
        text = " ".join(str(w.get("text") or "") for w in items)
        confs.extend(float(w.get("conf", 0.0)) for w in items)
        panes.append({"pane": i, "x": min(xs), "y": min(ys),
                      "w": max(xe) - min(xs), "h": max(ye) - min(ys),
                      "text": text[:2000], "words": len(items)})
    confidence = (min(confs) if confs else 0.0) * 0.7
    return panes, round(min(confidence, 0.7), 3)


def _vlm_available() -> bool:
    try:
        from app.gateway.roles import require_text_provider, text_role_available
    except Exception:
        return False
    if not text_role_available():
        return False
    try:
        return bool(getattr(require_text_provider(), "supports_media", False))
    except Exception:
        return False


async def infer_vlm(image_bytes: bytes, words: list[dict]) -> tuple[list[dict] | None, float]:
    """Ask the keyed media-capable model for panes. None when unavailable."""
    if not _vlm_available():
        return None, 0.0
    try:
        from app.compliance.policy import remote_processing_allowed

        if not remote_processing_allowed("chat_egress"):
            return None, 0.0
    except Exception:
        return None, 0.0
    try:
        import base64 as _b64

        from app.contracts import ChatMessage, MediaPart
        from app.gateway.roles import require_text_provider

        provider = require_text_provider()
        hint = "\n".join(f"- {w['text']} @({w['x']:.2f},{w['y']:.2f})"
                         for w in words[:120])
        digest = hashlib.sha256(image_bytes).hexdigest()
        messages = [ChatMessage(
            role="user",
            content=("Divide this terminal screenshot into panes. Reply with JSON "
                     "only: {\"panes\": [{\"x\":0-1,\"y\":0-1,\"w\":0-1,\"h\":0-1,"
                     "\"text\":\"\"}]}. OCR hint lines:\n" + hint),
            media=[MediaPart(kind="image", content_type="image/png",
                             data_url="data:image/png;base64," + _b64.b64encode(
                                 image_bytes).decode(),
                             sha256=digest, size_bytes=len(image_bytes))],
        )]
        result = await provider.chat_with_tools(messages, [])
        match = re.search(r"\{.*\}", result.text or "", re.S)
        if not match:
            return None, 0.0
        payload = json.loads(match.group(0))
        panes = payload.get("panes") if isinstance(payload, dict) else None
        if not isinstance(panes, list) or not panes:
            return None, 0.0
        cleaned = []
        for i, pane in enumerate(panes[:12]):
            if not isinstance(pane, dict):
                continue
            cleaned.append({"pane": i,
                            "x": float(pane.get("x", 0.0)), "y": float(pane.get("y", 0.0)),
                            "w": float(pane.get("w", 0.0)), "h": float(pane.get("h", 0.0)),
                            "text": str(pane.get("text", ""))[:2000]})
        return (cleaned, 0.85) if cleaned else (None, 0.0)
    except Exception:
        return None, 0.0


def build_artifact(*, source: str, path: str, width: int = 0, height: int = 0,
                   lines: list[dict] | None = None, ocr_provider: str = "",
                   panes: list[dict] | None = None, confidence: float = 0.0,
                   method: str = "geometry") -> ScreenArtifact:
    digest = ""
    with contextlib.suppress(OSError):
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return ScreenArtifact({
        "id": f"shot-{uuid.uuid4().hex[:12]}",
        "source": source,
        "path": path,
        "source_hash": digest,
        "captured_at": time.time(),
        "width": width,
        "height": height,
        "ocr": {"provider": ocr_provider, "lines": lines or [], "n": len(lines or [])},
        "panes": panes or [],
        "confidence": confidence,
        "method": method,
        "untrusted": True,
    })


async def persist_artifact(session: AsyncSession, artifact: ScreenArtifact,
                           *, actor: str = "master") -> dict[str, Any]:
    from app.schemas import EventCreate
    from app.services.event_service import EventService

    service = EventService(session, actor=actor)
    event = await service.create(EventCreate(
        source="fabric",
        event_type="screen.artifact",
        text=f"screenshot artifact {artifact.get('id')} ({artifact.get('method')})",
        content={"artifact": dict(artifact)},
    ))
    await session.commit()
    from app.models import Event

    stored = await session.get(Event, event.id)
    if stored is None or stored.event_type != "screen.artifact":
        return {"ok": False, "event_id": None, "error": "artifact was not stored"}
    return {"ok": True, "event_id": str(stored.id)}


def _pixel_panes(image_bytes: bytes, words: list[dict] | None = None) -> tuple[list[dict], float, str] | None:
    """Use the pixel splitter when it finds more panes than OCR banding."""
    if not image_bytes:
        return None
    try:
        from .screen_layout import infer_splits

        tree = infer_splits(image_bytes)
    except Exception:
        return None
    if int(tree.get("pane_count") or 0) < 2:
        return None
    width = int(tree.get("width") or 0) or 1
    height = int(tree.get("height") or 0) or 1
    panes: list[dict[str, Any]] = []
    for index, pane in enumerate(tree.get("panes") or []):
        panes.append({
            "pane": index,
            "x": round(float(pane["x"]) / width, 3),
            "y": round(float(pane["y"]) / height, 3),
            "w": round(float(pane["w"]) / width, 3),
            "h": round(float(pane["h"]) / height, 3),
            "text": "",
        })
    for word in words or []:
        cx = float(word.get("x") or 0) + float(word.get("w") or 0) / 2
        cy = float(word.get("y") or 0) + float(word.get("h") or 0) / 2
        for pane in panes:
            px, py = float(pane["x"]), float(pane["y"])
            right = px + float(pane["w"])
            bottom = py + float(pane["h"])
            if px <= cx <= right and py <= cy <= bottom:
                pane["text"] = (str(pane["text"]) + " " + str(word.get("text") or "")).strip()
                break
    return panes, 0.45, "pixels"


async def analyze_image(path: str | Path, *, session: AsyncSession | None = None,
                        actor: str = "master") -> dict[str, Any]:
    """Full pipeline for one image file: OCR -> infer -> artifact (+Event)."""
    ocr = await ocr_path(path)
    if not ocr.get("ok") or not ocr.get("verified"):
        return ocr
    ev = ocr["evidence"]
    words = normalize_words(ev.get("lines", []))
    try:
        image_bytes = Path(str(path)).read_bytes()
    except OSError:
        image_bytes = b""
    panes, confidence = await infer_vlm(image_bytes, words)
    method = "vlm"
    if panes is None:
        panes, confidence = infer_geometry(words)
        method = "geometry"
    pixel = _pixel_panes(image_bytes, words)
    if pixel is not None and len(pixel[0]) > len(panes or []):
        panes, confidence, method = pixel
    artifact = build_artifact(
        source="screenshot", path=str(path),
        lines=ev.get("lines", []), ocr_provider=str(ev.get("provider") or ""),
        panes=panes, confidence=confidence, method=method)
    result: dict[str, Any] = {"ok": True, "verified": True,
                              "evidence": {"artifact": dict(artifact)},
                              "error": None, "undo": None}
    if session is not None:
        persisted = await persist_artifact(session, artifact, actor=actor)
        if persisted.get("ok") and persisted.get("event_id"):
            result["evidence"]["event_id"] = persisted["event_id"]
        else:
            result["evidence"]["persist_error"] = (
                persisted.get("error") or "artifact was not stored"
            )
    return result


def extract_table(words: list[dict], *, x_tol: float = 0.03) -> list[list[str]]:
    """Cluster OCR words into table rows/columns by geometry (no guessing).

    Rows group by vertical overlap; columns split where horizontal gaps
    exceed x_tol (normalized units). Empty cells stay empty strings.
    """
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w.get("y", 0.0), w.get("x", 0.0))):
        placed = False
        for row in rows:
            sample = row[0]
            top = min(w.get("y", 0.0) for w in row)
            bottom = max(w.get("y", 0.0) + w.get("h", 0.0) for w in row)
            mid = word.get("y", 0.0) + word.get("h", 0.0) / 2.0
            _ = sample
            if top - 0.01 <= mid <= bottom + 0.01:
                row.append(word)
                placed = True
                break
        if not placed:
            rows.append([word])
    bounds: list[tuple[float, float]] = []
    for row in rows:
        for word in sorted(row, key=lambda w: w.get("x", 0.0)):
            left, right = word.get("x", 0.0), word.get("x", 0.0) + word.get("w", 0.0)
            for i, (lo, hi) in enumerate(bounds):
                if left <= hi + x_tol and right >= lo - x_tol:
                    bounds[i] = (min(lo, left), max(hi, right))
                    break
            else:
                bounds.append((left, right))
    bounds.sort()
    table: list[list[str]] = []
    for row in rows:
        cells = ["" for _ in bounds]
        for word in row:
            center = word.get("x", 0.0) + word.get("w", 0.0) / 2.0
            best, best_dist = 0, float("inf")
            for i, (lo, hi) in enumerate(bounds):
                dist = 0.0 if lo <= center <= hi else min(abs(center - lo), abs(center - hi))
                if dist < best_dist:
                    best, best_dist = i, dist
            cells[best] = (cells[best] + " " + str(word.get("text") or "")).strip()
        table.append(cells)
    return table


_ERROR_RES = (
    re.compile(r"(?i)\b(error|failed|failure|exception|traceback|denied|refused|timeout)\b"),
    re.compile(r"(?i)^(E\d+|ERR|FATAL|CRITICAL)[:\s]"),
)


def extract_errors(words: list[dict], *, window: int = 2) -> list[dict]:
    """Pull error-indicating OCR lines with context (extraction, not diagnosis)."""
    lines: dict[float, list[dict]] = {}
    for word in words:
        key = round(float(word.get("y", 0.0)), 3)
        lines.setdefault(key, []).append(word)
    ordered = [" ".join(w.get("text", "") for w in sorted(group, key=lambda x: x.get("x", 0.0)))
               for _, group in sorted(lines.items())]
    hits: list[dict] = []
    for i, line in enumerate(ordered):
        if any(rx.search(line or "") for rx in _ERROR_RES):
            lo, hi = max(0, i - window), min(len(ordered), i + window + 1)
            hits.append({"line": i, "text": line[:500],
                         "context": " | ".join(ordered[lo:hi])[:1500]})
    return hits
