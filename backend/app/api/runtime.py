"""24/7 runtime API: state machine, wake arbitration, heartbeats, actions, DLQ."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    ActorContext,
    require_actor,
    require_actor_context,
    require_owner_trust,
    require_reverification,
)
from app.db import get_session
from app.ev.actions import list_action_specs
from app.models import ApprovedAction, DeadLetter
from app.schemas import (
    ActionDecisionRequest,
    ActionSpecOut,
    ApprovedActionCreate,
    ApprovedActionOut,
    DeadLetterCreate,
    DeadLetterOut,
    MemoryDelta,
    RuntimeHeartbeatCreate,
    RuntimeHeartbeatOut,
    RuntimeStatusOut,
    RuntimeTransitionRequest,
    RuntimeUtteranceRequest,
    RuntimeUtteranceResponse,
    RuntimeVerifyRequest,
    RuntimeVerifyResponse,
    SpeechStyleOut,
    TtsOut,
    WakeArbitrationOut,
    WakeIntent,
)
from app.services import runtime as runtime_service
from app.voice.anti_spoof import ReplayError
from app.voice.lifecycle import VoiceError

router = APIRouter(prefix="/v1/runtime")


@router.post("/heartbeat", response_model=RuntimeHeartbeatOut, status_code=201)
async def heartbeat(
    data: RuntimeHeartbeatCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RuntimeHeartbeatOut:
    try:
        row = await runtime_service.record_heartbeat(session, data)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    await session.commit()
    return RuntimeHeartbeatOut.model_validate(row)


@router.post("/wake", response_model=WakeArbitrationOut)
async def wake(
    intents: list[WakeIntent],
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> WakeArbitrationOut:
    outcome = await runtime_service.arbitrate_wake(session, intents)
    await session.commit()
    return outcome


@router.post("/transition", response_model=RuntimeStatusOut)
async def transition(
    data: RuntimeTransitionRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RuntimeStatusOut:
    current = await runtime_service.active_session(session)
    if current is None:
        raise HTTPException(status_code=409, detail="No active runtime session")
    try:
        await runtime_service.transition(session, current, data.to_state, reason=data.reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return await runtime_service.runtime_status(session)


@router.post("/verify", response_model=RuntimeVerifyResponse)
async def verify_owner(
    data: RuntimeVerifyRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RuntimeVerifyResponse:
    """Owner speaker verification with anti-spoofing for the active wake."""
    current = await runtime_service.active_session(session)
    if current is None:
        raise HTTPException(status_code=409, detail="No active runtime session")
    if current.id != data.session_id:
        raise HTTPException(status_code=409, detail="Session is not the active wake")
    try:
        result = await runtime_service.verify_owner(
            session,
            current,
            nonce=data.nonce,
            samples=data.samples,
            phrase=data.phrase,
            liveness_proof=data.liveness_proof,
            live_score=data.live_score,
            audio_sha256=data.audio_sha256,
        )
    except ReplayError as exc:
        await session.commit()
        raise HTTPException(
            status_code=403,
            detail=f"Replay attack rejected: {exc}",
            headers={"X-Error-Code": "replay_rejected"},
        ) from exc
    await session.commit()
    status = await runtime_service.runtime_status(session)
    return RuntimeVerifyResponse(
        session_id=current.id,
        verified=result["verified"],
        state=status.state,
        confidence=result.get("confidence", 0.0),
        reason=result.get("reason", ""),
    )


@router.post("/utterance", response_model=RuntimeUtteranceResponse)
async def utterance(
    data: RuntimeUtteranceRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> RuntimeUtteranceResponse:
    """Listen → understand → act → reply on the centralized runtime session."""
    current = await runtime_service.active_session(session)
    if current is None:
        raise HTTPException(status_code=409, detail="No active runtime session")
    if data.session_id is not None and current.id != data.session_id:
        raise HTTPException(status_code=409, detail="Session is not the active wake")
    try:
        result = await runtime_service.handle_utterance(
            session,
            current,
            text=data.text,
            audio_b64=data.audio_b64,
            audio_ref=data.audio_ref,
            language=data.language,
            conversation_id=data.conversation_id,
            follow_up=data.follow_up,
            reverify_token=data.reverify_token,
            ctx=ctx,
        )
    except VoiceError as exc:
        await session.commit()
        raise HTTPException(
            status_code=exc.status,
            detail=exc.message,
            headers={"X-Error-Code": exc.code},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return RuntimeUtteranceResponse(
        session_id=current.id,
        state="follow_up",
        transcript=result["transcript"].text,
        transcript_confidence=result["transcript"].confidence,
        reply=result["reply"],
        conversation_id=UUID(result["conversation_id"]) if result["conversation_id"] else None,
        tts=(
            TtsOut(
                provider=result["tts"].provider,
                audio_ref=result["tts"].audio_ref,
                content_type=result["tts"].content_type,
                ssml=result["tts"].ssml,
                duration_ms=result["tts"].duration_ms,
            )
            if result["tts"]
            else None
        ),
        style=(
            SpeechStyleOut(
                urgency=result["style"].urgency,
                warmth=result["style"].warmth,
                brevity=result["style"].brevity,
                mode=result["style"].mode,
                length_target=result["style"].length_target,
                directness=result["style"].directness,
            )
            if result["style"]
            else None
        ),
        model=result["model"],
        context_tokens=result["context_tokens"],
        memory_deltas=[
            MemoryDelta.model_validate(d)
            for d in (result["memory_deltas"] or [])
            if isinstance(d, dict)
        ],
    )


@router.get("/status", response_model=RuntimeStatusOut)
async def status(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RuntimeStatusOut:
    result = await runtime_service.runtime_status(session)
    await session.commit()
    return result


@router.get("/health")
async def runtime_health(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    """Structured runtime health: DB, state machine, listeners, queue, DLQ."""
    report = await runtime_service.runtime_health(session)
    await session.commit()
    return report


@router.get("/sync")
async def runtime_sync(
    since: datetime | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    """Cross-device runtime state snapshot for convergent client sync."""
    status_out = await runtime_service.runtime_status(session)
    events = await runtime_service.list_runtime_events(
        session, since=since, limit=limit
    )
    latency = await runtime_service.runtime_latency(session)
    digest = await runtime_service.digest_state(session)
    await session.commit()
    return {
        "schema_version": "ev.runtime.sync.v1",
        "generated_at": datetime.now().astimezone().isoformat(),
        "policy": runtime_service.runtime_policy(),
        "latency": latency,
        "digest": digest,
        "runtime": {
            "state": status_out.state,
            "session_id": str(status_out.session.id) if status_out.session else None,
            "session_state": status_out.session.state if status_out.session else None,
            "device_id": str(status_out.session.device_id) if status_out.session else None,
            "quiet_hours_active": status_out.quiet_hours_active,
            "attention": status_out.attention,
            "dead_letters": status_out.dead_letters,
            "actions_pending": status_out.actions_pending,
        },
        "devices": [device.model_dump(mode="json") for device in status_out.devices],
        "events": [
            {
                "id": str(event.id),
                "occurred_at": event.occurred_at.isoformat(),
                "kind": event.kind,
                "device_id": str(event.device_id) if event.device_id else None,
                "session_id": str(event.session_id) if event.session_id else None,
                "action_id": str(event.action_id) if event.action_id else None,
                "payload": event.payload,
            }
            for event in events
        ],
    }


@router.post("/digest")
async def runtime_digest(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    """Batch pending non-urgent alerts into one quiet-hours-friendly digest."""
    from app.ev import alert_radar

    result = await alert_radar.build_digest(session)
    await runtime_service.record_runtime_event(
        session,
        kind="digest",
        payload={
            "digest_id": result["digest_id"],
            "delivered": result["delivered"],
        },
    )
    await session.commit()
    return result


@router.post("/actions", response_model=ApprovedActionOut, status_code=201)
async def route_action(
    data: ApprovedActionCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ApprovedActionOut:
    try:
        action = await runtime_service.route_action(
            session, data, requested_by=actor, device_id=data.device_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    return ApprovedActionOut.model_validate(action)


@router.get("/action-specs", response_model=list[ActionSpecOut])
async def action_specs(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[ActionSpecOut]:
    """Declared action capabilities: schemas, permissions, undoability."""
    return [ActionSpecOut.model_validate(spec) for spec in list_action_specs()]


@router.get("/actions", response_model=list[ApprovedActionOut])
async def list_actions(
    status_filter: Literal[
        "pending", "approved", "denied", "executed", "failed", "rolled_back"
    ] | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[ApprovedActionOut]:
    stmt = select(ApprovedAction).order_by(ApprovedAction.created_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(ApprovedAction.status == status_filter)
    rows = (await session.execute(stmt)).scalars().all()
    return [ApprovedActionOut.model_validate(row) for row in rows]


@router.post("/actions/{action_id}/approve", response_model=ApprovedActionOut)
async def approve_action(
    action_id: UUID,
    data: ActionDecisionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_reverification("runtime.action")),
) -> ApprovedActionOut:
    actor = ctx.actor
    try:
        action = await runtime_service.decide_action(
            session,
            action_id,
            actor=actor,
            decision="approve",
            reason=data.reason if data else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return ApprovedActionOut.model_validate(action)


@router.post("/actions/{action_id}/deny", response_model=ApprovedActionOut)
async def deny_action(
    action_id: UUID,
    data: ActionDecisionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_owner_trust),
) -> ApprovedActionOut:
    actor = ctx.actor
    try:
        action = await runtime_service.decide_action(
            session,
            action_id,
            actor=actor,
            decision="deny",
            reason=data.reason if data else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return ApprovedActionOut.model_validate(action)


@router.post("/actions/{action_id}/execute", response_model=ApprovedActionOut)
async def execute_action(
    action_id: UUID,
    data: ActionDecisionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_reverification("runtime.action")),
) -> ApprovedActionOut:
    actor = ctx.actor
    try:
        action = await runtime_service.execute_action(
            session,
            action_id,
            actor=actor,
            result=data.result if data else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return ApprovedActionOut.model_validate(action)


@router.post("/actions/{action_id}/rollback", response_model=ApprovedActionOut)
async def rollback_action(
    action_id: UUID,
    data: ActionDecisionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_reverification("runtime.action")),
) -> ApprovedActionOut:
    actor = ctx.actor
    try:
        action = await runtime_service.rollback_action(
            session,
            action_id,
            actor=actor,
            reason=data.reason if data else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return ApprovedActionOut.model_validate(action)


@router.post("/dead-letters", response_model=DeadLetterOut, status_code=201)
async def create_dead_letter(
    data: DeadLetterCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> DeadLetterOut:
    letter = await runtime_service.record_dead_letter(
        session,
        queue=data.queue,
        payload=data.payload,
        error=data.error,
        job_id=data.job_id,
    )
    await session.commit()
    return DeadLetterOut.model_validate(letter)


@router.get("/dead-letters", response_model=list[DeadLetterOut])
async def list_dead_letters(
    status_filter: Literal["new", "retrying", "discarded", "resolved"] | None = Query(
        default=None, alias="status"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[DeadLetterOut]:
    stmt = select(DeadLetter).order_by(DeadLetter.created_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(DeadLetter.status == status_filter)
    rows = (await session.execute(stmt)).scalars().all()
    return [DeadLetterOut.model_validate(row) for row in rows]


@router.post("/dead-letters/{letter_id}/retry", response_model=DeadLetterOut)
async def retry_dead_letter(
    letter_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> DeadLetterOut:
    try:
        letter = await runtime_service.retry_dead_letter(session, letter_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return DeadLetterOut.model_validate(letter)


@router.post("/dead-letters/{letter_id}/discard", response_model=DeadLetterOut)
async def discard_dead_letter(
    letter_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> DeadLetterOut:
    try:
        letter = await runtime_service.discard_dead_letter(session, letter_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    await session.commit()
    return DeadLetterOut.model_validate(letter)
