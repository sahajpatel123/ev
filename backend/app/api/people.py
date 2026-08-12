"""AGENT 7 ROSTER API: consented face enrollment, recognition, biodata, erasure."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext, require_actor, require_owner_trust
from app.config import settings
from app.db import get_session
from app.models import Entity, FaceEnrollment
from app.people.biodata import BiodataError, BiodataResolver
from app.people.calibration import apply_threshold, calibrate
from app.people.enrollment import FaceEnrollmentService
from app.people.erasure import erase_person
from app.people.errors import FaceError
from app.people.face_embed import FaceCrop
from app.people.resolver import FaceResolver
from app.schemas import (
    FaceCalibrationReport,
    FaceCalibrationRequest,
    FaceEnrollmentCreate,
    FaceEnrollmentDetailOut,
    FaceEnrollResponse,
    FaceRecognitionConfirmOut,
    FaceRecognitionConfirmRequest,
    FaceRecognitionRequest,
    FaceRecognitionResponse,
    PublicFigureBiodataOut,
    PublicFigureLinkRequest,
)

router = APIRouter(prefix="/v1/people", tags=["people"])


def _face_http(exc: FaceError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail=exc.message,
        headers={"X-Error-Code": exc.code},
    )


def _biodata_http(exc: BiodataError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail=exc.message,
        headers={"X-Error-Code": exc.code},
    )


async def _enrollment_out(session: AsyncSession, row: FaceEnrollment) -> FaceEnrollmentDetailOut:
    entity = await session.get(Entity, row.entity_id)
    return FaceEnrollmentDetailOut(
        id=row.id,
        entity_id=row.entity_id,
        person_name=entity.name if entity is not None else row.algorithm,
        version=row.version,
        is_current=row.is_current,
        algorithm=row.algorithm,
        embedding_dim=row.embedding_dim,
        threshold=row.threshold,
        sample_count=row.sample_count,
        status=row.status,
        privacy_level=row.privacy_level,
        consent_id=row.consent_id,
        supersedes_id=row.supersedes_id,
        superseded_by_id=row.superseded_by_id,
        reason_for_change=row.reason_for_change,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _crop_from_photo(photo) -> FaceCrop:
    return FaceCrop(
        image_b64=photo.image_b64,
        quality=photo.quality,
        confidence=photo.confidence,
        source=photo.source,
        attachment_id=str(photo.attachment_id) if photo.attachment_id else None,
        live_event_id=str(photo.live_event_id) if photo.live_event_id else None,
    )


@router.post("/enrollments", response_model=FaceEnrollResponse, status_code=201)
async def enroll_face(
    data: FaceEnrollmentCreate,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_owner_trust),
) -> FaceEnrollResponse:
    """Enroll one consented person from >=5 aligned crops (never stranger scans)."""
    service = FaceEnrollmentService(session, master_key=settings.master_key)
    try:
        row = await service.enroll(
            person_name=data.person_name,
            photos=[_crop_from_photo(photo) for photo in data.photos],
            reason=data.reason,
        )
    except FaceError as exc:
        await session.commit()
        raise _face_http(exc) from exc
    await session.commit()
    return FaceEnrollResponse(
        enrollment=await _enrollment_out(session, row),
        sample_count=row.sample_count,
        raw_photos_stored=False,
        provider=service.embedder.name,
        degraded=service.embedder.degraded,
    )


@router.get("/enrollments", response_model=list[FaceEnrollmentDetailOut])
async def list_face_enrollments(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[FaceEnrollmentDetailOut]:
    rows = list(
        (
            await session.execute(
                select(FaceEnrollment).order_by(FaceEnrollment.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [await _enrollment_out(session, row) for row in rows]


@router.post(
    "/enrollments/{enrollment_id}/revoke",
    response_model=FaceEnrollmentDetailOut,
)
async def revoke_face_enrollment(
    enrollment_id: UUID,
    reason: str = Query(default="user revoked", max_length=512),
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_owner_trust),
) -> FaceEnrollmentDetailOut:
    service = FaceEnrollmentService(session, master_key=settings.master_key)
    try:
        row = await service.revoke(enrollment_id, reason=reason)
    except FaceError as exc:
        await session.commit()
        raise _face_http(exc) from exc
    await session.commit()
    return await _enrollment_out(session, row)


@router.post(
    "/enrollments/{enrollment_id}/delete",
    response_model=FaceEnrollmentDetailOut,
)
async def delete_face_enrollment(
    enrollment_id: UUID,
    reason: str = Query(default="user deleted", max_length=512),
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_owner_trust),
) -> FaceEnrollmentDetailOut:
    service = FaceEnrollmentService(session, master_key=settings.master_key)
    try:
        row = await service.delete(enrollment_id, reason=reason)
    except FaceError as exc:
        await session.commit()
        raise _face_http(exc) from exc
    await session.commit()
    return await _enrollment_out(session, row)


@router.post("/recognize", response_model=FaceRecognitionResponse)
async def recognize_face(
    data: FaceRecognitionRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> FaceRecognitionResponse:
    """Match ONE aligned crop against enrolled templates; non-matches are unknown."""
    resolver = FaceResolver(session, master_key=settings.master_key)
    crop = FaceCrop(
        image_b64=data.image_b64,
        quality=data.quality,
        confidence=data.confidence,
        source=data.source,
        attachment_id=str(data.attachment_id) if data.attachment_id else None,
        live_event_id=str(data.live_event_id) if data.live_event_id else None,
    )
    try:
        result = await resolver.recognize(crop, write_log=data.write_log)
    except FaceError as exc:
        await session.commit()
        raise _face_http(exc) from exc
    await session.commit()
    return FaceRecognitionResponse(
        resolved=result.resolved,
        unknown=result.unknown,
        label=result.label,
        entity_id=result.entity_id,
        confidence=result.confidence,
        threshold=result.threshold,
        provider=result.provider,
        degraded=result.degraded,
        candidates=result.candidates,
        recognition_id=result.recognition_id,
    )


@router.post(
    "/recognitions/{recognition_id}/confirm",
    response_model=FaceRecognitionConfirmOut,
)
async def confirm_recognition(
    recognition_id: UUID,
    data: FaceRecognitionConfirmRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_owner_trust),
) -> FaceRecognitionConfirmOut:
    """Human confirmation/correction; flips a model sighting to user-confirmed."""
    resolver = FaceResolver(session, master_key=settings.master_key)
    try:
        row = await resolver.confirm(
            recognition_id,
            correct_label=data.correct_label,
            correct_entity_id=data.correct_entity_id,
            reason=data.reason,
            actor=ctx.actor,
        )
    except FaceError as exc:
        await session.commit()
        raise _face_http(exc) from exc
    await session.commit()
    return FaceRecognitionConfirmOut(
        recognition_id=row.id,
        label=row.label,
        entity_id=row.entity_id,
        confidence=row.confidence,
        source=row.source,
        confirmed=True,
        created_at=row.created_at,
    )


@router.get("/{name}/biodata", response_model=PublicFigureBiodataOut)
async def public_figure_biodata(
    name: str,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PublicFigureBiodataOut:
    """Licensed, attributed public-figure biodata (Wikidata CC0 + Wikipedia CC BY-SA)."""
    resolver = BiodataResolver(session)
    try:
        result = await resolver.resolve(name)
    except BiodataError as exc:
        await session.commit()
        raise _biodata_http(exc) from exc
    await session.commit()
    return await resolver.to_schema(result)


@router.post("/{name}/biodata/link", response_model=PublicFigureBiodataOut)
async def link_public_figure(
    name: str,
    data: PublicFigureLinkRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_owner_trust),
) -> PublicFigureBiodataOut:
    """Explicitly merge a public-figure record into a private person (never automatic)."""
    resolver = BiodataResolver(session)
    try:
        await resolver.resolve(name)
        await resolver.link_to_entity(
            name,
            data.entity_id,
            actor=ctx.actor,
            reason=data.reason,
        )
        result = await resolver.resolve(name, refresh=True)
    except BiodataError as exc:
        await session.commit()
        raise _biodata_http(exc) from exc
    await session.commit()
    out = await resolver.to_schema(result)
    out.merged = True
    return out


@router.post("/calibrate", response_model=FaceCalibrationReport)
async def calibrate_threshold(
    data: FaceCalibrationRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_owner_trust),
) -> FaceCalibrationReport:
    """Calibrate the cosine threshold from a real ROC over labeled trial photos."""
    report = await calibrate(
        [trial.model_dump() for trial in data.trials],
        target_far=data.target_far,
    )
    if data.apply:
        await apply_threshold(
            session,
            master_key=settings.master_key,
            threshold=report.threshold,
        )
    await session.commit()
    return report


@router.delete("/{entity_id}", response_model=dict)
async def erase_person_endpoint(
    entity_id: UUID,
    reason: str = Query(default="user requested person deletion", max_length=512),
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_owner_trust),
) -> dict:
    """Per-person erasure: templates, samples, sightings, and cached biodata."""
    manifest = await erase_person(
        session,
        entity_id=entity_id,
        reason=reason,
        actor=ctx.actor,
    )
    await session.commit()
    return manifest
