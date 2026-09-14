"""Geofence heading-out detection for the phone — strictly consented.

The owner leaves the house; Evie can catch them at the door with the one
thing they'll need (umbrella, keycard, "you're heading out — the 4pm is in
Santana Row"). Design laws:

- OPT-IN ONLY: nothing runs until the device posts explicit consent. The
  PWA asks once, the flag lives in the device's endpoint_profile, and
  clearing it stops everything.
- HOME ANCHOR: the center is the SAME home coords the weather lane uses
  (Home Station settings) — never the phone's guess.
- FOREGROUND ONLY by construction: the PWA's geolocation watcher only runs
  while the page is visible; iOS PWA background geofencing does not exist
  and is not faked.
- ONE NUDGE PER TRANSITION: profile state home->out fires once; no spam.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from app.search.live import home_coords
from app.utils.text import utcnow

HOME_RADIUS_METERS = 500.0


def heading_out_consent(device: Any) -> dict[str, Any]:
    profile = getattr(device, "endpoint_profile", None)
    raw = profile.get("heading_out") if isinstance(profile, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {
        "consent": bool(raw.get("consent")),
        "state": str(raw.get("state") or "unknown"),
        "radius_meters": float(raw.get("radius_meters") or HOME_RADIUS_METERS),
    }


def set_heading_out_consent(device: Any, *, consent: bool, radius_meters: float | None = None) -> dict[str, Any]:
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    previous = heading_out_consent(device)
    profile["heading_out"] = {
        "consent": bool(consent),
        "state": previous.get("state") if consent else "unknown",
        "radius_meters": max(50.0, min(5000.0, float(radius_meters or previous.get("radius_meters") or HOME_RADIUS_METERS))),
        "updated_at": utcnow().isoformat(),
    }
    device.endpoint_profile = profile
    return profile["heading_out"]


def _distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def evaluate_heading_out(
    device: Any,
    *,
    lat: float,
    lng: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One consented position sample -> transition decision (no side effects).

    Returns {"zone": "home"|"out"|"unknown", "transition": str | None,
    "distance_meters": float | None}. A transition fires at most once per
    zone change; the caller decides whether to nudge.
    """

    consent = heading_out_consent(device)
    if not consent["consent"]:
        return {"zone": "unknown", "transition": None, "distance_meters": None, "reason": "no_consent"}
    home = home_coords()
    if home is None:
        return {"zone": "unknown", "transition": None, "distance_meters": None, "reason": "no_home_anchor"}
    distance = _distance_meters(float(lat), float(lng), home[0], home[1])
    zone = "home" if distance <= consent["radius_meters"] else "out"
    previous = consent["state"]
    transition = None
    if previous in ("home", "unknown") and zone == "out":
        transition = "heading_out"
    elif previous == "out" and zone == "home":
        transition = "back_home"
    if transition is not None:
        profile = dict(getattr(device, "endpoint_profile", None) or {})
        profile["heading_out"] = {**consent, "state": zone, "updated_at": utcnow().isoformat()}
        device.endpoint_profile = profile
    return {"zone": zone, "transition": transition, "distance_meters": round(distance), "reason": None}
