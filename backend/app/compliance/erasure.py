"""Data-subject erasure and retention enforcement for biometric data."""

from __future__ import annotations

import contextlib
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.models import DataErasureRecord
from app.config import settings
from app.memory.writer import redact_memories_for_event
from app.models import Attachment, Event, VoiceEnrollment
from app.services.access_log import log_access
from app.services.event_service import EventService
from app.storage.object_store import get_object_store
from app.training import consent as consent_service
from app.voice.lifecycle import VoiceRuntime

from .policy import VOICEPRINT, deletion_due, policy_summary

BIOMETRIC_TRACKS = ("voice_enrollment", "training_corpus")


async def erase_biometric_data(
    session: AsyncSession, *, reason: str, actor: str
) -> dict:
    """Revoke biometric consent, delete voiceprints, tombstone voice events,
    physically remove audio blobs, and record an auditable erasure manifest.
    """
    runtime = VoiceRuntime(session, master_key=settings.master_key)

    consents_revoked = 0
    for track in BIOMETRIC_TRACKS:
        row = await consent_service.revoke_consent(
            session, track=track, reason=f"data subject erasure: {reason}"
        )
        if row is not None:
            consents_revoked += 1

    enrollments = list(
        (await session.execute(select(VoiceEnrollment))).scalars().all()
    )
    enrollment_ids = [str(row.id) for row in enrollments]
    for enrollment in enrollments:
        await runtime.delete(enrollment.id, reason=f"data subject erasure: {reason}")

    voice_events = list(
        (
            await session.execute(
                select(Event).where(
                    Event.source == "voice", Event.tombstoned_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    event_service = EventService(session, actor=actor)
    memories_redacted = 0
    for event in voice_events:
        await event_service.tombstone(event.id, f"data subject erasure: {reason}")
        memories_redacted += await redact_memories_for_event(session, event.id)

    storage_keys: list[str] = []
    attachments_deleted = 0
    if voice_events:
        event_ids = [event.id for event in voice_events]
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
        for attachment in attachments:
            storage_keys.append(attachment.storage_key)
            with contextlib.suppress(Exception):
                await store.delete(attachment.storage_key)
            await session.delete(attachment)
            attachments_deleted += 1

    manifest = {
        "reason": reason,
        "consents_revoked": consents_revoked,
        "enrollments_processed": len(enrollments),
        "enrollment_ids": enrollment_ids,
        "events_tombstoned": len(voice_events),
        "memories_redacted": memories_redacted,
        "attachments_deleted": attachments_deleted,
        "storage_keys": storage_keys,
        "backup_purge_required": bool(storage_keys or enrollments),
    }
    session.add(
        DataErasureRecord(actor=actor, reason=reason, manifest=manifest)
    )
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="data_erasure",
        endpoint="POST /v1/compliance/erasure",
        resource_type="biometric_data",
        resource_ids=[UUID(value) for value in enrollment_ids],
        details=manifest,
    )
    return manifest


async def retention_sweep(
    session: AsyncSession,
    *,
    reason: str = "retention policy",
    actor: str = "compliance",
    now: datetime | None = None,
) -> dict:
    """Delete voice enrollments whose configured retention window has expired."""
    runtime = VoiceRuntime(session, master_key=settings.master_key)
    enrollments = list(
        (
            await session.execute(
                select(VoiceEnrollment).where(
                    VoiceEnrollment.status.in_(("active", "revoked"))
                )
            )
        )
        .scalars()
        .all()
    )
    deleted_ids: list[str] = []
    for enrollment in enrollments:
        reference = getattr(enrollment, "revoked_at", None) or enrollment.created_at
        if deletion_due(VOICEPRINT, reference, now=now):
            await runtime.delete(enrollment.id, reason=reason)
            deleted_ids.append(str(enrollment.id))
    summary = policy_summary()
    return {
        "voiceprints_deleted": len(deleted_ids),
        "enrollment_ids": deleted_ids,
        "policy_retention_days": summary["retention_days"][VOICEPRINT],
    }
