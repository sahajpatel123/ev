"""End-to-end tests for the EV CLI client and offline capture sync."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment
from clients.cli import (
    QUEUE_FILENAME,
    attach,
    audit,
    capture,
    card,
    correct,
    enqueue_attachment,
    enqueue_capture,
    export_bundle,
    forget,
    identity_owner_create,
    identity_passkey_add,
    identity_passkey_list,
    identity_passkey_remove,
    identity_recovery_redeem,
    identity_reverification_issue,
    identity_status,
    import_bundle_file,
    list_queue,
    memories,
    onboarding,
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


async def test_cli_import_bundle_merge_roundtrip(
    client: AsyncClient, tmp_path: Path
) -> None:
    await capture("Import me later.", client=client)
    bundle = await export_bundle(client=client)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle, default=str), encoding="utf-8")

    summary = await import_bundle_file(path, mode="merge", client=client)
    assert summary["completed_at"]
    assert summary["events_total"] >= 1
    # Same DB: every event already exists, so the merge dedupes by content hash.
    assert summary["events_imported"] == 0
    assert summary["events_skipped"] >= 1


async def test_cli_onboarding_creates_first_memories_and_audit(
    client: AsyncClient,
) -> None:
    result = await onboarding(
        [
            "I prefer fixed-term contracts for client work.",
            "Goal: ship EV this month.",
            "Sam likes local AI tools.",
        ],
        client=client,
    )
    assert len(result["events"]) == 3
    assert all(event["source"] == "onboarding" for event in result["events"])
    assert result["audits"]
    first = result["audits"][0]
    assert first["memory"]["id"]
    assert first["source_events"]
    assert first["versions"]


async def test_cli_attach_file_capture_roundtrip(
    client: AsyncClient, tmp_path: Path
) -> None:
    path = tmp_path / "note.txt"
    payload = b"Attachment capture integrity check for EV."
    path.write_bytes(payload)

    result = await attach(path, client=client)
    attachment = result["attachment"]
    event = result["event"]

    assert attachment["filename"] == "note.txt"
    assert attachment["size_bytes"] == len(payload)
    assert event["source"] == "attachment"
    assert event["event_type"] == "file"
    assert (event.get("content") or {}).get("filename") == "note.txt"

    resp = await client.get(f"/v1/attachments/{attachment['id']}")
    assert resp.status_code == 200
    assert resp.content == payload


async def test_offline_attachment_queues_and_syncs(
    client: AsyncClient,
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    queue = tmp_path / "queue"
    file_path = tmp_path / "offline.txt"
    payload = b"offline attachment bytes"
    file_path.write_bytes(payload)

    async with offline_client() as offline:
        result = await attach(file_path, client=offline, queue=queue)
    assert result["queued"] is True
    records = list_queue(queue)
    assert len(records) == 1
    assert records[0]["kind"] == "attachment"
    assert records[0]["file_path"] == str(file_path)

    summary = await sync_captures(client, queue)
    assert summary["synced"] == 1
    assert summary["remaining"] == 0
    assert list_queue(queue) == []

    data = await timeline(client=client, limit=100)
    events = [e for e in data["events"] if e["source"] == "attachment"]
    assert events and (events[0]["content"] or {}).get("filename") == "offline.txt"

    rows = (await db_session.execute(select(Attachment))).scalars().all()
    assert len(rows) == 1
    resp = await client.get(f"/v1/attachments/{rows[0].id}")
    assert resp.status_code == 200
    assert resp.content == payload


async def test_sync_quarantines_attachment_with_missing_file(
    client: AsyncClient,
    tmp_path: Path,
) -> None:
    queue = tmp_path / "queue"
    missing = tmp_path / "gone.txt"
    enqueue_attachment(
        {
            "source": "attachment",
            "event_type": "file",
            "privacy_level": "normal",
            "metadata": "{}",
        },
        missing,
        "cli-missing-file",
        queue,
    )

    summary = await sync_captures(client, queue)
    assert summary["quarantined"] == 1
    assert summary["synced"] == 0
    assert summary["remaining"] == 0
    assert list_queue(queue) == []

    quarantine = queue / "quarantine.jsonl"
    assert quarantine.exists()
    entries = [json.loads(line) for line in quarantine.read_text().splitlines()]
    assert "file missing" in entries[0]["reason"]


async def test_cli_identity_owner_passkey_lifecycle(client: AsyncClient) -> None:
    status = await identity_status(client=client)
    assert status["owner_established"] is False

    owner = await identity_owner_create("Sahaj", client=client)
    assert owner["owner_id"]
    assert len(owner["recovery_codes"]) == 8

    status = await identity_status(client=client)
    assert status["owner_established"] is True
    assert status["trust_level"] == "master"

    registered = await identity_passkey_add(
        "cli-credential-id-0001",
        "cli key",
        client=client,
    )
    passkey_id = registered["passkey"]["id"]
    rows = await identity_passkey_list(client=client)
    assert len(rows) == 1

    revoked = await identity_passkey_remove(passkey_id, client=client)
    assert revoked["revoked_at"] is not None
    assert await identity_passkey_list(client=client) == []


async def test_cli_identity_verify_and_recovery_redeem(client: AsyncClient) -> None:
    owner = await identity_owner_create("Sahaj", client=client)
    proof = await identity_reverification_issue("memory.delete", client=client)
    assert proof["token"]
    assert proof["purpose"] == "memory.delete"

    # Recovery redeem works through the same API (real CLI path is unauthenticated).
    code = owner["recovery_codes"][0]["code"]
    redeemed = await identity_recovery_redeem(code, "new-phone", client=client)
    assert redeemed["device"]["trust_level"] == "owner"
    assert redeemed["token"]
