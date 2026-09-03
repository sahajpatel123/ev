"""Write classified archive items as immutable Events.

The archive stays on the locator path. Nothing here is derived into Memories,
so 15k contacts/photos/mail cannot fill the realtime token budget or the
general retriever window.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.life_archive.classify import CatalogRecord
from app.memory.life_archive.locate import reset_people_cache
from app.memory.life_archive.parse import ParsedItem, parse_record
from app.models import Event
from app.schemas import EventCreate, PrivacyLevel
from app.services.event_service import EventService
from app.utils.text import sha256_hex, utcnow

SOURCE = "life_archive"
_BATCH = 200


async def ingest_records(
    session: AsyncSession,
    *,
    root: Path,
    records: Iterable[CatalogRecord],
    include: frozenset[str] | None = None,
    actor: str = "owner",
) -> dict[str, Any]:
    allowed = include or frozenset({"ingest", "index"})
    root = root.expanduser().resolve()
    created = 0
    skipped_existing = 0
    skipped_disposition = 0
    skipped_parse = 0
    retired_photos = 0
    service = EventService(session, actor=actor)
    packed = list(records)
    if any(record.adapter == "photos_meta" and record.disposition in allowed for record in packed):
        retired_photos = await _retire_file_photo_pointers(session)
    reset_people_cache()

    for record in packed:
        if record.disposition not in allowed:
            skipped_disposition += 1
            continue
        if record.disposition in {"skip", "quarantine"}:
            skipped_disposition += 1
            continue
        items = parse_record(root, record.rel, record.adapter)
        if not items:
            skipped_parse += 1
            continue
        for item in items:
            event, status = await _write_item(session, service, item)
            if item.event_type == "life.person":
                await _link_person(session, item)
            if status == "existing":
                skipped_existing += 1
                continue
            created += 1
            if created % _BATCH == 0:
                await session.commit()

    await session.commit()
    locator = {}
    try:
        from app.memory.life_archive.locate import rebuild_locator

        locator = await rebuild_locator(session)
    except Exception:  # noqa: BLE001 - locator is an accelerator, never blocks ingest
        locator = {"error": "rebuild_failed"}
    return {
        "created": created,
        "skipped_existing": skipped_existing,
        "skipped_disposition": skipped_disposition,
        "skipped_parse": skipped_parse,
        "retired_photos": retired_photos,
        "extracted": 0,
        "locator": {key: locator.get(key) for key in ("total", "shelves") if key in locator},
    }


async def _retire_file_photo_pointers(session: AsyncSession) -> int:
    """Tombstone undated Apple file pointers once Photo Details.csv is the aisle."""
    rows = (
        await session.execute(
            select(Event).where(
                Event.source == SOURCE,
                Event.event_type == "life.photo.index",
                Event.tombstoned_at.is_(None),
            )
        )
    ).scalars().all()
    now = utcnow()
    retired = 0
    for event in rows:
        key = str((event.metadata_ or {}).get("life_archive_key") or "")
        if "icloud photos" not in key.lower():
            continue
        if key.startswith("photo:details:"):
            continue
        event.tombstoned_at = now
        event.tombstone_reason = "replaced_by_photo_details_csv"
        retired += 1
    return retired


async def _write_item(
    session: AsyncSession,
    service: EventService,
    item: ParsedItem,
) -> tuple[Event | None, str]:
    key = sha256_hex(f"life_archive:{item.item_key}")
    existing = (
        await session.execute(select(Event).where(Event.idempotency_key_hash == key))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, "existing"
    event = await service.create(
        EventCreate(
            source=SOURCE,
            event_type=item.event_type,
            text=item.text,
            content=item.content,
            metadata={"life_archive_key": item.item_key, "ref": item.content.get("ref")},
            occurred_at=item.occurred_at,
            privacy_level=_privacy(item.privacy_level),
        ),
        idempotency_key=f"life_archive:{item.item_key}",
    )
    return event, "created"


async def _link_person(session: AsyncSession, item: ParsedItem) -> None:
    """Put WhatsApp counterparts on the people graph. Never enrolls a face."""
    name = str((item.content or {}).get("name") or "").strip()
    if not name:
        return
    relation = str((item.content or {}).get("relation") or "friend")
    aliases = [str(alias) for alias in (item.content or {}).get("aliases") or [] if alias]
    try:
        from app.life.people import ensure_person, set_relationship

        await ensure_person(session, name=name, aliases=aliases, summary=item.text[:240])
        await set_relationship(
            session,
            actor="owner",
            person_name=name,
            relation=relation,
            note=item.text[:240],
        )
    except Exception:  # noqa: BLE001 - aisle ingest must not fail on graph edges
        return


def _privacy(value: str) -> PrivacyLevel:
    if value in {"private", "normal", "sensitive", "never_send_to_model"}:
        return value  # type: ignore[return-value]
    return "normal"
