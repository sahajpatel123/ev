"""24/7 runtime API: state machine, wake arbitration, heartbeats, actions, DLQ."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor
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
    RuntimeHeartbeatCreate,
    RuntimeHeartbeatOut,
    RuntimeStatusOut,
    RuntimeTransitionRequest,
    RuntimeVerifyRequest,
    RuntimeVerifyResponse,
    WakeArbitrationOut,
    WakeIntent,
)
from app.services import runtime as runtime_service
from app.voice.anti_spoof import ReplayError

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
    await session.commit()
    return {
        "schema_version": "ev.runtime.sync.v1",
        "generated_at": datetime.now().astimezone().isoformat(),
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
    actor: str = Depends(require_actor),
) -> ApprovedActionOut:
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
    actor: str = Depends(require_actor),
) -> ApprovedActionOut:
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
    actor: str = Depends(require_actor),
) -> ApprovedActionOut:
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
    actor: str = Depends(require_actor),
) -> ApprovedActionOut:
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
