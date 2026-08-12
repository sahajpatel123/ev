"""Consent-gated face enrollment for AGENT 7 ROSTER.

Templates are Fernet-encrypted at rest (mirroring the voiceprint design):
the mean embedding and every per-sample embedding are ciphertext + salt in
the database, and cosine matching happens in-process after decryption. No
template is ever written without an active ``face_enrollment`` consent record.
"""

from __future__ import annotations

import math
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory.entities import get_or_create_entity
from app.models import Entity, FaceEnrollment, FaceSample
from app.people.errors import FaceError
from app.people.face_embed import FaceCrop, FaceEmbedder, get_face_embedder
from app.training.consent import ConsentRequiredError, require_consent
from app.utils.text import utcnow
from app.voice.security import decrypt_payload, encrypt_payload


def _normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def _mean(embeddings: list[list[float]]) -> list[float]:
    if not embeddings:
        raise FaceError("No sample embeddings produced", status=422, code="enroll_embedding")
    dim = len(embeddings[0])
    mean = [0.0] * dim
    for embedding in embeddings:
        if len(embedding) != dim:
            raise FaceError(
                "Inconsistent embedding dimensions across samples",
                status=422,
                code="enroll_embedding",
            )
        for index, value in enumerate(embedding):
            mean[index] += value
    return _normalize([value / len(embeddings) for value in mean])


