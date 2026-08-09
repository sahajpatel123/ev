"""Coarse location/presence collector (never exact coordinates).

Reads a user-managed coarse place + presence from ``EV_LOCATION_PLACE`` /
``EV_LOCATION_PRESENCE`` or ``~/.ev/location.json``.  Exact GPS fixes must be
rounded to a coarse label by the OS layer before this collector sees them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _local_location_file() -> Path:
    return Path(os.environ.get("EV_LOCATION_FILE", str(Path.home() / ".ev" / "location.json")))


def location_context() -> dict | None:
    place = os.environ.get("EV_LOCATION_PLACE")
    presence = os.environ.get("EV_LOCATION_PRESENCE")
    if place is None and presence is None:
        try:
            data = json.loads(_local_location_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        place = data.get("place") or data.get("coarse_place")
        presence = data.get("presence")
    if place is None and presence is None:
        return None
    payload: dict = {}
    if place:
        payload["place"] = str(place)[:80]
    if presence:
        payload["presence"] = str(presence)[:32]
    return payload or None
