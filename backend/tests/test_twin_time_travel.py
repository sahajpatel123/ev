"""Digital-twin time travel: as-of queries over versioned memory."""

from __future__ import annotations

from httpx import AsyncClient


async def post_event(client: AsyncClient, text: str) -> dict:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def post_event_at(client: AsyncClient, text: str, occurred_at: str) -> dict:
    resp = await client.post(
        "/v1/events",
        json={
            "source": "test",
            "event_type": "note",
            "text": text,
            "occurred_at": occurred_at,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def test_twin_as_of_returns_superseded_version(client: AsyncClient) -> None:
    first = await post_event(client, "I want to build EV as a persistent personal AI.")
    await post_event(
        client,
        "I want to build EV as a persistent personal AI, and make it self-hosted.",
    )

    current = (await client.get("/v1/twin")).json()
    assert any("self-hosted" in g["text"] for g in current["goals"])

    past = (await client.get(f"/v1/twin?as_of={first['occurred_at']}")).json()
    assert any(
        "build EV as a persistent personal AI" in g["text"] and "self-hosted" not in g["text"]
        for g in past["goals"]
    )
    assert not any("self-hosted" in g["text"] for g in past["goals"])

    # Provenance survives the time-travel view.
    past_goal = next(
        g for g in past["goals"] if "build EV as a persistent personal AI" in g["text"]
    )
    assert past_goal["source_event_ids"]
    assert past_goal["version"] == 1


async def test_twin_as_of_last_march_returns_the_march_thinking(client: AsyncClient) -> None:
    """'What was I thinking in March?' returns the superseded March version."""
    await post_event_at(
        client,
        "I want to build EV as a persistent personal AI.",
        "2026-03-01T09:00:00Z",
    )
    await post_event_at(
        client,
        "I want to build EV as a persistent personal AI, and make it self-hosted.",
        "2026-04-01T09:00:00Z",
    )

    current = (await client.get("/v1/twin")).json()
    assert any("self-hosted" in g["text"] for g in current["goals"])

    march = (await client.get("/v1/twin?as_of=2026-03-15T00:00:00Z")).json()
    march_goal = next(
        g for g in march["goals"] if "build EV as a persistent personal AI" in g["text"]
    )
    assert "self-hosted" not in march_goal["text"]
    assert march_goal["version"] == 1
    assert march_goal["source_event_ids"]
