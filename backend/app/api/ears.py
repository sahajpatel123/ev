"""Always-on ears ingest: wake + owner-verified follow-up."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext, require_actor_context
from app.config import settings
from app.db import get_session
from app.identity import service as identity_service
from app.models import VoiceSession
from app.schemas import EarsWakeRequest, EarsWakeResponse, TtsOut
from app.voice.lifecycle import VoiceError, VoiceRuntime
from app.voice.speaker import default_speaker_verifier

router = APIRouter(prefix="/v1/ears", tags=["ears"])


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


@router.post("/wake", response_model=EarsWakeResponse)
async def ears_wake(
    data: EarsWakeRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> EarsWakeResponse:
    runtime = _runtime(session)
    device_id = str(ctx.device_id) if ctx.is_device else data.device_id
    try:
        outcome = await runtime.handle_ears_ingest(
            device_id=device_id,
            frames_b64=data.frames_b64,
            sample_rate=data.sample_rate,
            wake_confidence=data.wake_confidence,
            consent=data.consent,
            audio_ref=data.audio_ref,
            text_hint=data.text_hint,
            defer_command=data.defer_command,
        )
    except VoiceError as exc:
        await session.commit()
        raise _http(exc) from exc
    if outcome.session_id is not None:
        row = await session.get(VoiceSession, UUID(outcome.session_id))
        owner = await identity_service.get_owner(session)
        if row is not None and owner is not None:
            row.owner_id = owner.id
    await session.commit()
    tts = None
    if outcome.playback_owner != "ears":
        outcome.tts = None
    if outcome.tts is not None:
        audio_b64 = None
        if outcome.tts.audio:
            import base64

            audio_b64 = base64.b64encode(outcome.tts.audio).decode("ascii")
        tts = TtsOut(
            provider=outcome.tts.provider,
            audio_ref=outcome.tts.audio_ref,
            audio_b64=audio_b64,
            content_type=outcome.tts.content_type,
            ssml=outcome.tts.ssml,
            duration_ms=outcome.tts.duration_ms,
            degraded=outcome.tts.degraded,
        )
    return EarsWakeResponse(
        accepted=outcome.accepted,
        message=outcome.message,
        session_id=UUID(outcome.session_id) if outcome.session_id else None,
        state=outcome.state,
        listening=outcome.listening,
        queued=outcome.queued,
        transcript=outcome.transcript,
        reply=outcome.reply,
        tts=tts,
        playback_owner=outcome.playback_owner,
        command_deferred=outcome.command_deferred,
    )
