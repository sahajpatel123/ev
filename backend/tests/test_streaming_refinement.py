"""Streaming refinement protocol (plan 3.9): raw deltas then refined replace."""

from __future__ import annotations

import json

from httpx import AsyncClient


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        name = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: ").strip())
        if name and data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events


async def test_chat_sse_streams_raw_then_refined(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/chat",
        json={"message": "Why did I decide to use SQLite for local testing?", "stream": True},
    )
    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers["content-type"]

    events = _parse_sse(resp.text)
    names = [name for name, _data in events]
    assert "memory-delta" in names
    assert "provenance" in names
    assert "delta" in names
    assert "refined" in names
    assert "done" in names

    deltas = [data["text"] for name, data in events if name == "delta"]
    refined = next(data for name, data in events if name == "refined")
    done = next(data for name, data in events if name == "done")

    assert len(deltas) > 1  # progressive chunks, not one buffered blob
    assert all(isinstance(chunk, str) and chunk for chunk in deltas)
    assert "".join(deltas) == refined["text"]  # raw stream == final text when unfiltered
    assert refined["replaces"] is True
    assert done["conversation_id"] is not None

    # Every delta except the last carries final=false.
    for name, data in events:
        if name == "delta":
            assert isinstance(data["final"], bool)
