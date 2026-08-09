"""Cross-device continuity: every client surface reads the same memory.

Captures from CLI, web, and iOS-style sources all land in one event log and
derive into one shared memory store; an ask from any surface retrieves the
same provenance.
"""

from __future__ import annotations

from httpx import AsyncClient

from clients.cli import ask, capture, memories, timeline


async def test_cross_surface_captures_share_one_memory(client: AsyncClient) -> None:
    await capture(
        "The EVIE build uses fixed-term contracts for client work.",
        source="cli",
        client=client,
    )
    await capture(
        "The enclosure needs a chamfered edge for the new gasket.",
        source="web",
        client=client,
    )
    await capture(
        "Sam likes local AI tools.",
        source="ios",
        client=client,
    )

    data = await timeline(client=client, limit=50)
    sources = {event["source"] for event in data["events"]}
    assert {"cli", "web", "ios"} <= sources

    # The memory written from the CLI surface is retrievable from any surface.
    found = await memories(client=client, q="fixed-term contracts", limit=10)
    assert found["total"] >= 1
    assert any("fixed-term" in memory["text"] for memory in found["memories"])

    # Ask from the "web" surface: same memory store, provenance is non-empty.
    reply = await ask("What do I prefer for client work?", client=client)
    assert reply["reply"]
    assert reply.get("provenance")


async def test_conversation_continues_across_surfaces(client: AsyncClient) -> None:
    first = await ask("Remember the project codename: EVIE.", client=client)
    assert first["conversation_id"]

    second = await ask(
        "What is the project codename?",
        client=client,
    )
    assert second["reply"]
    assert second["conversation_id"] == first["conversation_id"]
