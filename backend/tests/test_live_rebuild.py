"""Deterministic live-data replay/rebuild guarantees."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LiveDerivedState, LiveEvent


async def _ingest_signals(client: AsyncClient) -> None:
    now = datetime.now(UTC)
    base = now.replace(hour=23, minute=0, second=0, microsecond=0) - timedelta(days=1)
    payloads = [
        ("screen-activity", "screen", "focus_change", {"app": "Xcode"}, base + timedelta(minutes=30)),
        ("screen-activity", "screen", "focus_change", {"app": "Notes"}, base + timedelta(minutes=31)),
        ("health-belt", "health", "heart_rate", {"bpm": 132}, base + timedelta(minutes=32)),
        ("audio-ambient", "audio", "scene", {"scene": "meeting", "in_call": True}, base + timedelta(minutes=33)),
        ("location-coarse", "location", "location_change", {"place": "Bengaluru Airport", "presence": "present", "latitude": 12.99, "longitude": 77.6}, base + timedelta(minutes=34)),
    ]
    for channel, kind, event_type, payload, occurred_at in payloads:
        resp = await client.post(
            "/v1/live/events",
            json={
                "channel": channel,
                "kind": kind,
                "events": [
                    {
                        "event_type": event_type,
                        "payload": payload,
                        "occurred_at": occurred_at.isoformat(),
                    }
                ],
            },
        )
        assert resp.status_code == 201, resp.text


async def _event_snapshot(db_session: AsyncSession) -> list[tuple]:
    rows = (await db_session.execute(select(LiveEvent).order_by(LiveEvent.occurred_at))).scalars().all()
    return [
        (str(row.id), str(row.channel_id), row.occurred_at.isoformat(), row.event_type, row.sha256, row.payload)
        for row in rows
    ]


async def test_live_rebuild_replays_stream_deterministically(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await _ingest_signals(client)
    events_before = await _event_snapshot(db_session)
    assert len(events_before) == 5

    resp = await client.post("/v1/live/rebuild")
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["events_total"] == 5
    assert report["events_replayed"] == 5
    assert report["consumed_count"] == 5
    assert report["channels_rebuilt"] == 4
    assert report["deleted_derived_rows"] == 0

    by_name = {c["channel_name"]: c for c in report["channels"]}
    assert by_name["screen-activity"]["event_count"] == 2
    assert by_name["health-belt"]["event_count"] == 1
    assert by_name["audio-ambient"]["event_count"] == 1
    assert by_name["location-coarse"]["event_count"] == 1

    screen_signals = {s["kind"]: s for s in by_name["screen-activity"]["signals"]}
    assert "screen_late_night" in screen_signals
    assert len(screen_signals["screen_late_night"]["basis_ids"]) == 2
    assert {s["kind"] for s in by_name["health-belt"]["signals"]} == {"live_health_signal"}
    assert {s["kind"] for s in by_name["audio-ambient"]["signals"]} == {"audio_in_call"}
    location_signals = {s["kind"]: s for s in by_name["location-coarse"]["signals"]}
    assert location_signals["location_presence"]["place"] == "Bengaluru Airport"

    # Derived state never carries raw payload content.
    raw = str(report["channels"])
    assert "12.99" not in raw
    assert "77.6" not in raw

    # The lifecycle flag is now meaningful: every folded event is consumed.
    resp = await client.get("/v1/live/status")
    assert resp.status_code == 200
    assert resp.json()["consumed_24h"] == 5

    # Raw events are untouched by the rebuild.
    assert await _event_snapshot(db_session) == events_before

    # Determinism: a second rebuild produces the identical derived layer.
    resp = await client.post("/v1/live/rebuild")
    assert resp.status_code == 200
    second = resp.json()
    assert second["deleted_derived_rows"] == 4
    assert second["events_replayed"] == 5
    assert second["consumed_count"] == 5
    second_by_name = {c["channel_name"]: c for c in second["channels"]}
    for name in by_name:
        assert second_by_name[name]["event_count"] == by_name[name]["event_count"]
        assert second_by_name[name]["latest_event_id"] == by_name[name]["latest_event_id"]
        assert second_by_name[name]["signals"] == by_name[name]["signals"]

    derived_rows = (
        await db_session.execute(select(LiveDerivedState).order_by(LiveDerivedState.channel_id))
    ).scalars().all()
    assert len(derived_rows) == 4
    assert all(row.consumed_count == row.event_count for row in derived_rows)
    assert all(row.signals for row in derived_rows)
