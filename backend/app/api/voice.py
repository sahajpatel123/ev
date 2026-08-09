"""EVIE voice & speech API: enrollment, wake, verify, utterance, follow-up, idle."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    ActorContext,
    require_actor_context,
    require_owner_trust,
    require_reverification,
)
from app.config import settings
from app.db import get_session
from app.identity import service as identity_service
from app.models import VoiceSession
from app.schemas import (
    ConsentOut,
    MemoryDelta,
    SpeechStyleOut,
    TtsOut,
    VoiceDeleteRequest,
    VoiceEnrollmentCreate,
    VoiceEnrollmentDetailOut,
    VoiceEnrollResponse,
    VoiceExportOut,
    VoicePrintExportOut,
    VoiceRevokeRequest,
    VoiceRollbackRequest,
    VoiceSessionVerifyRequest,
    VoiceSessionVerifyResponse,
    VoiceStatusOut,
    VoiceUtteranceRequest,
    VoiceUtteranceResponse,
    VoiceWakeRequest,
    VoiceWakeResponse,
)
from app.utils.text import utcnow
from app.voice.lifecycle import VoiceError, VoiceRuntime

router = APIRouter(prefix="/v1/voice", tags=["voice"])


def _runtime(session: AsyncSession) -> VoiceRuntime:
    return VoiceRuntime(session, master_key=settings.master_key)


def _http(exc: VoiceError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail=exc.message,
        headers={"X-Error-Code": exc.code},
    )


def _guard_session(row: VoiceSession, ctx: ActorContext) -> None:
    """A voice session belongs to the device that woke it — no silent inheritance."""
    if ctx.is_master:
        return
    if row.owner_id is not None and (ctx.device is None or row.owner_id != ctx.device.owner_id):
        raise HTTPException(
            status_code=403,
            detail="Voice session belongs to a different owner",
            headers={"X-Error-Code": "session_owner_mismatch"},
        )
    if ctx.device_id is None or row.device_id != str(ctx.device_id):
        raise HTTPException(
            status_code=403,
            detail="Voice session belongs to another device",
            headers={"X-Error-Code": "session_device_mismatch"},
        )


# --------------------------------------------------------------------------- #
# Enrollment & voiceprint management
# --------------------------------------------------------------------------- #


@router.post("/enroll", response_model=VoiceEnrollResponse, status_code=201)
async def enroll_voice(
    data: VoiceEnrollmentCreate,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_owner_trust),
) -> VoiceEnrollResponse:
    runtime = _runtime(session)
    try:
        row = await runtime.enroll(
            [s.model_dump(exclude_none=True) for s in data.samples],
            reason=data.reason,
        )
    except VoiceError as exc:
        raise _http(exc) from exc
    owner = await identity_service.get_owner(session)
    if owner is not None:
        row.owner_id = owner.id
    await session.commit()
    return VoiceEnrollResponse(
        enrollment=VoiceEnrollmentDetailOut.model_validate(row),
        sample_count=row.sample_count,
        raw_samples_stored=False,
    )


@router.get("/enrollments", response_model=list[VoiceEnrollmentDetailOut])
async def list_enrollments(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_owner_trust),
) -> list[VoiceEnrollmentDetailOut]:
    rows = await _runtime(session).list_enrollments()
    return [VoiceEnrollmentDetailOut.model_validate(r) for r in rows]


@router.get("/enrollments/export", response_model=VoiceExportOut)
async def export_enrollments(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_owner_trust),
) -> VoiceExportOut:
    data = await _runtime(session).export_voiceprints()
    return VoiceExportOut(
        exported_at=utcnow(),
        consents=[ConsentOut.model_validate(c) for c in data["consents"]],
        enrollments=[VoiceEnrollmentDetailOut.model_validate(e) for e in data["enrollments"]],
        voiceprints=[VoicePrintExportOut(**vp) for vp in data["voiceprints"]],
    )


@router.post("/enrollments/{enrollment_id}/revoke", response_model=VoiceEnrollmentDetailOut)
async def revoke_enrollment(
    enrollment_id: UUID,
    data: VoiceRevokeRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_reverification("voice.revoke")),
) -> VoiceEnrollmentDetailOut:
    try:
        row = await _runtime(session).revoke(enrollment_id, reason=data.reason)
    except VoiceError as exc:
        raise _http(exc) from exc
    await session.commit()
    return VoiceEnrollmentDetailOut.model_validate(row)


@router.post("/enrollments/{enrollment_id}/delete", response_model=VoiceEnrollmentDetailOut)
async def delete_enrollment(
    enrollment_id: UUID,
    data: VoiceDeleteRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_reverification("voice.delete")),
) -> VoiceEnrollmentDetailOut:
    try:
        row = await _runtime(session).delete(enrollment_id, reason=data.reason)
    except VoiceError as exc:
        raise _http(exc) from exc
    await session.commit()
    return VoiceEnrollmentDetailOut.model_validate(row)


@router.post("/enrollments/{enrollment_id}/rollback", response_model=VoiceEnrollmentDetailOut)
async def rollback_enrollment(
    enrollment_id: UUID,
    data: VoiceRollbackRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_owner_trust),
) -> VoiceEnrollmentDetailOut:
    try:
        row = await _runtime(session).rollback(
            enrollment_id,
            target_version=data.target_version,
            reason=data.reason,
        )
    except VoiceError as exc:
        raise _http(exc) from exc
    await session.commit()
    return VoiceEnrollmentDetailOut.model_validate(row)


# --------------------------------------------------------------------------- #
# Lifecycle: wake → verify → listen → act → reply → follow-up → idle
# --------------------------------------------------------------------------- #


@router.post("/wake", response_model=VoiceWakeResponse, status_code=201)
async def wake(
    data: VoiceWakeRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> VoiceWakeResponse:
    runtime = _runtime(session)
    try:
        outcome = await runtime.handle_wake(
            device_id=str(ctx.device_id) if ctx.is_device else data.device_id,
            audio_ref=data.audio_ref,
            text_hint=data.text_hint,
            wake_word=data.wake_word,
        )
    except VoiceError as exc:
        raise _http(exc) from exc
    if outcome.session_id is not None:
        session_row = await session.get(VoiceSession, UUID(outcome.session_id))
        owner = await identity_service.get_owner(session)
        if session_row is not None and owner is not None:
            session_row.owner_id = owner.id
    await session.commit()
    return VoiceWakeResponse(
        session_id=UUID(outcome.session_id) if outcome.session_id else None,
        state=outcome.state,
        owner_enrolled=outcome.owner_enrolled,
        challenge_nonce=outcome.challenge_nonce,
        challenge_phrase=outcome.challenge_phrase,
        message=outcome.message,
    )


@router.post("/verify", response_model=VoiceSessionVerifyResponse)
async def verify(
    data: VoiceSessionVerifyRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> VoiceSessionVerifyResponse:
    runtime = _runtime(session)
    row = await session.get(VoiceSession, data.session_id)
    if row is not None:
        _guard_session(row, ctx)
    try:
        outcome = await runtime.handle_verify(
            session_id=data.session_id,
            nonce=data.nonce,
            samples=[{"audio_b64": sample} for sample in data.samples],
            phrase=data.phrase,
            audio_ref=data.audio_ref,
            liveness_proof=data.liveness_proof,
            live_score=data.live_score,
            audio_sha256=data.audio_sha256,
        )
    except VoiceError as exc:
        raise _http(exc) from exc
    await session.commit()
    return VoiceSessionVerifyResponse(
        session_id=UUID(outcome.session_id) if outcome.session_id else None,
        state=outcome.state,
        verified=outcome.verified,
        confidence=outcome.confidence,
        reason=outcome.reason,
    )


@router.post("/utterance", response_model=VoiceUtteranceResponse)
async def utterance(
    data: VoiceUtteranceRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> VoiceUtteranceResponse:
    runtime = _runtime(session)
    row = await session.get(VoiceSession, data.session_id)
    if row is not None:
        _guard_session(row, ctx)
    try:
        outcome = await runtime.handle_utterance(
            session_id=data.session_id,
            text=data.text,
            audio_b64=data.audio_b64,
            audio_ref=data.audio_ref,
            language=data.language,
            conversation_id=data.conversation_id,
            follow_up=data.follow_up,
        )
    except VoiceError as exc:
        raise _http(exc) from exc
    await session.commit()
    return VoiceUtteranceResponse(
        session_id=UUID(outcome.session_id),
        state=outcome.state,
        transcript=outcome.transcript.text,
        transcript_confidence=outcome.transcript.confidence,
        reply=outcome.reply,
        conversation_id=UUID(outcome.conversation_id) if outcome.conversation_id else None,
        tts=(
            TtsOut(
                provider=outcome.tts.provider,
                audio_ref=outcome.tts.audio_ref,
                content_type=outcome.tts.content_type,
                ssml=outcome.tts.ssml,
                duration_ms=outcome.tts.duration_ms,
            )
            if outcome.tts
            else None
        ),
        style=(
            SpeechStyleOut(
                urgency=outcome.style.urgency,
                warmth=outcome.style.warmth,
                brevity=outcome.style.brevity,
                mode=outcome.style.mode,
                length_target=outcome.style.length_target,
                directness=outcome.style.directness,
            )
            if outcome.style
            else None
        ),
        model=outcome.model,
        context_tokens=outcome.context_tokens,
        memory_deltas=[
            MemoryDelta.model_validate(d)
            for d in (outcome.memory_deltas or [])
            if isinstance(d, dict)
        ],
    )


@router.get("/sessions/{session_id}", response_model=VoiceStatusOut)
async def session_status(
    session_id: UUID,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> VoiceStatusOut:
    row = await session.get(VoiceSession, session_id)
    if row is not None:
        _guard_session(row, ctx)
    try:
        status = await _runtime(session).status(session_id)
    except VoiceError as exc:
        raise _http(exc) from exc
    return VoiceStatusOut(
        session_id=UUID(status.session_id) if status.session_id else None,
        state=status.state,
        owner_enrolled=status.owner_enrolled,
        owner_verified=status.owner_verified,
        device_id=status.device_id,
        speaker_confidence=status.speaker_confidence,
        follow_up_remaining_seconds=status.follow_up_remaining_seconds,
        expires_at=status.expires_at,
        ended_at=status.ended_at,
        end_reason=status.end_reason,
    )


@router.post("/sessions/{session_id}/end", response_model=VoiceStatusOut)
async def end_session(
    session_id: UUID,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> VoiceStatusOut:
    row = await session.get(VoiceSession, session_id)
    if row is not None:
        _guard_session(row, ctx)
    try:
        status = await _runtime(session).handle_end(session_id, reason="user-ended")
    except VoiceError as exc:
        raise _http(exc) from exc
    await session.commit()
    return VoiceStatusOut(
        session_id=UUID(status.session_id) if status.session_id else None,
        state=status.state,
        owner_enrolled=status.owner_enrolled,
        owner_verified=status.owner_verified,
        device_id=status.device_id,
        speaker_confidence=status.speaker_confidence,
        follow_up_remaining_seconds=status.follow_up_remaining_seconds,
        expires_at=status.expires_at,
        ended_at=status.ended_at,
        end_reason=status.end_reason,
    )
