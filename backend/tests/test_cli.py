"""End-to-end tests for the EV CLI client and offline capture sync."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from httpx import AsyncClient

from clients.cli import (
    QUEUE_FILENAME,
    audit,
    capture,
    card,
    correct,
    enqueue_capture,
    forget,
    list_queue,
    memories,
    restore,
    sync_captures,
    timeline,
)


class OfflineTransport(httpx.AsyncBaseTransport):
    """Transport that simulates a lost connection."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)


def offline_client() -> AsyncClient:
    return AsyncClient(
        transport=OfflineTransport(),
        base_url="http://offline",
        headers={"Authorization": "Bearer test-key"},
    )


async def test_cli_capture_timeline_memories_audit_roundtrip(
    client: AsyncClient, tmp_path: Path
) -> None:
    result = await capture(
        "Remember: I prefer fixed-term contracts for client work now.",
        client=client,
        queue=tmp_path / "queue",
    )
    event = result["event"]
    assert event["source"] == "cli"
    assert event["event_type"] == "note"

    data = await timeline(client=client, limit=10)
    assert any(e["id"] == event["id"] for e in data["events"])

    data = await memories(client=client, q="fixed-term contracts", limit=10)
    assert data["total"] >= 1
    memory = data["memories"][0]

    trail = await audit(memory["id"], client=client)
    assert trail["memory"]["id"] == memory["id"]
    assert any(e["id"] == event["id"] for e in trail["source_events"])
    assert trail["versions"][0]["version"] == memory["version"]


async def test_cli_memory_correct_forget_restore(client: AsyncClient, tmp_path: Path) -> None:
    await capture("The enclosure needs a chamfered edge for the new gasket.", client=client)
    data = await memories(client=client, q="chamfered edge", limit=10)
    memory = data["memories"][0]

    corrected = await correct(
        memory["id"],
        "The enclosure needs a rounded edge for the new gasket.",
        reason="geometry fix",
        client=client,
    )
    assert corrected["version"] == memory["version"] + 1
    assert "rounded edge" in corrected["text"]
    assert corrected["is_current"] is True

    forgotten = await forget(corrected["id"], reason="test", client=client)
    assert forgotten["is_current"] is False
    assert forgotten["valid_until"] is not None

    restored = await restore(corrected["id"], client=client)
    assert restored["is_current"] is True
    assert restored["valid_until"] is None


async def test_idempotency_key_deduplicates_events(client: AsyncClient) -> None:
    payload = {
        "source": "cli",
        "event_type": "note",
        "text": "offline replay must not duplicate",
    }
    headers = {"Idempotency-Key": "cli-test-replay-1"}
    first = await client.post("/v1/events", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    event_id = first.json()["event"]["id"]

    second = await client.post("/v1/events", json=payload, headers=headers)
    assert second.status_code == 409, second.text
    assert second.json()["event"]["id"] == event_id

    data = await timeline(client=client, limit=100)
    assert sum(1 for e in data["events"] if e["id"] == event_id) == 1


async def test_offline_capture_queues_and_syncs(client: AsyncClient, tmp_path: Path) -> None:
    queue = tmp_path / "queue"
    async with offline_client() as offline:
        result = await capture(
            "Captured while disconnected.",
            client=offline,
            queue=queue,
        )
    assert result["queued"] is True
    assert (queue / QUEUE_FILENAME).exists()
    records = list_queue(queue)
    assert len(records) == 1
    assert records[0]["payload"]["text"] == "Captured while disconnected."

    summary = await sync_captures(client, queue)
    assert summary["synced"] == 1
    assert summary["dropped"] == 0
    assert summary["remaining"] == 0
    assert list_queue(queue) == []

    data = await timeline(client=client, limit=100)
    texts = [
        (e.get("content") or {}).get("text", "")
        for e in data["events"]
        if e["source"] == "cli"
    ]
    assert "Captured while disconnected." in texts


async def test_sync_drops_duplicate_replays(client: AsyncClient, tmp_path: Path) -> None:
    queue = tmp_path / "queue"
    payload = {"source": "cli", "event_type": "note", "text": "idempotent replay"}
    enqueue_capture(payload, "cli-replay-key", queue)

    summary = await sync_captures(client, queue)
    assert summary["synced"] == 1

    enqueue_capture(payload, "cli-replay-key", queue)
    summary = await sync_captures(client, queue)
    assert summary["synced"] == 0
    assert summary["dropped"] == 1
    assert summary["remaining"] == 0
    assert list_queue(queue) == []

    data = await timeline(client=client, limit=100)
    texts = [
        (e.get("content") or {}).get("text", "")
        for e in data["events"]
        if e["source"] == "cli"
    ]
    assert texts.count("idempotent replay") == 1


async def test_hud_card_schema(client: AsyncClient, tmp_path: Path) -> None:
    hud = await card(client=client)
    assert hud["schema_version"] == "ev.hud.card.v1"
    assert hud["title"]
    assert hud["body"]
    assert "priority" in hud


async def test_sync_quarantines_invalid_payload(client: AsyncClient, tmp_path: Path) -> None:
    queue = tmp_path / "queue"
    enqueue_capture(
        {"source": "cli"},  # missing event_type -> 422 validation
        "cli-invalid-key",
        queue,
    )
    summary = await sync_captures(client, queue)
    assert summary["synced"] == 0
    assert summary["quarantined"] == 1
    assert summary["remaining"] == 0
    assert list_queue(queue) == []
    quarantine = queue / "quarantine.jsonl"
    assert quarantine.exists()
    entries = [json.loads(line) for line in quarantine.read_text().splitlines()]
    assert entries[0]["idempotency_key"] == "cli-invalid-key"
