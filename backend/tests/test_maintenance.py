"""Operational maintenance tests: tombstoned-blob purge and backup retention."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment, Event
from app.services.maintenance import prune_backups
from app.utils.text import utcnow


async def test_purge_tombstoned_blobs_respects_audit_window(
    client,
    db_session: AsyncSession,
) -> None:
    data = b"secret attachment bytes"
    upload = await client.post(
        "/v1/attachments",
        files={"file": ("notes.txt", data, "text/plain")},
        data={"event_type": "note"},
    )
    assert upload.status_code == 201, upload.text
    attachment = upload.json()["attachment"]
    event_id = upload.json()["event"]["id"]

    # Still inside the audit window -> nothing purged.
    resp = await client.post("/v1/maintenance/purge-tombstoned-blobs", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["events_eligible"] == 0

    # Tombstone the event and age the tombstone past the default 30-day window.
    resp = await client.delete(f"/v1/events/{event_id}?reason=user-requested")
    assert resp.status_code == 200, resp.text
    event = (
        await db_session.execute(select(Event).where(Event.id == UUID(event_id)))
    ).scalar_one()
    event.tombstoned_at = utcnow() - timedelta(days=31)
    await db_session.commit()

    resp = await client.post(
        "/v1/maintenance/purge-tombstoned-blobs",
        json={"older_than_days": 30},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["events_eligible"] == 1
    assert payload["blobs_deleted"] == 1
    assert payload["storage_keys"] == [attachment["storage_key"]]

    row = (
        await db_session.execute(
            select(Attachment).where(Attachment.id == UUID(attachment["id"]))
        )
    ).scalar_one_or_none()
    assert row is None

    # The tombstoned event (audit trail) survives.
    resp = await client.get(f"/v1/events/{event_id}")
    assert resp.status_code == 200
    assert resp.json()["tombstoned_at"] is not None


async def test_prune_backups_keeps_newest(client, tmp_path: Path) -> None:
    for index in range(3):
        resp = await client.post(
            "/v1/backup",
            json={
                "passphrase": "correct-horse-battery-staple-42",
                "destination": str(tmp_path / f"ev-backup-2026080{index}.evbackup"),
            },
        )
        assert resp.status_code == 201, resp.text
        path = tmp_path / f"ev-backup-2026080{index}.evbackup"
        os.utime(path, (1_700_000_000 + index * 60, 1_700_000_000 + index * 60))

    result = prune_backups(directory=str(tmp_path), keep=2)
    assert result["kept"] == 2
    assert len(result["removed"]) == 1
    remaining = sorted(p.name for p in tmp_path.glob("*.evbackup"))
    assert len(remaining) == 2
    assert "ev-backup-20260800.evbackup" not in remaining
