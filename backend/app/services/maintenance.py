"""Operational maintenance: tombstoned-blob purge and backup retention."""

from __future__ import annotations

import contextlib
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Attachment, Event
from app.services.access_log import log_access
from app.storage.object_store import get_object_store
from app.utils.text import utcnow


async def purge_tombstoned_blobs(
    session: AsyncSession,
    *,
    older_than_days: int | None = None,
    actor: str = "maintenance",
) -> dict:
    """Delete blobs whose source event was tombstoned beyond the audit window.

    The event row (with its tombstone audit trail) is preserved; only the
    attachment metadata and its object-store blob are removed.
    """
    days = older_than_days or settings.tombstone_blob_retention_days
    if days < 0:
        raise ValueError("older_than_days must be >= 0")
    cutoff = utcnow() - timedelta(days=days)
    events = list(
        (
            await session.execute(
                select(Event).where(
                    Event.tombstoned_at.is_not(None),
                    Event.tombstoned_at <= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    event_ids = [event.id for event in events]
    if not event_ids:
        return {"events_eligible": 0, "blobs_deleted": 0, "storage_keys": []}

    attachments = list(
        (
            await session.execute(
                select(Attachment).where(Attachment.event_id.in_(event_ids))
            )
        )
        .scalars()
        .all()
    )
    store = get_object_store()
    storage_keys: list[str] = []
    blobs_deleted = 0
    for attachment in attachments:
        storage_keys.append(attachment.storage_key)
        with contextlib.suppress(Exception):
            await store.delete(attachment.storage_key)
            blobs_deleted += 1
    await session.execute(delete(Attachment).where(Attachment.event_id.in_(event_ids)))
    await log_access(
        session,
        actor=actor,
        action="blob_purge",
        endpoint="POST /v1/maintenance/purge-tombstoned-blobs",
        resource_type="attachment",
        resource_ids=[],
        details={
            "events_eligible": len(event_ids),
            "blobs_deleted": blobs_deleted,
            "older_than_days": days,
        },
    )
    return {
        "events_eligible": len(event_ids),
        "blobs_deleted": blobs_deleted,
        "storage_keys": storage_keys,
    }


def prune_backups(
    *,
    directory: str | None = None,
    keep: int | None = None,
) -> dict:
    """Keep the newest ``keep`` backup files, removing the rest."""
    folder = Path(directory or (Path(settings.storage_root) / "backups"))
    count = keep if keep is not None else settings.backup_retention_count
    if count < 0:
        raise ValueError("keep must be >= 0")
    if not folder.exists():
        return {"kept": 0, "removed": []}
    files = sorted(
        folder.glob("ev-backup-*.evbackup"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed = files[count:]
    for path in removed:
        path.unlink(missing_ok=True)
    return {
        "kept": len(files) - len(removed),
        "removed": [path.name for path in removed],
    }
