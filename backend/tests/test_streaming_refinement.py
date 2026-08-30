"""Streaming refinement protocol (plan 3.9): raw deltas then refined replace."""

from __future__ import annotations

import json

from httpx import AsyncClient

from app.filter.envelope import GroundingMaterial
from app.filter.stream_refiner import StreamRefiner


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
    assert names[0] == "status"
    assert events[0][1].get("stage") == "accepted"
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


def test_stream_refiner_buffers_claim_until_sentence_boundary() -> None:
    refiner = StreamRefiner(grounding=[])
    emitted, buffered, _ = refiner.feed("You decided to move to Mars")
    assert emitted == ""
    assert buffered == "You decided to move to Mars"

    emitted, buffered, _ = refiner.feed(" next week.")
    assert "Mars" not in emitted
    assert "can't confirm" in emitted.lower()
    assert buffered == ""
    assert any(e["type"] == "flag" for e in refiner.events)


def test_stream_refiner_emits_grounded_claim() -> None:
    refiner = StreamRefiner(
        grounding=[
            GroundingMaterial(
                text="I decided to use SQLite for local testing.",
                memory_id="m1",
                memory_type="decision",
            )
        ]
    )
    emitted, buffered, _ = refiner.feed("You decided to use SQLite for local testing.")
    assert "SQLite" in emitted
    assert buffered == ""


def test_stream_refiner_does_not_stall_on_long_buffer() -> None:
    refiner = StreamRefiner(grounding=[], buffer_limit=120)
    emitted, buffered, _ = refiner.feed("You decided to move to Mars " + "x" * 300)
    assert emitted
    assert buffered == ""
    assert any(e["type"] == "buffer_overflow" for e in refiner.events)
    # The overflowed claim is re-audited by the final refined pass, which is
    # the only correction that can happen after a chunk has been emitted.
    assert refiner.final_text()


def test_stream_refiner_flush_audits_tail() -> None:
    refiner = StreamRefiner(grounding=[])
    refiner.feed("You moved to Mars")
    emitted, buffered, _ = refiner.flush()
    assert "Mars" not in emitted
    assert "can't confirm" in emitted.lower()
    assert buffered == ""
