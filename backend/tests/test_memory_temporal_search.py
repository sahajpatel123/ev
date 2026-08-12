"""As-of temporal search: resolved temporal ranges drive time-travel queries."""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient

from app.memory.temporal import (
    memory_temporal_bounds,
    temporal_overlap,
)


async def _post_event(client: AsyncClient, text: str, occurred_at: datetime) -> dict:
    resp = await client.post(
        "/v1/events",
        json={
            "source": "test",
            "event_type": "note",
            "text": text,
            "occurred_at": occurred_at.isoformat(),
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def _temporal_search(
    client: AsyncClient,
    *,
    period_start: str,
    period_end: str,
) -> list[dict]:
    resp = await client.get(
        "/v1/temporal/memories",
        params={"period_start": period_start, "period_end": period_end},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["memories"]


def test_temporal_overlap_semantics() -> None:
    assert temporal_overlap(None, None, None, None) is True
    assert temporal_overlap(datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 3, 15, tzinfo=UTC), datetime(2026, 4, 15, tzinfo=UTC)) is True
    assert temporal_overlap(datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 5, 1, tzinfo=UTC)) is False
    assert temporal_overlap(datetime(2026, 1, 1, tzinfo=UTC), None, datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)) is True


async def test_temporal_search_returns_resolved_ranges(client: AsyncClient) -> None:
    anchor = datetime(2026, 8, 12, 9, 30, tzinfo=UTC)
    await _post_event(client, "In March I want to focus on health.", anchor)
    await _post_event(client, "We met last Tuesday for coffee.", anchor)

    march = await _temporal_search(
        client,
        period_start="2027-03-01T00:00:00+00:00",
        period_end="2027-04-01T00:00:00+00:00",
    )
    assert any("health" in m["text"] for m in march)
    assert not any("coffee" in m["text"] for m in march)

    tuesday = await _temporal_search(
        client,
        period_start="2026-08-04T00:00:00+00:00",
        period_end="2026-08-05T00:00:00+00:00",
    )
    assert any("coffee" in m["text"] for m in tuesday)

    # Event-time overlap also counts (a capture recorded during the period).
    same_day = await _temporal_search(
        client,
        period_start="2026-08-12T00:00:00+00:00",
        period_end="2026-08-13T00:00:00+00:00",
    )
    assert any("coffee" in m["text"] for m in same_day)
    assert any("health" in m["text"] for m in same_day)


async def test_memory_temporal_bounds_aggregates_entries(client: AsyncClient) -> None:
    anchor = datetime(2026, 8, 12, tzinfo=UTC)
    event = await _post_event(client, "In March I want to focus on health.", anchor)
    resp = await client.get(f"/v1/events/{event['id']}")
    assert resp.status_code == 200

    # Verify through the stored memory payload rather than re-running rules.
    memories = await _temporal_search(
        client,
        period_start="2027-03-01T00:00:00+00:00",
        period_end="2027-04-01T00:00:00+00:00",
    )
    from types import SimpleNamespace

    fake = SimpleNamespace(payload=memories[0]["payload"])
    start, end = memory_temporal_bounds(fake)
    assert start is not None and end is not None
    assert start.date().isoformat() == "2027-03-01"
    assert end.date().isoformat() == "2027-04-01"
