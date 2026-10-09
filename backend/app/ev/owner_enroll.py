"""Owner contact enrollment: explicit owner-provided contact facts.

A single ``owner.enrollment`` event captures the enrollment action; one
``fact`` memory per contact field carries the value with explicit provenance.
Contact values live in the fact memories (and the owner's mouth), never in
the enrollment event text.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import MemoryCandidate
from app.models import Memory, OwnerIdentity
from app.schemas import EventCreate
from app.services.event_service import EventService


async def enroll_owner_contact(
    session: AsyncSession,
    *,
    display_name: str,
    primary_phone: str,
    emails: dict[str, str],
    actor: str = "owner",
) -> dict[str, Any]:
    """Enroll one owner's contact details. Flushes; the caller commits.

    Creates ONE enrollment event (source ``owner``) plus one explicit fact
    memory per contact field. Fills the blank ``owner_identities`` row when
    the table is empty; never modifies an existing row.
    """
    clean_emails = {
        slot: address
        for slot, address in emails.items()
        if (address or "").strip()
    }
    contact_fields = ["primary_phone"] + [f"email_{slot}" for slot in clean_emails]
    event = await EventService(session, actor=actor).create(
        EventCreate(
            source="owner",
            event_type="owner.enrollment",
            text="Owner contact enrollment",
            metadata={"contact_fields": contact_fields, "display_name": display_name},
        )
    )

    # Mirror process_event_sync writer usage: MemoryWriter + shared embedder.
    from app.embeddings import get_embedder
    from app.memory.writer import MemoryWriter

    items: list[tuple[str, str, str]] = [
        ("primary_phone", primary_phone, f"Owner primary phone number: {primary_phone}")
    ]
    items.extend(
        (
            f"email_{slot}",
            address,
            f"Owner {slot} email: {address}",
        )
        for slot, address in clean_emails.items()
    )
    candidates = [
        MemoryCandidate(
            memory_type="fact",
            text=text,
            payload={
                "subject": "owner",
                "property": field,
                "value": value,
                "enrolled_by": "owner",
                "contact_field": field,
                "evidence_type": "owner_enrolled",
            },
            importance=0.9,
            confidence=1.0,
            source_type="explicit",
            privacy_level="normal",
        )
        for field, value, text in items
    ]
    writer = MemoryWriter(session, embeddings=get_embedder())
    results = await writer.write_all(event, candidates)
    # write_all re-scores importance by type; owner-stated facts pin to 0.9.
    memory_ids = [result.memory_id for result in results]
    if memory_ids:
        rows = (
            await session.execute(
                select(Memory).where(Memory.id.in_([UUID(mid) for mid in memory_ids]))
            )
        ).scalars().all()
        for row in rows:
            row.importance = 0.9
        await session.flush()

    existing = (
        await session.execute(select(OwnerIdentity).limit(1))
    ).scalars().first()
    display_name_set = False
    if existing is None:
        session.add(OwnerIdentity(display_name=display_name))
        display_name_set = True
    await session.flush()
    return {
        "event_id": str(event.id),
        "memory_ids": memory_ids,
        "display_name_set": display_name_set,
    }
