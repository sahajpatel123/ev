"""EVIE voice & speech API: enrollment, wake, verify, utterance, follow-up, idle."""

from __future__ import annotations

import base64
import contextlib
import json
import re
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    ActorContext,
    require_actor_context,
    require_owner_trust,
    require_reverification,
)
from app.config import settings
from app.db import SessionLocal, get_session
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
    VoicePartialOut,
    VoicePrintExportOut,
    VoiceRevokeRequest,
    VoiceRollbackRequest,
    VoiceSessionVerifyRequest,
    VoiceSessionVerifyResponse,
    VoiceStatusOut,
    VoiceLiveOpenRequest,
    VoiceLiveOpenResponse,
    VoiceUtteranceRequest,
    VoiceUtteranceResponse,
    VoiceWakeRequest,
    VoiceWakeResponse,
)
from app.utils.text import utcnow
from app.voice.lifecycle import VoiceError, VoiceRuntime
from app.voice.speaker import default_speaker_verifier

router = APIRouter(prefix="/v1/voice", tags=["voice"])


def _runtime(session: AsyncSession) -> VoiceRuntime:
    return VoiceRuntime(
        session,
        master_key=settings.master_key,
        verifier=default_speaker_verifier(),
    )


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


def _tts_out(result) -> TtsOut | None:
    if result is None:
        return None
    audio_b64 = None
    audio = getattr(result, "audio", None)
    if audio and len(audio) <= 1_500_000:
        audio_b64 = base64.b64encode(audio).decode("ascii")
    return TtsOut(
        provider=result.provider,
        audio_ref=result.audio_ref,
        audio_b64=audio_b64,
        content_type=result.content_type,
        ssml=result.ssml,
        duration_ms=result.duration_ms,
        degraded=result.degraded,
    )


def _sse(name: str, data) -> str:
    return f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"


def _as_uuid(value) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


