"""Biometric erasure for the AGENT 7 ROSTER people subsystem.

Erasure destroys face templates, recognition sightings, and cached public
biodata while keeping auditable enrollment rows (redacted, without ciphertext)
so the event is explainable and backup purge jobs can find the manifest.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance.models import DataErasureRecord
from app.models import FaceEnrollment, FaceSample, PublicFigureCache, RecognitionLog
from app.services.access_log import log_access
from app.utils.text import utcnow


async def erase_person(
    session: AsyncSession,
    *,
    entity_id: UUID,
    reason: str,
    actor: str,
) -> dict:
    """Erase every face biometric and sighting belonging to one person."""
    recognition_ids = list(
        (
            await session.execute(
                select(RecognitionLog.id).where(RecognitionLog.entity_id == entity_id)
            )
        )
        .scalars()
        .all()
    )
    if recognition_ids:
        await session.execute(
            delete(RecognitionLog).where(RecognitionLog.id.in_(recognition_ids))
        )

    sample_ids = list(
        (
            await session.execute(
                select(FaceSample.id).where(FaceSample.entity_id == entity_id)
            )
        )
        .scalars()
        .all()
    )
    if sample_ids:
        await session.execute(
            delete(FaceSample).where(FaceSample.id.in_(sample_ids))
        )

    enrollments = list(
        (
            await session.execute(
                select(FaceEnrollment).where(FaceEnrollment.entity_id == entity_id)
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    for row in enrollments:
        row.status = "deleted"
        row.ciphertext = None
        row.salt = None
        row.redacted = True
        row.is_current = False
        row.reason_for_change = reason
        row.updated_at = now

    cache_ids = list(
        (
            await session.execute(
                select(PublicFigureCache.id).where(
                    PublicFigureCache.entity_id == entity_id
                )
            )
        )
        .scalars()
        .all()
    )
    if cache_ids:
        await session.execute(
            delete(PublicFigureCache).where(PublicFigureCache.id.in_(cache_ids))
        )

    manifest = {
        "reason": reason,
        "recognition_logs_deleted": len(recognition_ids),
        "face_samples_deleted": len(sample_ids),
        "face_enrollments_processed": len(enrollments),
        "face_enrollment_ids": [str(row.id) for row in enrollments],
        "public_figure_cache_deleted": len(cache_ids),
        "backup_purge_required": bool(enrollments),
    }
    session.add(DataErasureRecord(actor=actor, reason=reason, manifest=manifest))
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="people_erasure",
        endpoint="DELETE /v1/people/{id}",
        resource_type="person",
        resource_ids=[entity_id],
        details=manifest,
    )
    return manifest


async def erase_all_face_biometrics(
    session: AsyncSession,
    *,
    reason: str,
    actor: str,
) -> dict:
    """Erase all face biometrics across every enrolled person.

    This is the hook Agent 19 (compliance) will call from the biometric
    erasure sweep. Enrollment audit rows are kept but redacted and stripped of
    ciphertext; samples and sighting logs are physically deleted.
    """
    enrollments = list(
        (await session.execute(select(FaceEnrollment))).scalars().all()
    )
    enrollment_ids = [str(row.id) for row in enrollments]

    sample_ids = list(
        (await session.execute(select(FaceSample.id))).scalars().all()
    )
    if sample_ids:
        await session.execute(
            delete(FaceSample).where(FaceSample.id.in_(sample_ids))
        )

    recognition_ids = list(
        (
            await session.execute(
                select(RecognitionLog.id).where(
                    RecognitionLog.source.in_(("model", "face")),
                    RecognitionLog.label.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if recognition_ids:
        await session.execute(
            delete(RecognitionLog).where(RecognitionLog.id.in_(recognition_ids))
        )

    cache_ids = list(
        (
            await session.execute(
                select(PublicFigureCache.id).where(
                    PublicFigureCache.entity_id.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    if cache_ids:
        await session.execute(
            delete(PublicFigureCache).where(PublicFigureCache.id.in_(cache_ids))
        )

    now = utcnow()
    for row in enrollments:
        row.status = "deleted"
        row.ciphertext = None
        row.salt = None
        row.redacted = True
        row.is_current = False
        row.reason_for_change = reason
        row.updated_at = now

    manifest = {
        "reason": reason,
        "face_samples_deleted": len(sample_ids),
        "recognition_logs_deleted": len(recognition_ids),
        "public_figure_cache_deleted": len(cache_ids),
        "face_enrollments_processed": len(enrollments),
        "face_enrollment_ids": enrollment_ids,
        "backup_purge_required": bool(enrollments),
    }
    session.add(DataErasureRecord(actor=actor, reason=reason, manifest=manifest))
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="people_biometrics_erasure",
        endpoint="compliance:biometric_erasure_sweep",
        resource_type="face_biometrics",
        resource_ids=[UUID(value) for value in enrollment_ids],
        details=manifest,
    )
    return manifest
