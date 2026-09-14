"""Proactive nudges to the phone, with quiet hours (cycle 50).

The Mac speaks nudges through live sockets; the phone had no proactive
delivery at all. Nudges now flow: nudge → quiet-hours gate → inbox row →
Web Push (when configured). Quiet hours protect the owner's nights; timer
alarms bypass them (an alarm you asked for is not an intrusion).

Quiet hours are interpreted in the Home Station's local time — the same
clock that produced the owner's timers. Single-owner local deployment law:
the server's timezone IS the owner's timezone.
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any

from app.models import Device

logger = logging.getLogger(__name__)

DEFAULT_QUIET_START = "22:00"
DEFAULT_QUIET_END = "07:00"


def nudge_prefs(device: Device) -> dict[str, Any]:
    profile = getattr(device, "endpoint_profile", None)
    raw = profile.get("nudge_prefs") if isinstance(profile, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "quiet_start": str(raw.get("quiet_start") or DEFAULT_QUIET_START),
        "quiet_end": str(raw.get("quiet_end") or DEFAULT_QUIET_END),
    }


def store_nudge_prefs(device: Device, *, enabled: bool, quiet_start: str, quiet_end: str) -> dict[str, Any]:
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    prefs = {
        "enabled": bool(enabled),
        "quiet_start": _parse_hhmm(quiet_start, DEFAULT_QUIET_START),
        "quiet_end": _parse_hhmm(quiet_end, DEFAULT_QUIET_END),
        "updated_at": __import__("app.utils.text", fromlist=["utcnow"]).utcnow().isoformat(),
    }
    profile["nudge_prefs"] = prefs
    device.endpoint_profile = profile
    return prefs


def _parse_hhmm(value: str, fallback: str) -> str:
    text = str(value or "").strip()
    try:
        hh, mm = text.split(":")
        assert 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
        return f"{int(hh):02d}:{int(mm):02d}"
    except Exception:
        return fallback


def _hhmm_to_time(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def in_quiet_hours(prefs: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or datetime.now().astimezone()
    start = _hhmm_to_time(prefs.get("quiet_start") or DEFAULT_QUIET_START)
    end = _hhmm_to_time(prefs.get("quiet_end") or DEFAULT_QUIET_END)
    current = now.time()
    if start <= end:
        return start <= current < end
    # Wrap past midnight: quiet from e.g. 22:00 through 07:00.
    return current >= start or current < end


async def send_nudge(
    session: Any,
    device: Device,
    *,
    kind: str,
    title: str,
    body: str,
    payload: dict[str, Any] | None = None,
    bypass_quiet: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    prefs = nudge_prefs(device)
    if not prefs["enabled"]:
        return {"status": "disabled", "item": None}
    if not bypass_quiet and in_quiet_hours(prefs, now=now):
        return {"status": "quiet", "item": None}
    battery = getattr(device, "battery_percent", None)
    if not bypass_quiet and battery is not None and float(battery) <= 15.0:
        # Cycle 52 — a dying phone keeps its silence except for alarms.
        return {"status": "low_battery", "item": None}
    from app.everywhere.inbox import push_inbox

    item = await push_inbox(
        session,
        device_id=device.id,
        kind=(kind or "notice")[:64],
        title=title,
        body=body,
        payload=payload or {},
    )
    return {"status": "sent", "item": item}
