"""Encrypted backup, verify, and restore-drill tests."""

from __future__ import annotations

from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.main import app

PASSPHRASE = "correct-horse-battery-staple-42"


async def post_event(client: AsyncClient, text: str, *, privacy_level: str = "normal") -> dict:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": text, "privacy_level": privacy_level},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def _counts(client: AsyncClient) -> dict:
    timeline = (await client.get("/v1/timeline?include_tombstoned=true&limit=500")).json()
    memories = (await client.get("/v1/memories?limit=200")).json()
    return {"events": len(timeline["events"]), "memories": memories["total"]}


async def test_backup_restore_wipe_roundtrip(client: AsyncClient) -> None:
    await post_event(client, "I decided to use SQLite for local testing.")
    await post_event(client, "I prefer tea over coffee.")

    device = (
        await client.post("/v1/devices", json={"name": "iphone", "capabilities": ["voice"]})
    ).json()
    device_token = device["token"]

    before = await _counts(client)
    assert before["events"] == 2
    assert before["memories"] >= 2

    resp = await client.post("/v1/backup", json={"passphrase": PASSPHRASE})
    assert resp.status_code == 201, resp.text
    backup = resp.json()
    backup_path = backup["path"]
    assert backup["schema_version"] == "ev.backup.v1"
    assert backup["counts"]["events"] == 2
    assert Path(backup_path).exists()

    # Verify a pristine backup.
    verify = await client.post(
        "/v1/backup/verify",
        json={"path": backup_path, "passphrase": PASSPHRASE},
    )
    assert verify.status_code == 200, verify.text
    assert verify.json()["valid"] is True
    assert verify.json()["checksum_match"] is True

    # Mutate the store, then run the wipe restore drill.
    await post_event(client, "gamma event that must disappear.")
    assert (await _counts(client))["events"] == 3

    resp = await client.post(
        "/v1/backup/restore",
        json={
            "path": backup_path,
            "passphrase": PASSPHRASE,
            "mode": "wipe",
            "confirm_wipe": True,
        },
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["mode"] == "wipe"
    assert report["events_restored"] == 2
    assert report["events_skipped"] == 0

    after = await _counts(client)
    assert after["events"] == before["events"] == 2
    assert after["memories"] == before["memories"]

    # Restored device token still authenticates.
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {device_token}"},
    ) as device_client:
        resp = await device_client.get("/v1/timeline")
        assert resp.status_code == 200

    # Sample audit trail survived the drill.
    memory_id = (await client.get("/v1/memories?limit=1")).json()["memories"][0]["id"]
    audit = (await client.get(f"/v1/audit/{memory_id}")).json()
    assert audit["access_log"]


async def test_backup_verify_detects_tampering(client: AsyncClient) -> None:
    await post_event(client, "alpha")
    resp = await client.post("/v1/backup", json={"passphrase": PASSPHRASE})
    backup_path = resp.json()["path"]

    path = Path(backup_path)
    import json

    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["ciphertext"] = envelope["ciphertext"][:-8] + "AAAAAAAA"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    verify = await client.post(
        "/v1/backup/verify",
        json={"path": backup_path, "passphrase": PASSPHRASE},
    )
    assert verify.status_code == 200, verify.text
    payload = verify.json()
    assert payload["valid"] is False
    assert payload["checksum_match"] is False


async def test_backup_wrong_passphrase_rejected(client: AsyncClient) -> None:
    await post_event(client, "alpha")
    resp = await client.post("/v1/backup", json={"passphrase": PASSPHRASE})
    backup_path = resp.json()["path"]

    verify = await client.post(
        "/v1/backup/verify",
        json={"path": backup_path, "passphrase": "wrong-passphrase-999"},
    )
    assert verify.status_code == 200, verify.text
    assert verify.json()["valid"] is False

    restore = await client.post(
        "/v1/backup/restore",
        json={"path": backup_path, "passphrase": "wrong-passphrase-999", "mode": "merge"},
    )
    assert restore.status_code == 400, restore.text


async def test_backup_wipe_requires_confirmation(client: AsyncClient) -> None:
    await post_event(client, "alpha")
    resp = await client.post("/v1/backup", json={"passphrase": PASSPHRASE})
    backup_path = resp.json()["path"]

    resp = await client.post(
        "/v1/backup/restore",
        json={"path": backup_path, "passphrase": PASSPHRASE, "mode": "wipe"},
    )
    assert resp.status_code == 400, resp.text


async def test_backup_merge_deduplicates(client: AsyncClient) -> None:
    await post_event(client, "alpha")
    resp = await client.post("/v1/backup", json={"passphrase": PASSPHRASE})
    backup_path = resp.json()["path"]

    await post_event(client, "beta")
    resp = await client.post(
        "/v1/backup/restore",
        json={"path": backup_path, "passphrase": PASSPHRASE, "mode": "merge"},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["events_restored"] == 0
    assert report["events_skipped"] == 1
    assert (await _counts(client))["events"] == 2


async def test_backup_requires_master_key(client: AsyncClient) -> None:
    await post_event(client, "alpha")
    registered = (
        await client.post("/v1/devices", json={"name": "phone", "capabilities": ["voice"]})
    ).json()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {registered['token']}"},
    ) as device_client:
        resp = await device_client.post("/v1/backup", json={"passphrase": PASSPHRASE})
        assert resp.status_code == 403, resp.text
        resp = await device_client.post(
            "/v1/backup/verify",
            json={"path": "/tmp/nope.evbackup", "passphrase": PASSPHRASE},
        )
        assert resp.status_code == 403, resp.text
        resp = await device_client.post(
            "/v1/backup/restore",
            json={"path": "/tmp/nope.evbackup", "passphrase": PASSPHRASE},
        )
        assert resp.status_code == 403, resp.text


async def test_export_bundle_is_complete(client: AsyncClient) -> None:
    await post_event(client, "alpha")
    await post_event(client, "beta", privacy_level="never_send_to_model")
    await client.post("/v1/devices", json={"name": "phone", "capabilities": ["voice"]})

    resp = await client.post("/v1/export")
    assert resp.status_code == 200, resp.text
    bundle = resp.json()
    assert bundle["version"] == "1.0"
    assert len(bundle["events"]) == 2
    assert len(bundle["memories"]) >= 1
    assert len(bundle["devices"]) == 1
    assert "conflicts" in bundle
    assert "attachments" in bundle
    assert "access_log" in bundle
    assert bundle["access_log"], "export should include the audit trail"
