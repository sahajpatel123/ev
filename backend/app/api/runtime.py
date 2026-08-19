"""24/7 runtime API: state machine, wake arbitration, heartbeats, actions, DLQ."""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    ActorContext,
    require_actor,
    require_actor_context,
    require_master,
    require_owner_trust,
    require_reverification,
)
from app.db import get_session
from app.ev.actions import list_action_specs
from app.models import ApprovedAction, DeadLetter, LifeOutboundAction
from app.schemas import (
    ActionDecisionRequest,
    ActionSpecOut,
    ApprovedActionCreate,
    ApprovedActionOut,
    DeadLetterCreate,
    DeadLetterOut,
    LifeJobOut,
    LookoutComposeIn,
    LookoutDismissIn,
    LookoutListOut,
    MemoryDelta,
    NotificationCreate,
    NotificationOut,
    NotifyStatusOut,
    PresenceShowIn,
    PresenceShowOut,
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
    tts_device_id = result.get("tts_device_id")
    return RuntimeUtteranceResponse(
        session_id=current.id,
        state=result.get("state") or current.state,
        transcript=result["transcript"].text,
        transcript_confidence=result["transcript"].confidence,
        reply=result["reply"],
        conversation_id=UUID(result["conversation_id"]) if result["conversation_id"] else None,
        tts_device_id=UUID(str(tts_device_id)) if tts_device_id else None,
        tts=(
            TtsOut(
                provider=result["tts"].provider,
                audio_ref=result["tts"].audio_ref,
                audio_b64=(
                    base64.b64encode(result["tts"].audio).decode("ascii")
                    if getattr(result["tts"], "audio", None)
                    and len(result["tts"].audio) <= 1_500_000
                    else None
                ),
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


@router.get("/transcript")
async def transcript(
    since: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    """JSON snapshot of the owner's live conversation thread."""
    from app.ev.fleet import list_transcript

    payload = await list_transcript(session, since=since, limit=limit)
    await session.commit()
    return payload


@router.get("/transcript/stream")
async def transcript_stream(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
):
    """SSE stream of new live-thread events for web/Mac lookout."""
    import asyncio
    import json

    from fastapi.responses import StreamingResponse

    from app.ev.fleet import list_transcript

    first = await list_transcript(session, limit=50)
    await session.commit()
    last_ids = {item["id"] for item in first["events"]}

    async def events():
        yield f"data: {json.dumps(first)}\n\n"
        for _ in range(120):
            await asyncio.sleep(1.0)
            nxt = await list_transcript(session, limit=50)
            fresh = [item for item in nxt["events"] if item["id"] not in last_ids]
            if fresh:
                last_ids.update(item["id"] for item in fresh)
                yield f"data: {json.dumps({'conversation_id': nxt['conversation_id'], 'events': fresh})}\n\n"
            else:
                yield ":\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/lock-all")
async def lock_all(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> dict:
    """Master key: revoke every device token."""
    from app.ev.tools import dispatch

    result = await dispatch(
        session,
        "lock_everything",
        {"confirm": True},
        actor=actor,
        allow_sensitive=True,
        channel="action",
        request_id="runtime-lock-all",
    )
    payload = result.result if isinstance(result.result, dict) else {
        "ok": result.ok,
        "error": result.error,
    }
    await session.commit()
    return payload


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
    """Batch pending non-urgent alerts into one delivered digest.

    Alerts are marked delivered only after the digest notification receipt
    proves backend delivery (never from a dashboard flip).
    """
    from app.notify.service import build_and_deliver_digest

    result = await build_and_deliver_digest(session)
    if result is None:
        return {
            "schema_version": "ev.runtime.digest.v1",
            "digest_id": None,
            "generated_at": datetime.now().astimezone().isoformat(),
            "delivered": 0,
            "suppressed": 0,
            "failed": 0,
            "alerts": [],
        }
    await runtime_service.record_runtime_event(
        session,
        kind="digest",
        payload={
            "digest_id": result["digest_id"],
            "delivered": result["delivered"],
            "suppressed": result["suppressed"],
            "failed": result["failed"],
        },
    )
    await session.commit()
    return result


@router.post("/notify", response_model=NotificationOut, status_code=201)
async def send_notification(
    data: NotificationCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> NotificationOut:
    """Manually dispatch one notification through the configured backend."""
    from app.notify.service import dispatch_notification

    row = await dispatch_notification(
        session,
        title=data.title,
        body=data.body,
        priority=data.priority,
        tier=data.tier,
        kind=data.kind,
        source=data.source,
        emergency=data.emergency,
    )
    await session.commit()
    return NotificationOut.model_validate(row)


@router.get("/notifications", response_model=list[NotificationOut])
async def list_notifications(
    status_filter: Literal[
        "attempted", "delivered", "failed", "suppressed"
    ] | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[NotificationOut]:
    """Delivery receipts: every attempted/suppressed/failed notification."""
    from app.models import Notification

    stmt = (
        select(Notification)
        .order_by(Notification.queued_at.desc())
        .limit(limit)
    )
    if status_filter:
        stmt = stmt.where(Notification.status == status_filter)
    rows = (await session.execute(stmt)).scalars().all()
    return [NotificationOut.model_validate(row) for row in rows]


@router.get("/notify/status", response_model=NotifyStatusOut)
async def notify_status(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> NotifyStatusOut:
    """Backend availability, macOS permission, and today's receipt counts."""
    from app.notify.service import notify_status as service_status

    report = await service_status(session)
    return NotifyStatusOut.model_validate(report)


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


async def _require_independent_approve_factor(
    session: AsyncSession,
    ctx: ActorContext,
    data: ActionDecisionRequest | None,
    x_ev_reverify: str | None,
) -> None:
    """Master, purpose-bound reverify, or a verified WebAuthn assertion."""

    if ctx.is_master:
        return
    assertion = data.webauthn if data is not None else None
    if assertion is not None:
        from app.identity.service import IdentityError, verify_webauthn_assertion

        try:
            await verify_webauthn_assertion(
                session,
                challenge_id=assertion.challenge_id,
                credential_id=assertion.credential_id,
                client_data_json=assertion.client_data_json,
                authenticator_data=assertion.authenticator_data,
                signature=assertion.signature,
            )
        except IdentityError as exc:
            raise HTTPException(
                status_code=exc.status,
                detail=exc.message,
                headers={"X-Error-Code": exc.code},
            ) from exc
        return
    if not x_ev_reverify:
        raise HTTPException(
            status_code=403,
            detail="Re-verification required for this sensitive action",
            headers={"X-Error-Code": "reverification_required"},
        )
    from app.identity.service import IdentityError, consume_reverification

    try:
        await consume_reverification(
            session,
            token=x_ev_reverify,
            purpose="runtime.action",
            ctx=ctx,
        )
    except IdentityError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail=exc.message,
            headers={"X-Error-Code": exc.code},
        ) from exc


@router.post("/actions/{action_id}/approve", response_model=ApprovedActionOut)
async def approve_action(
    action_id: UUID,
    data: ActionDecisionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
    x_ev_reverify: str | None = Header(default=None, alias="X-EV-Reverify"),
) -> ApprovedActionOut:
    await _require_independent_approve_factor(session, ctx, data, x_ev_reverify)
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
        await session.commit()
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


@router.get("/life-jobs", response_model=list[LifeJobOut])
async def list_life_jobs(
    status_filter: str | None = Query(default=None, alias="status"),
    lifecycle_filter: str | None = Query(default=None, alias="lifecycle"),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[LifeJobOut]:
    """Outbound life-action jobs with the full lifecycle (never fake success)."""
    stmt = (
        select(LifeOutboundAction)
        .order_by(LifeOutboundAction.created_at.desc())
        .limit(limit)
    )
    if status_filter:
        stmt = stmt.where(LifeOutboundAction.status == status_filter)
    if lifecycle_filter:
        stmt = stmt.where(LifeOutboundAction.lifecycle == lifecycle_filter)
    rows = (await session.execute(stmt)).scalars().all()
    return [LifeJobOut.model_validate(row) for row in rows]


@router.post("/life-jobs/{job_id}/claim", response_model=LifeJobOut)
async def claim_life_job(
    job_id: UUID,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> LifeJobOut:
    """A device acknowledges that it has picked up its dispatched job."""
    from app.notify.routing import claim_life_job

    if ctx.device_id is None:
        raise HTTPException(status_code=403, detail="Claiming requires a device token")
    try:
        row = await claim_life_job(session, job_id, device_id=ctx.device_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return LifeJobOut.model_validate(row)


@router.post("/present", response_model=PresenceShowOut)
async def present_overlay(
    data: PresenceShowIn,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> PresenceShowOut:
    """Open EVIE's native HUD on the owner's Mac. Never a fake success."""
    from app.ev.tools import dispatch

    response = await dispatch(
        session,
        "present",
        {
            "title": data.title,
            "body": data.body,
            "kind": data.kind,
            "size": data.size,
            "time_type": data.time_type,
            "placement": data.placement,
            "ttl_ms": data.ttl_ms,
            "items": data.items,
            "questions": data.questions,
            "response": data.response,
            "layout": data.layout,
            "recommendation": data.recommendation,
            "source": data.source,
            "lookout": data.lookout,
            "window_id": data.window_id,
        },
        actor=ctx.actor,
        device_id=ctx.device_id,
        channel="action",
    )
    await session.commit()
    outcome = response.result if isinstance(response.result, dict) else {
        "ok": False,
        "opened": False,
        "reason": response.error or "present_failed",
    }
    outcome.setdefault("ok", response.ok)
    outcome.setdefault("opened", False)
    return PresenceShowOut.model_validate(outcome)


@router.post("/lookouts", response_model=PresenceShowOut)
async def compose_lookouts(
    data: LookoutComposeIn,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PresenceShowOut:
    """Let surface intelligence plan (and optionally open) HUD windows."""
    from app.ev.lookout import compose_and_maybe_open, fill_windows_from_state, plan_surfaces

    if data.open:
        payload = await compose_and_maybe_open(
            session,
            message=data.message,
            reply=data.body,
            title=data.title,
            explicit=data.explicit,
        )
        return PresenceShowOut(
            ok=True,
            opened=bool(payload.get("opened")),
            surface=str(payload.get("surface") or "overlay"),
            url=payload.get("url"),
            via=payload.get("via"),
            degraded=bool(payload.get("degraded")),
            reason=payload.get("reason"),
            windows=list(payload.get("windows") or []),
            plan=payload,
        )
    plan = plan_surfaces(data.message, explicit=data.explicit, title=data.title, body=data.body or "")
    await fill_windows_from_state(session, plan)
    payload = plan.as_dict()
    return PresenceShowOut(
        ok=True,
        opened=False,
        surface="overlay",
        windows=payload["windows"],
        plan=payload,
    )


@router.get("/lookouts", response_model=LookoutListOut)
async def list_lookouts(actor: str = Depends(require_actor)) -> LookoutListOut:
    from app.notify.lookouts import list_windows

    return LookoutListOut(windows=list_windows())


@router.post("/lookouts/dismiss")
async def dismiss_lookouts(
    data: LookoutDismissIn,
    actor: str = Depends(require_actor),
) -> dict:
    from app.notify.presence import dismiss_presence

    return await dismiss_presence(data.window_id)
