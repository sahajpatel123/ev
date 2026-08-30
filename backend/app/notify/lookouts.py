"""In-process registry of HUD windows EVIE has asked the suit to show.

The native app is the display truth. This registry is what intelligence
remembers she *attempted* to open, so she can refresh or dismiss a lookout
without inventing a second product surface.
"""

from __future__ import annotations

from threading import Lock
from typing import Any

from app.utils.text import utcnow

_LOCK = Lock()
_WINDOWS: dict[str, dict[str, Any]] = {}


def upsert(window: dict[str, Any], *, opened: bool, via: str | None = None) -> dict[str, Any]:
    ident = str(window.get("id") or "").strip()
    if not ident:
        return window
    record = {
        **window,
        "opened": bool(opened),
        "via": via,
        "updated_at": utcnow().isoformat(),
    }
    with _LOCK:
        existing = _WINDOWS.get(ident) or {}
        if "created_at" in existing:
            record["created_at"] = existing["created_at"]
        else:
            record["created_at"] = record["updated_at"]
        _WINDOWS[ident] = record
        return dict(record)


def get(window_id: str) -> dict[str, Any] | None:
    with _LOCK:
        row = _WINDOWS.get(window_id)
        return dict(row) if row else None


def list_windows() -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(row) for row in _WINDOWS.values()]


def dismiss(window_id: str | None = None) -> list[str]:
    with _LOCK:
        if window_id:
            return [window_id] if _WINDOWS.pop(window_id, None) else []
        ids = list(_WINDOWS.keys())
        _WINDOWS.clear()
        return ids


def reset() -> None:
    with _LOCK:
        _WINDOWS.clear()
