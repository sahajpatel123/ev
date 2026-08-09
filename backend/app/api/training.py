"""Training & personalization API: consent lifecycle and voice enrollment.

Voice enrollment/verification is delegated to the EVIE voice subsystem
(``app.voice.lifecycle.VoiceRuntime``) so there is one owner-identity path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor
from app.config import settings
from app.db import get_session
from app.schemas import (
    ConsentGrant,
    ConsentOut,
    ConsentRevoke,
    PersonalizationCalibrateResponse,
    PersonalizationCalibrationOut,
    PersonalizationDeleteResponse,
    PersonalizationRollbackRequest,
    TrainingTrack,
    VoiceDeleteRequest,
    VoiceEnrollmentDetailOut,
    VoiceEnrollResponse,
    VoiceExportOut,
    VoicePrintExportOut,
    VoiceRevokeRequest,
    VoiceRollbackRequest,
    VoiceVerifyRequest,
    VoiceVerifyResponse,
)
from app.services.access_log import log_access
from app.training import consent as consent_service
from app.training import personalization as personalization_service
from app.training.consent import ConsentRequiredError
from app.utils.text import utcnow
from app.voice.lifecycle import VoiceError, VoiceRuntime

router = APIRouter(prefix="/v1/training")


def _runtime(session: AsyncSession) -> VoiceRuntime:
    return VoiceRuntime(session, master_key=settings.master_key)


def _voice_http(exc: VoiceError) -> HTTPException:
    if exc.code == "consent_required":
        return HTTPException(
            status_code=403,
            detail="Consent required: grant voice_enrollment consent before using biometric data",
            headers={"X-Error-Code": exc.code},
        )
    return HTTPException(status_code=exc.status, detail=exc.message, headers={"X-Error-Code": exc.code})


def _personalization_http(exc: Exception) -> HTTPException:
    if isinstance(exc, ConsentRequiredError):
        return HTTPException(
            status_code=403,
            detail="Consent required: grant life_data_personalization consent before calibrating",
            headers={"X-Error-Code": "consent_required"},
        )
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.post("/consent", response_model=ConsentOut, status_code=201)
async def grant_consent(
    data: ConsentGrant,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ConsentOut:
    row = await consent_service.grant_consent(
        session,
        track=data.track,
        purpose=data.purpose,
        scope=data.scope,
        source=data.source,
        consent_version=data.consent_version,
    )
    await log_access(
        session,
        actor=actor,
        action="consent_grant",
        endpoint="POST /v1/training/consent",
        resource_type="consent",
        resource_ids=[row.id],
        details={"track": row.track, "consent_version": row.consent_version},
    )
    await session.commit()
    return ConsentOut.model_validate(row)


@router.get("/consent", response_model=list[ConsentOut])
async def list_consents(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[ConsentOut]:
    rows = await consent_service.list_consents(session)
    return [ConsentOut.model_validate(r) for r in rows]


@router.post("/consent/{track}/revoke", response_model=ConsentOut)
async def revoke_consent(
    track: TrainingTrack,
    data: ConsentRevoke,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ConsentOut:
    row = await consent_service.revoke_consent(session, track=track, reason=data.reason)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No active consent for track {track}")
    revoked = 0
    if track == "voice_enrollment":
        try:
            revoked = await _runtime(session).revoke_all(reason=f"consent revoked: {data.reason}")
        except VoiceError as exc:
            raise _voice_http(exc) from exc
    await log_access(
        session,
        actor=actor,
        action="consent_revoke",
        endpoint=f"POST /v1/training/consent/{track}/revoke",
        resource_type="consent",
        resource_ids=[row.id],
        details={"track": track, "reason": data.reason, "enrollments_revoked": revoked},
    )
    await session.commit()
    return ConsentOut.model_validate(row)


@router.post("/voice/enroll", response_model=VoiceEnrollResponse, status_code=201)
async def voice_enroll(
    data: VoiceVerifyRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> VoiceEnrollResponse:
    """Enroll with base64 audio samples (raw bytes are discarded immediately)."""
    runtime = _runtime(session)
    try:
        enrollment = await runtime.enroll(
            [{"audio_b64": sample} for sample in data.samples],
            reason="training voice enrollment",
        )
    except VoiceError as exc:
        raise _voice_http(exc) from exc
    await session.commit()
    return VoiceEnrollResponse(
        enrollment=VoiceEnrollmentDetailOut.model_validate(enrollment),
        sample_count=enrollment.sample_count,
        raw_samples_stored=False,
    )


@router.post("/voice/verify", response_model=VoiceVerifyResponse)
async def voice_verify(
    data: VoiceVerifyRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> VoiceVerifyResponse:
    runtime = _runtime(session)
    try:
        result = await runtime.verify_samples(
            [{"audio_b64": sample} for sample in data.samples]
        )
    except VoiceError as exc:
        raise _voice_http(exc) from exc
    await session.commit()
    return VoiceVerifyResponse(**result)


@router.post("/voice/rollback", response_model=VoiceEnrollmentDetailOut)
async def voice_rollback(
    data: VoiceRollbackRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> VoiceEnrollmentDetailOut:
    if data.enrollment_id is None:
        raise HTTPException(status_code=422, detail="enrollment_id is required")
    try:
        enrollment = await _runtime(session).rollback(
            data.enrollment_id,
            target_version=data.target_version,
            reason=data.reason,
        )
    except VoiceError as exc:
        raise _voice_http(exc) from exc
    await session.commit()
    return VoiceEnrollmentDetailOut.model_validate(enrollment)


@router.post("/voice/revoke", response_model=VoiceEnrollmentDetailOut)
async def voice_revoke(
    data: VoiceRevokeRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> VoiceEnrollmentDetailOut:
    if data.enrollment_id is None:
        raise HTTPException(status_code=422, detail="enrollment_id is required")
    try:
        enrollment = await _runtime(session).revoke(
            data.enrollment_id, reason=data.reason
        )
    except VoiceError as exc:
        raise _voice_http(exc) from exc
    await session.commit()
    return VoiceEnrollmentDetailOut.model_validate(enrollment)


@router.post("/voice/delete", response_model=VoiceEnrollmentDetailOut)
async def voice_delete(
    data: VoiceDeleteRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> VoiceEnrollmentDetailOut:
    if data.enrollment_id is None:
        raise HTTPException(status_code=422, detail="enrollment_id is required")
    try:
        enrollment = await _runtime(session).delete(
            data.enrollment_id, reason=data.reason
        )
    except VoiceError as exc:
        raise _voice_http(exc) from exc
    await session.commit()
    return VoiceEnrollmentDetailOut.model_validate(enrollment)


@router.get("/voice/enrollments", response_model=list[VoiceEnrollmentDetailOut])
async def voice_enrollments(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[VoiceEnrollmentDetailOut]:
    rows = await _runtime(session).list_enrollments()
    return [VoiceEnrollmentDetailOut.model_validate(r) for r in rows]


@router.get("/voice/export", response_model=VoiceExportOut)
async def voice_export(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> VoiceExportOut:
    data = await _runtime(session).export_voiceprints()
    await log_access(
        session,
        actor=actor,
        action="voice_export",
        endpoint="GET /v1/training/voice/export",
        resource_type="voiceprint",
        resource_ids=[],
        details={
            "consents": len(data["consents"]),
            "enrollments": len(data["enrollments"]),
            "voiceprints": len(data["voiceprints"]),
        },
    )
    await session.commit()
    return VoiceExportOut(
        exported_at=utcnow(),
        consents=[ConsentOut.model_validate(c) for c in data["consents"]],
        enrollments=[
            VoiceEnrollmentDetailOut.model_validate(e) for e in data["enrollments"]
        ],
        voiceprints=[VoicePrintExportOut(**vp) for vp in data["voiceprints"]],
    )


# --------------------------------------------------------------------------- #
# Life-data personalization — evidence-backed importance/retrieval calibration
# --------------------------------------------------------------------------- #


@router.post(
    "/personalization/calibrate",
    response_model=PersonalizationCalibrateResponse,
    status_code=201,
)
async def personalization_calibrate(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PersonalizationCalibrateResponse:
    try:
        row = await personalization_service.calibrate(session, actor=actor)
        evidence = row.evidence
    except ConsentRequiredError as exc:
        raise _personalization_http(exc) from exc
    await session.commit()
    return PersonalizationCalibrateResponse(
        calibration=PersonalizationCalibrationOut.model_validate(row),
        evidence=evidence,
        applied=bool(row.calibrations),
    )


@router.get("/personalization/calibration", response_model=PersonalizationCalibrationOut | None)
async def personalization_calibration(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PersonalizationCalibrationOut | None:
    row = await personalization_service.current_calibration(session)
    if row is None:
        return None
    return PersonalizationCalibrationOut.model_validate(row)


@router.get("/personalization/history", response_model=list[PersonalizationCalibrationOut])
async def personalization_history(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[PersonalizationCalibrationOut]:
    rows = await personalization_service.list_calibrations(session)
    return [PersonalizationCalibrationOut.model_validate(r) for r in rows]


@router.post("/personalization/rollback", response_model=PersonalizationCalibrationOut)
async def personalization_rollback(
    data: PersonalizationRollbackRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PersonalizationCalibrationOut:
    try:
        row = await personalization_service.rollback(
            session,
            target_version=data.target_version,
            actor=actor,
            reason=data.reason,
        )
    except (ConsentRequiredError, KeyError) as exc:
        raise _personalization_http(exc) from exc
    await session.commit()
    return PersonalizationCalibrationOut.model_validate(row)


@router.post("/personalization/delete", response_model=PersonalizationDeleteResponse)
async def personalization_delete(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PersonalizationDeleteResponse:
    deleted = await personalization_service.delete_all(
        session, actor=actor, reason="user deleted personalization data"
    )
    await session.commit()
    return PersonalizationDeleteResponse(deleted=deleted, applied=False)
