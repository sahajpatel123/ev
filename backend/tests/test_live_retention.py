"""Live-event retention policy: dry-run, conservative deletion, provenance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LiveDerivedState, LiveEvent
from app.workers.scheduler import LiveMaintenance


async def _ingest(
    client: AsyncClient,
    channel: str,
    kind: str,
    event_type: str,
    payload: dict,
    occurred_at: datetime,
) -> dict:
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
    return resp.json()[0]


async def test_live_retention_plans_then_deletes_conservatively(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=200)
    recent = now - timedelta(days=1)

    # Screen: two old + one recent -> the two old are deletable.
    await _ingest(client, "screen-activity", "screen", "focus_change", {"app": "Xcode"}, old)
    await _ingest(client, "screen-activity", "screen", "focus_change", {"app": "Notes"}, old + timedelta(minutes=1))
    screen_recent = await _ingest(client, "screen-activity", "screen", "focus_change", {"app": "Figma"}, recent)

    # Health: a single old event -> protected because it is the channel's latest.
    health_old = await _ingest(client, "health-belt", "health", "heart_rate", {"bpm": 132}, old + timedelta(minutes=2))

    # Location: three old events; the oldest is provenance-linked by a
    # recognition log, the newest is the channel's latest -> middle is deletable.
    loc_1 = await _ingest(client, "location-coarse", "location", "location_change", {"place": "Home"}, old + timedelta(minutes=3))
    await _ingest(client, "location-coarse", "location", "location_change", {"place": "Office"}, old + timedelta(minutes=4))
    loc_3 = await _ingest(client, "location-coarse", "location", "location_change", {"place": "Gym"}, old + timedelta(minutes=5))
    resp = await client.post(
        "/v1/vision/annotate",
        json={
            "label": "Home",
            "entity_type": "place",
            "live_event_id": loc_1["id"],
            "source": "user",
        },
    )
    assert resp.status_code == 201, resp.text

    # Fold everything into derived state so events become consumed.
    resp = await client.post("/v1/live/rebuild")
    assert resp.status_code == 200
    assert resp.json()["consumed_count"] == 7

    # Dry run: plans deletion without touching anything.
    resp = await client.post("/v1/live/retention?days=90")
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["dry_run"] is True
    assert plan["events_scanned"] == 6
    assert plan["events_deleted"] == 3  # screen_old_1, screen_old_2, loc_2
    assert plan["events_kept_latest"] == 2  # health_old, loc_3
    assert plan["events_protected"] == 3  # health_old, loc_1, loc_3
    assert plan["channels_updated"] == 0

    rows = (await db_session.execute(select(LiveEvent))).scalars().all()
    assert len(rows) == 7

    # Execute: only the three eligible events are removed.
    resp = await client.post("/v1/live/retention?days=90&dry_run=false")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["dry_run"] is False
    assert result["events_deleted"] == 3
    assert result["events_protected"] == 3
    assert result["channels_updated"] == 2  # screen + location rollups recomputed

    remaining = (await db_session.execute(select(LiveEvent))).scalars().all()
    remaining_ids = {str(row.id) for row in remaining}
    assert remaining_ids == {
        screen_recent["id"],
        health_old["id"],
        loc_1["id"],
        loc_3["id"],
    }

    # Derived rollups stay consistent with the retained stream.
    derived = {
        str(row.channel_id): row
        for row in (await db_session.execute(select(LiveDerivedState))).scalars().all()
    }
    assert derived[screen_recent["channel_id"]].event_count == 1
    assert str(derived[screen_recent["channel_id"]].latest_event_id) == screen_recent["id"]
    assert derived[loc_1["channel_id"]].event_count == 2

    # Unconsumed events are never eligible for retention.
    unconsumed = await _ingest(client, "screen-activity", "screen", "focus_change", {"app": "OldTab"}, old + timedelta(days=1))
    assert unconsumed["consumed"] is False
    resp = await client.post("/v1/live/retention?days=90&dry_run=false")
    assert resp.status_code == 200
    # The three protected old events are scanned but never deleted; the
    # unconsumed event is not even eligible.
    assert resp.json()["events_scanned"] == 3
    assert resp.json()["events_deleted"] == 0
    assert resp.json()["events_protected"] == 3
    rows = (await db_session.execute(select(LiveEvent))).scalars().all()
    assert any(str(row.id) == unconsumed["id"] for row in rows)


async def test_sensitive_channels_retain_shorter_than_private(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=60)
    recent = now - timedelta(days=10)

    # Screen (sensitive) defaults to 30 days; location (private) to 90 days.
    await _ingest(client, "screen-activity", "screen", "focus_change", {"app": "Xcode"}, old)
    await _ingest(client, "screen-activity", "screen", "focus_change", {"app": "Notes"}, recent)
    await _ingest(client, "location-coarse", "location", "location_change", {"place": "Home"}, old)
    await _ingest(client, "location-coarse", "location", "location_change", {"place": "Office"}, recent)

    resp = await client.post("/v1/live/rebuild")
    assert resp.status_code == 200

    resp = await client.post("/v1/live/retention?dry_run=false")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["events_scanned"] == 2  # only the 60-day-old consumed events
    assert result["events_deleted"] == 1  # screen is past 30d; location stays until 90d
    assert result["events_protected"] == 1  # the 60-day-old location event

    remaining = (await db_session.execute(select(LiveEvent))).scalars().all()
    assert len(remaining) == 3
    payloads = [row.payload for row in remaining]
    assert {"app": "Xcode"} not in payloads
    assert {"place": "Home"} in payloads


def test_scheduler_runs_live_retention_on_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[str] = []
    monkeypatch.setattr(
        "app.workers.scheduler.run_live_retention",
        lambda: runs.append("retention") or {"ok": True},
    )
    monkeypatch.setattr(
        "app.workers.scheduler.run_live_rebuild",
        lambda: runs.append("rebuild") or {"ok": True},
    )

    maintenance = LiveMaintenance(
        retention_interval_seconds=10,
        rebuild_interval_seconds=1_000,
    )
    startup = maintenance.run_due(now=0.0)
    assert startup == {"retention": {"ok": True}, "rebuild": {"ok": True}}
    assert runs == ["retention", "rebuild"]

    assert maintenance.run_due(now=5.0) == {}
    assert runs == ["retention", "rebuild"]

    second = maintenance.run_due(now=11.0)
    assert second == {"retention": {"ok": True}}
    assert runs == ["retention", "rebuild", "retention"]
