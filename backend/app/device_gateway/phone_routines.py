"""Phone routine registry: per-device digest scheduling preferences.

Pure helpers (no I/O) so the scheduler and the API share one validator.
A routine config lives in the device endpoint_profile under "routines":

    {
        "enabled": bool,
        "digest_times": ["07:30", "21:00"],   # 0..4 entries, HH:MM local
        "quiet_hours_start": "22:00" | None,
        "quiet_hours_end": "07:30" | None,
        "timezone": "Asia/Kolkata",
        "last_digest_at": iso | None,          # written by the scheduler
        "updated_at": iso,
    }
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

DEFAULTS: dict = {
    "enabled": False,
    "digest_times": [],
    "quiet_hours_start": None,
    "quiet_hours_end": None,
    "timezone": "UTC",
    "last_digest_at": None,
    "updated_at": None,
}


def valid_time(value: str | None) -> bool:
    return bool(value and _TIME_RE.match(value.strip()))


def valid_timezone(value: str | None) -> bool:
    if not value:
        return False
    try:
        ZoneInfo(value.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def normalize(profile_value: dict | None) -> dict:
    """Return a validated routine config; invalid fields fall back safely."""
    raw = dict(profile_value or {})
    out = dict(DEFAULTS)
    out["enabled"] = bool(raw.get("enabled"))
    times = []
    for t in (raw.get("digest_times") or []):
        t = str(t).strip()
        if valid_time(t):
            times.append(t.zfill(5))
    out["digest_times"] = sorted(times)[:4]
    for key in ("quiet_hours_start", "quiet_hours_end"):
        value = str(raw.get(key) or "").strip() or None
        out[key] = value.zfill(5) if valid_time(value) else None
    tz = str(raw.get("timezone") or "UTC").strip()
    out["timezone"] = tz if valid_timezone(tz) else "UTC"
    last = raw.get("last_digest_at")
    out["last_digest_at"] = str(last) if last else None
    updated = raw.get("updated_at")
    out["updated_at"] = str(updated) if updated else None
    return out


def in_quiet_hours(now: datetime, cfg: dict) -> bool:
    """True when `now` (local to cfg timezone) falls inside quiet hours."""
    start = cfg.get("quiet_hours_start")
    end = cfg.get("quiet_hours_end")
    if not valid_time(start) or not valid_time(end):
        return False
    t = now.strftime("%H:%M")
    if start < end:
        return start <= t < end
    return t >= start or t < end  # overnight window


def due_times(now: datetime, cfg: dict) -> list[str]:
    """Digest times whose minute bucket is now and that have not fired since
    that same minute last fired (last_digest_at older than one minute)."""
    cfg = normalize(cfg)
    if not cfg["enabled"]:
        return []
    bucket = now.strftime("%H:%M")
    if bucket not in cfg["digest_times"]:
        return []
    if in_quiet_hours(now, cfg):
        return []
    last = cfg.get("last_digest_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(str(last))
            if now - last_dt < timedelta(minutes=1):
                return []
        except ValueError:
            pass
    return [bucket]
