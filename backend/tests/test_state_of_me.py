"""Long-horizon 'state of me' rollups from versioned memory with provenance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient


async def post_event(
    client: AsyncClient,
    text: str,
    *,
    occurred_at: datetime,
) -> dict:
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


async def _state_of_me(client: AsyncClient) -> dict | None:
    resp = await client.get("/v1/memories", params={"memory_type": "summary"})
    assert resp.status_code == 200, resp.text
    for memory in resp.json()["memories"]:
        if (memory["payload"] or {}).get("kind") == "state_of_me":
            return memory
    return None


async def test_state_of_me_rollup_with_version_changes(client: AsyncClient) -> None:
    base = datetime(2026, 7, 5, tzinfo=UTC)
    await post_event(client, "I decided to use SQLite for local testing.", occurred_at=base)
    await post_event(
        client,
        "I decided to use SQLite for local testing, and document the choice.",
        occurred_at=base + timedelta(hours=1),
    )
    await post_event(client, "I prefer tea over coffee.", occurred_at=base + timedelta(days=1))
    await post_event(client, "I want to ship the iOS companion.", occurred_at=base + timedelta(days=2))

    resp = await client.post(
        "/v1/state-of-me",
        params={
            "period_start": "2026-07-01T00:00:00+00:00",
            "period_end": "2026-08-01T00:00:00+00:00",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["written"]

    rollup = await _state_of_me(client)
    assert rollup is not None
    payload = rollup["payload"]
    assert payload["kind"] == "state_of_me"
    assert payload["memory_group_count"] >= 3
    assert payload["counts"]["decision"] >= 2
    assert rollup["source_type"] == "derived"
    assert len(rollup["source_events"]) >= 4
    decision_group = next(
        g for g in payload["groups"] if g["memory_type"] == "decision"
    )
    assert decision_group["version_count"] >= 2
    assert decision_group["latest_reason"] == "Value changed"
    assert "State of me" in rollup["text"]

    # Deterministic rerun with no new evidence returns the same summary.
    resp2 = await client.post(
        "/v1/state-of-me",
        params={
            "period_start": "2026-07-01T00:00:00+00:00",
            "period_end": "2026-08-01T00:00:00+00:00",
        },
    )
    assert resp2.status_code == 200
    assert [str(w) for w in resp2.json()["written"]] == [str(w) for w in resp.json()["written"]]


async def test_state_of_me_survives_rebuild(client: AsyncClient) -> None:
    base = datetime(2026, 7, 5, tzinfo=UTC)
    await post_event(client, "I decided to use SQLite for local testing.", occurred_at=base)
    await post_event(client, "I prefer tea over coffee.", occurred_at=base + timedelta(days=1))
    await client.post(
        "/v1/state-of-me",
        params={
            "period_start": "2026-07-01T00:00:00+00:00",
            "period_end": "2026-08-01T00:00:00+00:00",
        },
    )
    before = await _state_of_me(client)
    assert before is not None

    resp = await client.post("/v1/memory/rebuild")
    assert resp.status_code == 200, resp.text
    after = await _state_of_me(client)
    assert after is not None
    assert after["payload"]["memory_group_count"] == before["payload"]["memory_group_count"]
    assert after["payload"]["counts"] == before["payload"]["counts"]
    assert after["text"] == before["text"]
