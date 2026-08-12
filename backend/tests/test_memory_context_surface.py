"""End-to-end: conflicts and progressive depth reach the real chat context."""

from __future__ import annotations

from httpx import AsyncClient


async def _post_event(client: AsyncClient, text: str) -> dict:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def _chat(client: AsyncClient, message: str) -> dict:
    resp = await client.post("/v1/chat", json={"message": message})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_chat_context_plan_includes_open_conflicts(client: AsyncClient) -> None:
    await _post_event(client, "I prefer tea over coffee.")
    await _post_event(client, "I prefer coffee over tea.")
    body = await _chat(client, "Which do I prefer, tea or coffee?")
    plan = body["context_plan"]
    sections = {section["name"]: section for section in plan["sections"]}
    assert "open_conflicts" in sections
    assert sections["open_conflicts"]["items_included"] >= 1


async def test_chat_context_plan_progressive_deep(client: AsyncClient) -> None:
    for index in range(20):
        await _post_event(client, f"@topic{index} is interesting.")
    body = await _chat(
        client,
        "Why did I decide to use Postgres in March?",
    )
    plan = body["context_plan"]
    assert plan["metadata"]["progressive"] is True
    assert plan["metadata"]["depth"] == "deep"
    assert plan["metadata"]["attempts"] == 2
    memory_section = next(
        section
        for section in plan["sections"]
        if section["name"] == "retrieved_memory"
    )
    assert memory_section["items_included"] > 10