def _utterance_response(outcome, *, tts_device_id=None) -> VoiceUtteranceResponse:
    state = getattr(outcome.state, "value", outcome.state)
    return VoiceUtteranceResponse(
        session_id=_as_uuid(outcome.session_id) or UUID(int=0),
        state=str(state),
        transcript=outcome.transcript.text,
        transcript_confidence=outcome.transcript.confidence,
        transcript_degraded=outcome.transcript.degraded,
        transcript_provider=outcome.transcript.provider,
        reply=outcome.reply,
        conversation_id=_as_uuid(outcome.conversation_id),
        tts=_tts_out(outcome.tts),
        tts_device_id=_as_uuid(tts_device_id),
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
        error=getattr(outcome, "error", None),
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
        await session.commit()
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
        await session.commit()
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
        await session.commit()
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
        await session.commit()
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
    if data.push_to_talk and not ctx.is_master and (
        ctx.device is None or ctx.device.trust_level != "owner"
    ):
        raise HTTPException(
            status_code=403,
            detail="Owner-trusted device required for push-to-talk",
            headers={"X-Error-Code": "owner_trust_required"},
        )
    runtime = _runtime(session)
    try:
        outcome = await runtime.handle_wake(
            device_id=str(ctx.device_id) if ctx.is_device else data.device_id,
            priority=data.priority,
            audio_ref=data.audio_ref,
            text_hint=data.text_hint,
            wake_word=data.wake_word,
            audio_b64=data.audio_b64,
            push_to_talk=data.push_to_talk,
            min_wake_confidence=data.wake_confidence,
        )
    except VoiceError as exc:
        await session.commit()
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
        greeting=outcome.greeting,
        onboarding=outcome.onboarding,
        conversation_id=(
            UUID(outcome.conversation_id) if outcome.conversation_id else None
        ),
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
        await session.commit()
        raise _http(exc) from exc
    await session.commit()
    return VoiceSessionVerifyResponse(
        session_id=UUID(outcome.session_id) if outcome.session_id else None,
        state=outcome.state,
        verified=outcome.verified,
        confidence=outcome.confidence,
        reason=outcome.reason,
        conversation_id=UUID(outcome.conversation_id) if outcome.conversation_id else None,
        greeting=outcome.greeting,
        onboarding=outcome.onboarding,
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
            reverify_token=data.reverify_token,
            ctx=ctx,
            language=data.language,
            conversation_id=data.conversation_id,
            follow_up=data.follow_up,
            push_to_talk=data.push_to_talk,
        )
    except VoiceError as exc:
        await session.commit()
        raise _http(exc) from exc
    from app.ev.fleet import tts_playback_device

    target = await tts_playback_device(session)
    await session.commit()
    return _utterance_response(
        outcome, tts_device_id=str(target.id) if target is not None else None
    )


@router.post("/utterance/stream")
async def stream_utterance(
    data: VoiceUtteranceRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> StreamingResponse:
    """SSE utterance: partial ASR hypotheses, final transcript, then reply."""

    runtime = _runtime(session)
    row = await session.get(VoiceSession, data.session_id)
    if row is not None:
        _guard_session(row, ctx)

    async def events():
        try:
            async for event, payload in runtime.stream_utterance(
                session_id=data.session_id,
                text=data.text,
                audio_b64=data.audio_b64,
                audio_ref=data.audio_ref,
                reverify_token=data.reverify_token,
                ctx=ctx,
                language=data.language,
                conversation_id=data.conversation_id,
                follow_up=data.follow_up,
                push_to_talk=data.push_to_talk,
            ):
                if event == "partial":
                    item = VoicePartialOut(
                        text=payload.text,
                        provider=payload.provider,
                        sequence=payload.sequence,
                        stable=payload.stable,
                        confidence=payload.confidence,
                        degraded=payload.degraded,
                        timestamp_ms=payload.timestamp_ms,
                    )
                    yield _sse("partial", item.model_dump(mode="json"))
                elif event == "final_transcript":
                    yield _sse(
                        "final_transcript",
                        {
                            "text": payload.text,
                            "confidence": payload.confidence,
                            "provider": payload.provider,
                            "degraded": payload.degraded,
                            "audio_ref": payload.audio_ref,
                        },
                    )
                elif event == "tts_chunk":
                    tts = _tts_out(payload.tts)
                    yield _sse(
                        "tts_chunk",
                        {
                            "index": payload.index,
                            "text": payload.text,
                            "audio_b64": tts.audio_b64 if tts else None,
                            "content_type": tts.content_type if tts else None,
                            "duration_ms": tts.duration_ms if tts else None,
                            "provider": tts.provider if tts else None,
                        },
                    )
                elif event == "reply":
                    from app.ev.fleet import tts_playback_device

                    target = await tts_playback_device(session)
                    await session.commit()
                    yield _sse(
                        "reply",
                        _utterance_response(
                            payload,
                            tts_device_id=str(target.id) if target is not None else None,
                        ).model_dump(mode="json"),
                    )
                else:
                    await session.commit()
                    code = getattr(payload, "code", "voice_stream")
                    message = getattr(payload, "message", str(payload))
                    yield _sse("error", {"code": code, "message": message})
            yield _sse("done", {})
        except Exception:  # noqa: BLE001 - never abort the SSE socket
            with contextlib.suppress(Exception):
                await session.commit()
            yield _sse(
                "error",
                {
                    "code": "voice_stream",
                    "message": "Voice reply failed — try again.",
                },
            )
            yield _sse("done", {})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/barge_in", response_model=VoiceStatusOut)
async def barge_in(
    session_id: UUID,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> VoiceStatusOut:
    """Stop playback immediately and re-enter listening."""

    runtime = _runtime(session)
    row = await session.get(VoiceSession, session_id)
    if row is not None:
        _guard_session(row, ctx)
    try:
        status = await runtime.handle_barge_in(session_id)
    except VoiceError as exc:
        await session.commit()
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


def _ws_authorization(websocket: WebSocket, token: str | None) -> str | None:
    header = websocket.headers.get("authorization")
    if header:
        return header
    if token:
        return f"Bearer {token}"
    return None


@router.post("/live/open", response_model=VoiceLiveOpenResponse, status_code=201)
async def open_live_voice(
    data: VoiceLiveOpenRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> VoiceLiveOpenResponse:
    """Open a full-duplex live conversation without a wake word.

    EV.app calls this on launch. The owner is already authenticated; saying
    Evie is not required while the app is open.
    """

    if not ctx.is_master and (
        ctx.device is None or ctx.device.trust_level != "owner"
    ):
        raise HTTPException(
            status_code=403,
            detail="Owner-trusted device required for live conversation",
            headers={"X-Error-Code": "owner_trust_required"},
        )
    runtime = _runtime(session)
    try:
        outcome = await runtime.open_live_session(
            device_id=str(ctx.device_id) if ctx.is_device else data.device_id,
        )
    except VoiceError as exc:
        await session.commit()
        raise _http(exc) from exc
    if outcome.session_id is None:
        raise HTTPException(status_code=500, detail="Live session failed to open")
    session_row = await session.get(VoiceSession, UUID(outcome.session_id))
    owner = await identity_service.get_owner(session)
    if session_row is not None and owner is not None:
        session_row.owner_id = owner.id
    conversation_id = session_row.conversation_id if session_row is not None else None
    await session.commit()
    return VoiceLiveOpenResponse(
        session_id=UUID(outcome.session_id),
        state=outcome.state,
        conversation_id=conversation_id,
        live=True,
        message=outcome.message or "Listening.",
        greeting=outcome.greeting,
        onboarding=outcome.onboarding,
    )


@router.websocket("/live")
async def voice_live(
    websocket: WebSocket,
    session_id: UUID,
    token: str | None = None,
    ticket: str | None = None,
) -> None:
    """Full-duplex EV LIVE channel. See ``docs/LIVE_VOICE.md``."""

    await websocket.accept()
    if not settings.voice_live_enabled:
        await websocket.send_json(
            {
                "type": "error",
                "code": "live_disabled",
                "message": "EV LIVE is disabled",
                "fatal": True,
            }
        )
        await websocket.close(code=4003)
        return

    from app.auth import _resolve_actor
    from app.device_gateway.tickets import consume as consume_ws_ticket
    from app.models import Device as DeviceRow
    from app.voice.live.transport import bind_live_session, serve_live_websocket

    ctx = None
    claimed = None
    if ticket:
        claimed = consume_ws_ticket(ticket, session_id=str(session_id))
        if claimed is None:
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "unauthorized",
                    "message": "Invalid or expired live ticket",
                    "fatal": True,
                }
            )
            await websocket.close(code=4001)
            return
        try:
            async with SessionLocal() as session:
                device = await session.get(DeviceRow, UUID(str(claimed["device_id"])))
                if device is None or device.revoked_at is not None:
                    raise HTTPException(status_code=401, detail="Invalid or revoked device")
                row = await session.get(VoiceSession, session_id)
                ctx = ActorContext(
                    actor=f"device:{device.name}",
                    device_id=device.id,
                    is_master=False,
                    device=device,
                )
                if row is not None:
                    _guard_session(row, ctx)
        except HTTPException as exc:
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "unauthorized" if exc.status_code == 401 else "forbidden",
                    "message": str(exc.detail),
                    "fatal": True,
                }
            )
            await websocket.close(code=4001 if exc.status_code == 401 else 4003)
            return
    else:
        authorization = _ws_authorization(websocket, token)
        try:
            async with SessionLocal() as session:
                actor, device = await _resolve_actor(authorization, session)
                ctx = ActorContext(
                    actor=actor,
                    device_id=device.id if device else None,
                    is_master=device is None,
                    device=device,
                )
                row = await session.get(VoiceSession, session_id)
                if row is not None:
                    _guard_session(row, ctx)
        except HTTPException as exc:
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "unauthorized" if exc.status_code == 401 else "forbidden",
                    "message": str(exc.detail),
                    "fatal": True,
                }
            )
            await websocket.close(code=4001 if exc.status_code == 401 else 4003)
            return

    try:
        live = await bind_live_session(session_id=session_id, ctx=ctx)
        if claimed:
            live.client_instance_id = str(claimed.get("instance_id") or "")
        if getattr(live, "memory_scope", None) == "sandbox":
            from app.device_gateway.live_fence import fence_sandbox_lives

            await fence_sandbox_lives(except_live=live)
    except VoiceError as exc:
        await websocket.send_json(
            {
                "type": "error",
                "code": exc.code,
                "message": exc.message,
                "fatal": True,
            }
        )
        await websocket.close(code=4004)
        return

    await serve_live_websocket(websocket, live=live)


