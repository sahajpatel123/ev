"""Tests for whole-life recall: reconstruct any past week with provenance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event, Memory


def _event(
    *,
    occurred_at: datetime,
    event_type: str = "note",
    text: str = "some note",
) -> Event:
    return Event(
        source="test",
        event_type=event_type,
        content={"text": text},
        occurred_at=occurred_at,
        sha256=uuid4().hex * 4,
    )


def _memory(
    *,
    memory_type: str,
    text: str,
    valid_from: datetime,
    valid_until: datetime | None = None,
    importance: float = 0.5,
) -> Memory:
    return Memory(
        memory_type=memory_type,
        text=text,
        payload={},
        importance=importance,
        event_time=valid_from,
        valid_from=valid_from,
        valid_until=valid_until,
        fingerprint=uuid4().hex,
    )


async def test_recall_week_reconstructs_events_and_memories(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    week_a_start = datetime(2026, 1, 5, tzinfo=UTC)
    week_b_start = datetime(2026, 1, 12, tzinfo=UTC)
    db_session.add_all(
        [
            _event(
                occurred_at=week_a_start + timedelta(days=1),
                text="decided to build the EV memory engine with SQLite first",
            ),
            _event(
                occurred_at=week_b_start + timedelta(days=1),
                text="started the iOS companion app",
            ),
            _memory(
                memory_type="decision",
                text="Build memory engine with SQLite first",
                valid_from=week_a_start,
                valid_until=week_a_start + timedelta(days=10),
                importance=0.9,
            ),
            _memory(
                memory_type="goal",
                text="Ship the iOS companion",
                valid_from=week_b_start + timedelta(hours=1),
            ),
        ]
    )
    await db_session.commit()

    resp = await client.get(
        "/v1/recall/week",
        params={"week_start": week_a_start.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert datetime.fromisoformat(body["week_start"]) == week_a_start
    assert datetime.fromisoformat(body["week_end"]) == week_b_start
    assert datetime.fromisoformat(body["as_of"]) == week_b_start
    assert body["event_count"] == 1
    assert body["memory_count"] == 1
    assert [m["memory_type"] for m in body["memories"]] == ["decision"]
    assert len(body["decisions"]) == 1
    assert body["goals"] == []
    assert body["events"][0]["content"]["text"].startswith("decided to build")
    assert body["top_topics"]
    assert body["consolidation"] is None

    # The week-B memory must not leak into week A's historical view.
    resp_b = await client.get(
        "/v1/recall/week",
        params={"week_start": week_b_start.isoformat()},
    )
    assert resp_b.status_code == 200
    assert [m["memory_type"] for m in resp_b.json()["memories"]] == ["goal"]


async def test_recall_week_boundaries_are_half_open(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    week_start = datetime(2026, 2, 2, tzinfo=UTC)
    week_end = week_start + timedelta(days=7)
    db_session.add(
        _event(
            occurred_at=week_start,
            text="first event of the week",
            event_type="message.user",
        )
    )
    db_session.add(
        _event(
            occurred_at=week_end,
            text="exactly at the boundary",
            event_type="message.user",
        )
    )
    await db_session.commit()

    resp = await client.get(
        "/v1/recall/week",
        params={"week_start": week_start.isoformat()},
    )
    assert resp.status_code == 200
    assert resp.json()["event_count"] == 1
    assert resp.json()["events"][0]["content"]["text"] == "first event of the week"


async def test_recall_week_includes_weekly_consolidation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    week_start = datetime(2026, 3, 2, tzinfo=UTC)
    db_session.add(
        _event(
            occurred_at=week_start + timedelta(hours=2),
            text="planned the March research sprint",
            event_type="message.user",
        )
    )
    await db_session.commit()

    resp = await client.post(
        "/v1/consolidate",
        params={
            "granularity": "week",
            "period_start": week_start.isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["written"]

    resp = await client.get(
        "/v1/recall/week",
        params={"week_start": week_start.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["consolidation"] is not None
    assert datetime.fromisoformat(body["consolidation"]["period_start"]) == week_start
    assert body["consolidation"]["event_count"] >= 1
    assert body["consolidation"]["summary"]
