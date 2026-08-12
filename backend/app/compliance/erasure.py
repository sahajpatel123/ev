"""Data-subject erasure and retention enforcement for biometric data."""

from __future__ import annotations

import contextlib
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.models import DataErasureRecord
from app.config import settings
from app.memory.writer import redact_memories_for_event
from app.models import (
    AccessLog,
    AdapterRegistration,
    Attachment,
    Event,
    FaceEnrollment,
    FaceSample,
    FilterLedger,
    FilterRecalibration,
    ModelCallLog,
    PersonalizationCalibration,
    PublicFigureCache,
    RecognitionLog,
    VoiceEnrollment,
)
from app.services.access_log import log_access
from app.services.event_service import EventService
from app.storage.object_store import get_object_store
from app.training import consent as consent_service
from app.training import corpus as corpus_service
from app.voice.lifecycle import VoiceRuntime

from .policy import ACCESS_LOG, FACEPRINT, VOICEPRINT, deletion_due, policy_summary

ERASURE_TRACKS = (
    "voice_enrollment",
    "face_enrollment",
    "training_corpus",
    "life_data_personalization",
    "adapter_fine_tuning",
    "filter_self_improvement",
    "chat_egress",
)


async def erase_biometric_data(
    session: AsyncSession, *, reason: str, actor: str
) -> dict:
    """Revoke biometric/personalization consent, destroy voice and face
    templates, redact derived adapters/calibrations/policies, tombstone voice
    events, physically remove audio blobs, and record an auditable manifest.
    """
    runtime = VoiceRuntime(session, master_key=settings.master_key)

    consents_revoked = 0
    for track in ERASURE_TRACKS:
        row = await consent_service.revoke_consent(
            session, track=track, reason=f"data subject erasure: {reason}"
        )
        if row is not None:
            consents_revoked += 1

    corpus_snapshots_redacted = await corpus_service.delete_all(
        session, actor=actor, reason=f"data subject erasure: {reason}"
    )

    enrollments = list(
        (await session.execute(select(VoiceEnrollment))).scalars().all()
    )
    enrollment_ids = [str(row.id) for row in enrollments]
    for enrollment in enrollments:
        await runtime.delete(enrollment.id, reason=f"data subject erasure: {reason}")

    # Face templates, per-sample templates, sightings, and cached biodata
    # (AGENT 7 ROSTER). Audit-worthy rows are redacted in place; per-sample
    # templates and recognition sightings are destroyed outright.
    face_enrollments = list(
        (await session.execute(select(FaceEnrollment))).scalars().all()
    )
    face_enrollment_ids = [str(row.id) for row in face_enrollments]
    for face_enrollment in face_enrollments:
        face_enrollment.status = "deleted"
        face_enrollment.redacted = True
        face_enrollment.ciphertext = None
        face_enrollment.salt = None
        face_enrollment.sample_count = 0
        face_enrollment.reason_for_change = f"data subject erasure: {reason}"
    face_samples_deleted = 0
    if face_enrollments:
        face_sample_rows = list(
            (await session.execute(select(FaceSample))).scalars().all()
        )
        face_samples_deleted = len(face_sample_rows)
        if face_sample_rows:
            await session.execute(
                delete(FaceSample).where(
                    FaceSample.id.in_([row.id for row in face_sample_rows])
                )
            )

    recognition_sightings = list(
        (await session.execute(select(RecognitionLog))).scalars().all()
    )
    recognition_sightings_deleted = len(recognition_sightings)
    if recognition_sightings:
        await session.execute(
            delete(RecognitionLog).where(
                RecognitionLog.id.in_([row.id for row in recognition_sightings])
            )
        )
    biodata_cache_rows = list(
        (await session.execute(select(PublicFigureCache))).scalars().all()
    )
    biodata_cache_deleted = len(biodata_cache_rows)
    if biodata_cache_rows:
        await session.execute(
            delete(PublicFigureCache).where(
                PublicFigureCache.id.in_([row.id for row in biodata_cache_rows])
            )
        )

    # Derived personal data (AGENT 11 FORGE): adapter style statistics,
    # retrieval calibrations, and filter policies are derived from the erased
    # corpus, so the derived content is destroyed even though audit rows stay.
    adapters = list(
        (await session.execute(select(AdapterRegistration))).scalars().all()
    )
    for adapter in adapters:
        adapter.redacted = True
        adapter.eval_metrics = {}
        adapter.adapter_ref = None
        adapter.reason_for_change = f"data subject erasure: {reason}"
    calibrations = list(
        (await session.execute(select(PersonalizationCalibration))).scalars().all()
    )
    for calibration in calibrations:
        calibration.is_current = False
        calibration.calibrations = {}
        calibration.evidence = {}
        calibration.reason_for_change = f"data subject erasure: {reason}"
    recalibrations = list(
        (await session.execute(select(FilterRecalibration))).scalars().all()
    )
    for recalibration in recalibrations:
        recalibration.redacted = True
        recalibration.metrics = {}
        recalibration.proposals = []
        recalibration.policy = {}
        recalibration.reason_for_change = f"data subject erasure: {reason}"

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

    voice_conversation_ids = {
        event.conversation_id for event in voice_events if event.conversation_id is not None
    }
    filter_ledger_redacted = 0
    model_call_envelopes_redacted = 0
    if voice_conversation_ids:
        ledger_rows = list(
            (
                await session.execute(
                    select(FilterLedger).where(
                        FilterLedger.conversation_id.in_(voice_conversation_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        request_ids = {row.request_id for row in ledger_rows if row.request_id}
        for ledger_row in ledger_rows:
            ledger_row.draft = None
            ledger_row.final_text = None
            ledger_row.scores = None
            ledger_row.detail = {
                "redacted": True,
                "reason": f"data subject erasure: {reason}",
            }
            filter_ledger_redacted += 1
        if request_ids:
            calls = list(
                (
                    await session.execute(
                        select(ModelCallLog).where(
                            ModelCallLog.request_id.in_(request_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for call in calls:
                call.envelope = {
                    "redacted": True,
                    "reason": f"data subject erasure: {reason}",
                }
                call.tool_calls = []
                model_call_envelopes_redacted += 1

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
        "face_enrollments_processed": len(face_enrollments),
        "face_enrollment_ids": face_enrollment_ids,
        "face_samples_deleted": face_samples_deleted,
        "recognition_sightings_deleted": recognition_sightings_deleted,
        "biodata_cache_deleted": biodata_cache_deleted,
        "adapter_registrations_redacted": len(adapters),
        "personalization_calibrations_redacted": len(calibrations),
        "filter_recalibrations_redacted": len(recalibrations),
        "corpus_snapshots_redacted": corpus_snapshots_redacted,
        "filter_ledger_redacted": filter_ledger_redacted,
        "model_call_envelopes_redacted": model_call_envelopes_redacted,
        "events_tombstoned": len(voice_events),
        "memories_redacted": memories_redacted,
        "attachments_deleted": attachments_deleted,
        "storage_keys": storage_keys,
        "backup_purge_required": bool(
            storage_keys or enrollments or face_enrollments
        ),
        "covered_tables": [
            "voice_enrollments",
            "voice_prints",
            "face_enrollments",
            "face_samples",
            "recognition_log",
            "public_figure_cache",
            "personalization_calibrations",
            "filter_recalibrations",
            "adapter_registrations",
            "training_corpus_snapshots",
            "filter_ledger",
            "model_call_log",
            "events",
            "attachments",
            "consent_records",
        ],
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
        resource_ids=[
            UUID(value) for value in enrollment_ids + face_enrollment_ids
        ],
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
    """Delete voice/face enrollments whose configured retention window expired."""
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
    corpus_deleted = await corpus_service.delete_due_snapshots(session, now=now, reason=reason)
    face_enrollments = list(
        (
            await session.execute(
                select(FaceEnrollment).where(
                    FaceEnrollment.status.in_(("active", "revoked")),
                    FaceEnrollment.redacted.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )
    face_deleted_ids: list[str] = []
    for face_enrollment in face_enrollments:
        reference = getattr(face_enrollment, "revoked_at", None) or face_enrollment.created_at
        if deletion_due(FACEPRINT, reference, now=now):
            face_enrollment.status = "deleted"
            face_enrollment.redacted = True
            face_enrollment.ciphertext = None
            face_enrollment.salt = None
            face_enrollment.sample_count = 0
            face_deleted_ids.append(str(face_enrollment.id))
    if face_deleted_ids:
        samples = list(
            (
                await session.execute(
                    select(FaceSample).where(
                        FaceSample.enrollment_id.in_(
                            [UUID(value) for value in face_deleted_ids]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        if samples:
            await session.execute(
                delete(FaceSample).where(
                    FaceSample.id.in_([row.id for row in samples])
                )
            )
    access_rows = list((await session.execute(select(AccessLog))).scalars().all())
    stale_access = [
        row for row in access_rows if deletion_due(ACCESS_LOG, row.occurred_at, now=now)
    ]
    access_logs_deleted = 0
    if stale_access:
        await log_access(
            session,
            actor=actor,
            action="retention",
            endpoint="POST /v1/compliance/retention/sweep",
            resource_type="access_log",
            resource_ids=[row.id for row in stale_access],
            details={"deleted": len(stale_access)},
        )
        await session.execute(
            delete(AccessLog).where(AccessLog.id.in_([row.id for row in stale_access]))
        )
        access_logs_deleted = len(stale_access)
    summary = policy_summary()
    return {
        "voiceprints_deleted": len(deleted_ids),
        "enrollment_ids": deleted_ids,
        "faceprints_deleted": len(face_deleted_ids),
        "face_enrollment_ids": face_deleted_ids,
        "corpus_snapshots_redacted": corpus_deleted,
        "access_logs_deleted": access_logs_deleted,
        "policy_retention_days": summary["retention_days"][VOICEPRINT],
    }
