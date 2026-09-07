"""iPhone routine registry — pure validator + gateway routes."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from sqlalchemy import select

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


async def test_digest_tick_delivers_once_and_honors_state(
    client: AsyncClient, gateway_phone, owner_phone, db_session
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.device_gateway.digest import phone_digest_tick
    from app.device_gateway.phone_routines import normalize

    _sbody, sandbox = gateway_phone
    body, owner = owner_phone
    tz = "Asia/Kolkata"
    # Owner phone: enable a 21:00 digest.
    put = await owner.put(
        "/v1/device-gateway/routines",
        json={"enabled": True, "digest_times": ["21:00"], "timezone": tz},
    )
    assert put.status_code == 200
    # Sandbox phone: enable the same digest; must NOT receive it.
    sput = await sandbox.put(
        "/v1/device-gateway/routines",
        json={"enabled": True, "digest_times": ["21:00"], "timezone": tz},
    )
    assert sput.status_code == 200

    now = datetime(2026, 9, 8, 15, 30, tzinfo=UTC)  # 21:00 in Asia/Kolkata
    delivered = await phone_digest_tick(db_session, now=now)
    assert len(delivered) == 1, delivered  # only the owner phone

    from app.models import DeviceInboxItem

    items = (
        (
            await db_session.execute(
                select(DeviceInboxItem).order_by(DeviceInboxItem.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    assert len(items) == 1
    assert items[0].kind == "digest"
    assert "Health: unavailable." in (items[0].body or "")
    assert items[0].title == "Evie digest"

    # Repeat tick a minute later must not double-deliver.
    later = now + timedelta(minutes=2)
    again = await phone_digest_tick(db_session, now=later)
    assert again == []


async def test_digest_push_state_honest_poll_fallback(
    client: AsyncClient, owner_phone, db_session
) -> None:
    from datetime import UTC, datetime

    from app.device_gateway.digest import phone_digest_tick

    _body, owner = owner_phone
    put = await owner.put(
        "/v1/device-gateway/routines",
        json={"enabled": True, "digest_times": ["21:00"], "timezone": "Asia/Kolkata"},
    )
    assert put.status_code == 200
    # No push token registered -> delivery stays poll, reason recorded.
    now = datetime(2026, 9, 8, 15, 30, tzinfo=UTC)
    delivered = await phone_digest_tick(db_session, now=now)
    assert len(delivered) == 1

    from app.models import DeviceInboxItem

    rows = (
        (
            await db_session.execute(
                select(DeviceInboxItem).order_by(DeviceInboxItem.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    assert rows
    push = (rows[0].payload or {}).get("push") or {}
    assert push.get("state") == "poll"
    assert push.get("reason")
