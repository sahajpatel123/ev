"""iPhone routine registry — pure validator + gateway routes."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from app.device_gateway.phone_routines import due_times, in_quiet_hours, normalize, valid_time, valid_timezone


def test_time_and_timezone_validators() -> None:
    assert valid_time("07:30")
    assert valid_time("23:59")
    assert valid_time("9:05")
    assert not valid_time("25:00")
    assert not valid_time("7:60")
    assert not valid_time(None)
    assert valid_timezone("Asia/Kolkata")
    assert not valid_timezone("Not/AZone")


def test_normalize_falls_back_safely() -> None:
    cfg = normalize(
        {
            "enabled": True,
            "digest_times": ["07:30", "bad", "21:00", "06:00", "12:00", "99:99"],
            "quiet_hours_start": "22:00",
            "quiet_hours_end": "7:30",
            "timezone": "Asia/Kolkata",
        }
    )
    assert cfg["enabled"] is True
    assert cfg["digest_times"] == ["06:00", "07:30", "12:00", "21:00"]
    assert cfg["quiet_hours_start"] == "22:00"
    assert cfg["quiet_hours_end"] == "07:30"
    assert cfg["timezone"] == "Asia/Kolkata"

    empty = normalize({})
    assert empty["enabled"] is False
    assert empty["digest_times"] == []
    assert empty["timezone"] == "UTC"


def test_quiet_hours_and_due_logic() -> None:
    tz = ZoneInfo("Asia/Kolkata")
    cfg = normalize(
        {
            "enabled": True,
            "digest_times": ["07:30", "21:00"],
            "quiet_hours_start": "22:00",
            "quiet_hours_end": "07:30",
            "timezone": "Asia/Kolkata",
        }
    )
    # 07:00 is inside overnight quiet hours (22:00 -> 07:30).
    early = datetime(2026, 9, 8, 7, 0, tzinfo=tz)
    assert in_quiet_hours(early, cfg) is True
    assert due_times(early, cfg) == []
    # 07:31 is clear and exactly on the 07:30 bucket? No — bucket is 07:31.
    clear = datetime(2026, 9, 8, 8, 0, tzinfo=tz)
    assert in_quiet_hours(clear, cfg) is False
    # Bucket must match a digest time.
    assert due_times(clear, cfg) == []
    hit = datetime(2026, 9, 8, 21, 0, tzinfo=tz)
    assert due_times(hit, cfg) == ["21:00"]
    # Fired within the last minute -> no repeat.
    cfg["last_digest_at"] = (hit - timedelta(seconds=30)).isoformat()
    assert due_times(hit, cfg) == []
    # Disabled -> never due.
    cfg["enabled"] = False
    assert due_times(hit, cfg) == []
    # Non-bucket minute never fires even when enabled.
    cfg["enabled"] = True
    off = datetime(2026, 9, 8, 21, 1, tzinfo=tz)
    assert due_times(off, cfg) == []


async def test_routines_roundtrip_and_validation(client: AsyncClient, gateway_phone) -> None:
    _body, phone = gateway_phone
    res = await phone.get("/v1/device-gateway/routines")
    assert res.status_code == 200
    assert res.json()["routines"]["enabled"] is False

    bad = await phone.put(
        "/v1/device-gateway/routines",
        json={"enabled": True, "digest_times": []},
    )
    assert bad.status_code == 422

    ok = await phone.put(
        "/v1/device-gateway/routines",
        json={
            "enabled": True,
            "digest_times": ["21:00", "07:30"],
            "quiet_hours_start": "22:00",
            "quiet_hours_end": "07:30",
            "timezone": "Asia/Kolkata",
        },
    )
    assert ok.status_code == 200, ok.text
    cfg = ok.json()["routines"]
    assert cfg["digest_times"] == ["07:30", "21:00"]
    assert cfg["timezone"] == "Asia/Kolkata"

    again = (await phone.get("/v1/device-gateway/routines")).json()["routines"]
    assert again["digest_times"] == ["07:30", "21:00"]