def _optional_uuid(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError as exc:
        raise FaceError(
            f"Invalid UUID reference: {value}",
            status=400,
            code="invalid_reference",
        ) from exc


class FaceEnrollmentService:
    """Enrollment/versioning/revocation/decryption for consented face templates."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        master_key: str,
        embedder: FaceEmbedder | None = None,
    ) -> None:
        self.session = session
        self.master_key = master_key
        self.embedder = embedder or get_face_embedder()

    async def enroll(
        self,
        *,
        person_name: str,
        photos: list[FaceCrop],
        reason: str | None = None,
    ) -> FaceEnrollment:
        """Enroll one person from >=5 aligned, quality-gated crops."""
        try:
            consent = await require_consent(self.session, "face_enrollment")
        except ConsentRequiredError as exc:
            raise FaceError(
                "Face enrollment requires active consent; grant face_enrollment consent first",
                status=403,
                code="consent_required",
            ) from exc

        if len(photos) < settings.face_min_photos:
            raise FaceError(
                f"Enrollment requires at least {settings.face_min_photos} photos",
                status=422,
                code="enroll_samples",
            )

        results: list = []
        for index, photo in enumerate(photos, start=1):
            if photo.quality is None or photo.confidence is None:
                raise FaceError(
                    f"Enrollment photo {index} is missing detector quality/confidence",
                    status=422,
                    code="enroll_quality",
                )
            if photo.quality < settings.face_quality_floor:
                raise FaceError(
                    f"Enrollment photo {index} quality {photo.quality:.3f} is below "
                    f"floor {settings.face_quality_floor}",
                    status=422,
                    code="enroll_quality",
                )
            if photo.confidence < settings.face_confidence_floor:
                raise FaceError(
                    f"Enrollment photo {index} confidence {photo.confidence:.3f} is below "
                    f"floor {settings.face_confidence_floor}",
                    status=422,
                    code="enroll_confidence",
                )
            results.append(await self.embedder.embed(photo))

        entity = await get_or_create_entity(self.session, person_name, "person")
        embeddings = [result.embedding for result in results]
        mean_embedding = _mean(embeddings)
        payload = {
            "algorithm": self.embedder.name,
            "embedding": mean_embedding,
            "dim": self.embedder.embedding_dim,
            "threshold": settings.face_threshold,
            "sample_count": len(photos),
            "provider": self.embedder.name,
            "degraded": self.embedder.degraded,
            "calibrated": False,
            "calibrated_at": None,
        }
        token, salt_hex = encrypt_payload(payload, master_key=self.master_key)

        previous = (
            await self.session.execute(
                select(FaceEnrollment).where(
                    FaceEnrollment.entity_id == entity.id,
                    FaceEnrollment.is_current.is_(True),
                    FaceEnrollment.status == "active",
                    FaceEnrollment.redacted.is_(False),
                )
            )
        ).scalar_one_or_none()
        version = (previous.version + 1) if previous is not None else 1

        enrollment = FaceEnrollment(
            entity_id=entity.id,
            version=version,
            is_current=True,
            algorithm=self.embedder.name,
            embedding_dim=self.embedder.embedding_dim,
            threshold=payload["threshold"],
            sample_count=len(photos),
            status="active",
            consent_id=consent.id,
            ciphertext=token,
            salt=salt_hex,
            privacy_level="sensitive",
            redacted=False,
            supersedes_id=previous.id if previous is not None else None,
            reason_for_change=reason,
        )
        self.session.add(enrollment)
        await self.session.flush()
        if previous is not None:
            previous.is_current = False
            previous.superseded_by_id = enrollment.id

        for index, (photo, result) in enumerate(zip(photos, results, strict=True)):
            sample_payload = {
                "embedding": result.embedding,
                "index": index,
                "algorithm": self.embedder.name,
                "provider": self.embedder.name,
                "degraded": self.embedder.degraded,
            }
            sample_token, sample_salt = encrypt_payload(
                sample_payload,
                master_key=self.master_key,
            )
            self.session.add(
                FaceSample(
                    enrollment_id=enrollment.id,
                    entity_id=entity.id,
                    sample_index=index,
                    ciphertext=sample_token,
                    salt=sample_salt,
                    quality=photo.quality if photo.quality is not None else 0.0,
                    confidence=photo.confidence if photo.confidence is not None else 0.0,
                    source=photo.source,
                    attachment_id=_optional_uuid(photo.attachment_id),
                    live_event_id=_optional_uuid(photo.live_event_id),
                )
            )
        await self.session.flush()
        return enrollment

    async def list_enrollments(
        self,
        entity_id: UUID | None = None,
    ) -> list[FaceEnrollment]:
        stmt = select(FaceEnrollment).order_by(FaceEnrollment.created_at.desc())
        if entity_id is not None:
            stmt = stmt.where(FaceEnrollment.entity_id == entity_id)
        rows = (await self.session.execute(stmt)).scalars().all()
        return list(rows)

    async def current_payloads(self) -> list[tuple[Entity, dict]]:
        """Active current enrollments with their decrypted mean templates."""
        rows = (
            await self.session.execute(
                select(FaceEnrollment, Entity)
                .join(Entity, Entity.id == FaceEnrollment.entity_id)
                .where(
                    FaceEnrollment.is_current.is_(True),
                    FaceEnrollment.status == "active",
                    FaceEnrollment.redacted.is_(False),
                )
            )
        ).all()
        result: list[tuple[Entity, dict]] = []
        for enrollment, entity in rows:
            result.append((entity, await self.decrypt_enrollment(enrollment)))
        return result

    async def decrypt_enrollment(self, enrollment: FaceEnrollment) -> dict:
        if not enrollment.ciphertext or not enrollment.salt:
            raise FaceError(
                "Face template payload is missing (revoked or deleted)",
                status=409,
                code="enrollment_unavailable",
            )
        try:
            return decrypt_payload(
                enrollment.ciphertext,
                enrollment.salt,
                master_key=self.master_key,
            )
        except ValueError as exc:
            raise FaceError(
                "Face template decryption failed: bad key, salt, or ciphertext",
                status=500,
                code="face_decrypt_failed",
            ) from exc

    async def revoke(self, enrollment_id: UUID, reason: str) -> FaceEnrollment:
        row = await self.session.get(FaceEnrollment, enrollment_id)
        if row is None:
            raise FaceError(
                "Face enrollment not found",
                status=404,
                code="face_enrollment_not_found",
            )
        row.status = "revoked"
        row.is_current = False
        row.reason_for_change = reason
        row.updated_at = utcnow()
        await self.session.flush()
        return row

    async def delete(self, enrollment_id: UUID, reason: str) -> FaceEnrollment:
        row = await self.session.get(FaceEnrollment, enrollment_id)
        if row is None:
            raise FaceError(
                "Face enrollment not found",
                status=404,
                code="face_enrollment_not_found",
            )
        await self.session.execute(
            delete(FaceSample).where(FaceSample.enrollment_id == enrollment_id)
        )
        row.status = "deleted"
        row.ciphertext = None
        row.salt = None
        row.redacted = True
        row.is_current = False
        row.reason_for_change = reason
        row.updated_at = utcnow()
        await self.session.flush()
        return row