_AUDIO_CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".bin": "application/octet-stream",
}


@router.get("/audio/{key:path}")
async def stream_audio(
    key: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> StreamingResponse:
    """Stream a persisted audio object (allowlisted to ``voice/**``)."""

    from app.storage.object_store import get_object_store

    if not key.startswith("voice/") or any(part in ("..", "") for part in key.split("/")):
        raise HTTPException(status_code=404, detail="Audio not found")
    store = get_object_store()
    try:
        data = await store.get(key)
    except Exception as exc:  # noqa: BLE001 - missing object -> 404
        raise HTTPException(status_code=404, detail="Audio not found") from exc
    content_type = _AUDIO_CONTENT_TYPES.get(
        key[key.rfind(".") :].lower() if "." in key else "", "application/octet-stream"
    )
    headers = {
        "Content-Type": content_type,
        "Cache-Control": "public, max-age=3600",
        "X-Content-Type-Options": "nosniff",
    }
    range_header = request.headers.get("range")
    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if match:
            start = int(match.group(1)) if match.group(1) else 0
            end = int(match.group(2)) if match.group(2) else len(data) - 1
            if start < len(data) and end >= start:
                headers["Content-Range"] = f"bytes {start}-{min(end, len(data) - 1)}/{len(data)}"
                headers["Accept-Ranges"] = "bytes"
                chunk = data[start : min(end, len(data) - 1) + 1]
                return StreamingResponse(
                    _chunked(chunk),
                    status_code=206,
                    headers=headers,
                )
    headers["Accept-Ranges"] = "bytes"
    return StreamingResponse(_chunked(data), headers=headers)


async def _chunked(data: bytes):
    size = 64 * 1024
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]


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
        await session.commit()
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
        await session.commit()
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
