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
