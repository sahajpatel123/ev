"""Training & personalization API: consent lifecycle and voice enrollment.

Voice enrollment/verification is delegated to the EVIE voice subsystem
(``app.voice.lifecycle.VoiceRuntime``) so there is one owner-identity path.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor
from app.config import settings
from app.db import get_session
from app.schemas import (
    AdapterActivateRequest,
    AdapterDeleteResponse,
    AdapterOut,
    AdapterRegisterRequest,
    AdapterRollbackRequest,
    ConsentGrant,
    ConsentOut,
    ConsentRevoke,
    FilterRecalibrationApplyRequest,
    FilterRecalibrationBuildResponse,
    FilterRecalibrationDeleteResponse,
    FilterRecalibrationOut,
    FilterRecalibrationRollbackRequest,
    FilterThresholdProposalOut,
    PersonalizationCalibrateResponse,
    PersonalizationCalibrationOut,
    PersonalizationDeleteResponse,
    PersonalizationRollbackRequest,
    TrainingCorpusBuildResponse,
    TrainingCorpusDeleteResponse,
    TrainingCorpusEntryOut,
    TrainingCorpusExportOut,
    TrainingCorpusRollbackRequest,
    TrainingCorpusSnapshotOut,
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
from app.training import adapter as adapter_service
from app.training import consent as consent_service
from app.training import corpus as corpus_service
from app.training import filter_improvement as filter_improvement_service
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


def _training_http(exc: Exception, *, track: str = "life_data_personalization") -> HTTPException:
    if isinstance(exc, ConsentRequiredError):
        return HTTPException(
            status_code=403,
            detail=f"Consent required: grant {track} consent before using this track",
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
        raise _training_http(exc) from exc
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
        raise _training_http(exc) from exc
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


# --------------------------------------------------------------------------- #
# Training corpus harvesting (consent-gated, versioned, erasable)
# --------------------------------------------------------------------------- #


@router.post("/corpus/build", response_model=TrainingCorpusBuildResponse, status_code=201)
async def corpus_build(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> TrainingCorpusBuildResponse:
    try:
        snapshot, excluded = await corpus_service.build_snapshot(session, actor=actor)
    except ConsentRequiredError as exc:
        raise _training_http(exc, track="training_corpus") from exc
    await session.commit()
    return TrainingCorpusBuildResponse(
        snapshot=TrainingCorpusSnapshotOut.model_validate(snapshot),
        entry_count=snapshot.entry_count,
        excluded_never_send_to_model=excluded,
    )


@router.get("/corpus/current", response_model=TrainingCorpusSnapshotOut | None)
async def corpus_current(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> TrainingCorpusSnapshotOut | None:
    row = await corpus_service.current_snapshot(session)
    if row is None:
        return None
    return TrainingCorpusSnapshotOut.model_validate(row)


@router.get("/corpus/history", response_model=list[TrainingCorpusSnapshotOut])
async def corpus_history(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[TrainingCorpusSnapshotOut]:
    rows = await corpus_service.list_snapshots(session)
    return [TrainingCorpusSnapshotOut.model_validate(r) for r in rows]


@router.get("/corpus/{version}/export", response_model=TrainingCorpusExportOut)
async def corpus_export(
    version: int,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> TrainingCorpusExportOut:
    try:
        row = await corpus_service.get_snapshot(session, version)
    except KeyError as exc:
        raise _training_http(exc) from exc
    await log_access(
        session,
        actor=actor,
        action="corpus_export",
        endpoint=f"GET /v1/training/corpus/{version}/export",
        resource_type="training_corpus",
        resource_ids=[row.id],
        details={"version": version, "entry_count": row.entry_count},
    )
    await session.commit()
    return TrainingCorpusExportOut(
        snapshot=TrainingCorpusSnapshotOut.model_validate(row),
        entries=[TrainingCorpusEntryOut.model_validate(e) for e in row.entries],
    )


@router.post("/corpus/rollback", response_model=TrainingCorpusSnapshotOut)
async def corpus_rollback(
    data: TrainingCorpusRollbackRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> TrainingCorpusSnapshotOut:
    try:
        row = await corpus_service.rollback(
            session,
            target_version=data.target_version,
            actor=actor,
            reason=data.reason,
        )
    except (ConsentRequiredError, KeyError) as exc:
        raise _training_http(exc, track="training_corpus") from exc
    await session.commit()
    return TrainingCorpusSnapshotOut.model_validate(row)


@router.post("/corpus/delete", response_model=TrainingCorpusDeleteResponse)
async def corpus_delete(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> TrainingCorpusDeleteResponse:
    deleted = await corpus_service.delete_all(
        session, actor=actor, reason="user deleted training corpus data"
    )
    await session.commit()
    return TrainingCorpusDeleteResponse(deleted=deleted, redacted=True)


# --------------------------------------------------------------------------- #
# Filter self-improvement — ledger-driven recalibration reports
# --------------------------------------------------------------------------- #


@router.post(
    "/filter/self-improve",
    response_model=FilterRecalibrationBuildResponse,
    status_code=201,
)
async def filter_self_improve(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> FilterRecalibrationBuildResponse:
    try:
        row = await filter_improvement_service.recalibrate(session, actor=actor)
    except ConsentRequiredError as exc:
        raise _training_http(exc, track="filter_self_improvement") from exc
    await session.commit()
    return FilterRecalibrationBuildResponse(
        recalibration=FilterRecalibrationOut.model_validate(row),
        proposals=[FilterThresholdProposalOut.model_validate(p) for p in row.proposals],
        applied=False,
    )


@router.get("/filter/recalibration", response_model=FilterRecalibrationOut | None)
async def filter_recalibration(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> FilterRecalibrationOut | None:
    row = await filter_improvement_service.current_recalibration(session)
    if row is None:
        return None
    return FilterRecalibrationOut.model_validate(row)


@router.get("/filter/recalibration/history", response_model=list[FilterRecalibrationOut])
async def filter_recalibration_history(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[FilterRecalibrationOut]:
    rows = await filter_improvement_service.list_recalibrations(session)
    return [FilterRecalibrationOut.model_validate(r) for r in rows]


@router.post(
    "/filter/recalibration/rollback",
    response_model=FilterRecalibrationOut,
)
async def filter_recalibration_rollback(
    data: FilterRecalibrationRollbackRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> FilterRecalibrationOut:
    try:
        row = await filter_improvement_service.rollback(
            session,
            target_version=data.target_version,
            actor=actor,
            reason=data.reason,
        )
    except (ConsentRequiredError, KeyError) as exc:
        raise _training_http(exc, track="filter_self_improvement") from exc
    await session.commit()
    return FilterRecalibrationOut.model_validate(row)


@router.post(
    "/filter/recalibration/apply",
    response_model=FilterRecalibrationOut,
)
async def filter_recalibration_apply(
    data: FilterRecalibrationApplyRequest | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> FilterRecalibrationOut:
    try:
        row = await filter_improvement_service.apply_current(
            session,
            actor=actor,
            reason=data.reason if data is not None else None,
        )
    except (ConsentRequiredError, KeyError, ValueError) as exc:
        raise _training_http(exc, track="filter_self_improvement") from exc
    await session.commit()
    return FilterRecalibrationOut.model_validate(row)


@router.post(
    "/filter/recalibration/delete",
    response_model=FilterRecalibrationDeleteResponse,
)
async def filter_recalibration_delete(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> FilterRecalibrationDeleteResponse:
    deleted = await filter_improvement_service.delete_all(
        session, actor=actor, reason="user deleted filter recalibration data"
    )
    await session.commit()
    return FilterRecalibrationDeleteResponse(deleted=deleted, redacted=True)


# --------------------------------------------------------------------------- #
# Adapter fine-tuning — versioned adapter registry with eval gates
# --------------------------------------------------------------------------- #


@router.post("/adapter/register", response_model=AdapterOut, status_code=201)
async def adapter_register(
    data: AdapterRegisterRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> AdapterOut:
    try:
        row = await adapter_service.register(
            session,
            name=data.name,
            provider=data.provider,
            base_model=data.base_model,
            adapter_ref=data.adapter_ref,
            corpus_version=data.corpus_version,
            actor=actor,
            reason=data.reason,
        )
    except (ConsentRequiredError, KeyError) as exc:
        raise _training_http(exc, track="adapter_fine_tuning") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    return AdapterOut.model_validate(row)


@router.get("/adapter", response_model=list[AdapterOut])
async def adapter_list(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[AdapterOut]:
    rows = await adapter_service.list_adapters(session)
    return [AdapterOut.model_validate(r) for r in rows]


@router.get("/adapter/{adapter_id}", response_model=AdapterOut)
async def adapter_get(
    adapter_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> AdapterOut:
    try:
        row = await adapter_service.get_adapter(session, adapter_id)
    except KeyError as exc:
        raise _training_http(exc) from exc
    return AdapterOut.model_validate(row)


@router.post("/adapter/activate", response_model=AdapterOut)
async def adapter_activate(
    data: AdapterActivateRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> AdapterOut:
    try:
        row = await adapter_service.activate(
            session, adapter_id=data.adapter_id, actor=actor, reason=data.reason
        )
    except (ConsentRequiredError, KeyError) as exc:
        raise _training_http(exc, track="adapter_fine_tuning") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    return AdapterOut.model_validate(row)


@router.post("/adapter/rollback", response_model=AdapterOut)
async def adapter_rollback(
    data: AdapterRollbackRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> AdapterOut:
    try:
        row = await adapter_service.rollback(
            session, adapter_id=data.adapter_id, actor=actor, reason=data.reason
        )
    except (ConsentRequiredError, KeyError) as exc:
        raise _training_http(exc, track="adapter_fine_tuning") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    return AdapterOut.model_validate(row)


@router.post("/adapter/delete", response_model=AdapterDeleteResponse)
async def adapter_delete(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> AdapterDeleteResponse:
    deleted = await adapter_service.delete_all(
        session, actor=actor, reason="user deleted adapter data"
    )
    await session.commit()
    return AdapterDeleteResponse(deleted=deleted, redacted=True)
