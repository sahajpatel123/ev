"""Coarse location/presence collector (never exact coordinates).

Three sources, most explicit first:

1. ``EV_LOCATION_PLACE`` / ``EV_LOCATION_PRESENCE`` env hints;
2. a user-managed ``~/.ev/location.json`` (or ``EV_LOCATION_FILE``), which the
   Swift helper's ``--monitor`` mode keeps updated via significant-location
   changes;
3. a one-shot CoreLocation probe (``EV_LOCATION_NATIVE=1``) that classifies
   the last fix against user-defined named places (``~/.ev/location-places.json``)
   into "home" / "work" / "elsewhere" -- coordinates are never emitted.

Denied/restricted Location TCC permission is surfaced once per process as a
``permission`` field so the human sees exactly which macOS permission is
missing, then suppressed until the status changes (no permission spam).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from clients.collectors import _native

# Last non-authorized TCC status we already surfaced this process.
_last_permission: str | None = None


def _local_location_file() -> Path:
    return Path(os.environ.get("EV_LOCATION_FILE", str(Path.home() / ".ev" / "location.json")))


def _native_enabled() -> bool:
    return os.environ.get("EV_LOCATION_NATIVE", "").lower() in {"1", "true", "yes"}


def _native_location() -> dict | None:
    if sys.platform != "darwin":
        return None
    return _native.run_helper(["--location", "--no-prompt"], timeout=10)


def _permission_payload(auth: str) -> dict:
    return {"presence": "unknown", "permission": auth}


def location_context() -> dict | None:
    """Return a coarse place/presence payload, or ``None`` when unknown."""

    global _last_permission

    place = os.environ.get("EV_LOCATION_PLACE")
    presence = os.environ.get("EV_LOCATION_PRESENCE")
    if place is None and presence is None:
        try:
            data = json.loads(_local_location_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        place = data.get("place") or data.get("coarse_place")
        presence = data.get("presence")
    if place or presence:
        payload: dict = {}
        if place:
            payload["place"] = str(place)[:80]
        if presence:
            payload["presence"] = str(presence)[:32]
        return payload

    if not _native_enabled():
        return None
    native = _native_location()
    if not native:
        return None

    native_place = str(native.get("place") or "").strip()
    native_presence = str(native.get("presence") or "").strip()
    if native_presence and native_presence != "unknown":
        payload = {}
        if native_place:
            payload["place"] = native_place[:80]
        payload["presence"] = native_presence[:32]
        return payload

    auth = str(native.get("authorization_status") or "").strip()
    if auth in {"denied", "restricted", "notDetermined"} and auth != _last_permission:
        _last_permission = auth
        return _permission_payload(auth)
    return None


def reset_permission_state() -> None:
    """Test/process hook: allow a permission status to be surfaced again."""

    global _last_permission
    _last_permission = None
