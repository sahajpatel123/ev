"""24/7 runtime & device coordination.

Centralized state machine (idle -> verifying -> awake -> processing ->
responding -> follow_up -> idle), multi-device wake arbitration, device
heartbeats, approved-action routing, and dead-letter recovery. Failures stay
observable and recoverable instead of silently disappearing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.ev_sense import quiet_hours_active
from app.models import (
    ApprovedAction,
    DeadLetter,
    Device,
    Prediction,
    RuntimeHeartbeat,
    RuntimeSession,
)
from app.schemas import (
    ApprovedActionCreate,
    RuntimeDeviceOut,
    RuntimeHeartbeatCreate,
    RuntimeStatusOut,
    WakeArbitrationOut,
    WakeCandidateOut,
    WakeIntent,
)
from app.utils.text import utcnow

RUNTIME_STATES = ("idle", "verifying", "awake", "processing", "responding", "follow_up")

LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "idle": {"verifying"},
    "verifying": {"awake", "idle"},
    "awake": {"processing", "idle"},
    "processing": {"responding", "idle"},
    "responding": {"follow_up", "idle"},
    "follow_up": {"idle"},
}

# Action type -> requires approval. Unknown action types default to requiring
# approval (safe default for anything that can touch the outside world).
ACTION_PERMISSIONS: dict[str, bool] = {
    "search_memory": False,
    "hud_card": False,
    "notification": True,
    "fleet_task": True,
    "web_search": True,
    "send_message": True,
    "execute_command": True,
}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=utcnow().tzinfo)


def _state_timeout(state: str) -> timedelta:
    return timedelta(
        seconds={
            "verifying": settings.runtime_verify_timeout_seconds,
            "awake": settings.runtime_awake_timeout_seconds,
            "processing": settings.runtime_processing_timeout_seconds,
            "responding": settings.runtime_respond_timeout_seconds,
            "follow_up": settings.runtime_followup_timeout_seconds,
        }.get(state, 0)
    )


async def active_session(session: AsyncSession) -> RuntimeSession | None:
    result = await session.execute(
        select(RuntimeSession)
        .where(RuntimeSession.ended_at.is_(None))
        .order_by(RuntimeSession.started_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def expire_stale(session: AsyncSession, now: datetime | None = None) -> RuntimeSession | None:
    """Timeout or quiet-hours-expire any active session that should return to idle."""
    now = now or utcnow()
    current = await active_session(session)
    if current is None:
        return None
    updated_at = _aware(current.updated_at) or now
    if current.state in ("verifying", "awake", "processing", "responding", "follow_up"):
        if now - updated_at > _state_timeout(current.state):
            await transition(session, current, "idle", reason=f"{current.state}_timeout")
        elif current.state in ("awake", "follow_up") and quiet_hours_active(now):
            await transition(session, current, "idle", reason="quiet_hours")
    return current


async def transition(
    session: AsyncSession,
    runtime_session: RuntimeSession,
    to_state: str,
    *,
    reason: str | None = None,
) -> RuntimeSession:
    if to_state not in LEGAL_TRANSITIONS.get(runtime_session.state, set()):
        raise ValueError(
            f"Illegal runtime transition {runtime_session.state} -> {to_state}"
        )
    runtime_session.state = to_state
    runtime_session.updated_at = utcnow()
    if to_state == "idle":
        runtime_session.ended_at = utcnow()
        runtime_session.end_reason = reason or "done"
    await session.flush()
    return runtime_session


async def _device_map(
    session: AsyncSession, device_ids: list[UUID]
) -> dict[UUID, Device]:
    if not device_ids:
        return {}
    rows = (
        await session.execute(select(Device).where(Device.id.in_(device_ids)))
    ).scalars().all()
    return {device.id: device for device in rows}


async def arbitrate_wake(
    session: AsyncSession,
    intents: list[WakeIntent],
    now: datetime | None = None,
) -> WakeArbitrationOut:
    """Pick the closest/most capable online device as the wake winner."""
    now = now or utcnow()
    devices = await _device_map(session, [intent.device_id for intent in intents])
    grace = timedelta(seconds=settings.runtime_heartbeat_grace_seconds)
    candidates: list[WakeCandidateOut] = []
    best: tuple[float, WakeIntent, Device] | None = None

    for intent in intents:
        device = devices.get(intent.device_id)
        if device is None or device.revoked_at is not None:
            candidates.append(
                WakeCandidateOut(
                    device_id=intent.device_id,
                    name=device.name if device else "unknown",
                    reason="unknown_or_revoked",
                )
            )
            continue
        caps = {c.lower() for c in (device.capabilities or [])}
        if not (caps & {"wake", "voice"}):
            candidates.append(
                WakeCandidateOut(
                    device_id=device.id,
                    name=device.name,
                    reason="no_wake_capability",
                )
            )
            continue
        last_seen = _aware(device.last_seen_at)
        if last_seen is None or now - last_seen > grace:
            candidates.append(
                WakeCandidateOut(
                    device_id=device.id,
                    name=device.name,
                    reason="offline",
                )
            )
            continue

        recency = 1.0 if now - last_seen <= timedelta(seconds=60) else 0.5
        battery = (intent.battery_percent or 0) / 100 if intent.battery_percent is not None else 0.5
        proximity = intent.proximity_score if intent.proximity_score is not None else 0.5
        score = round(
            0.45 * intent.signal_score
            + 0.25 * battery
            + 0.2 * proximity
            + 0.1 * recency,
            4,
        )
        candidates.append(
            WakeCandidateOut(
                device_id=device.id,
                name=device.name,
                score=score,
                reason="candidate",
            )
        )
        if best is None or score > best[0]:
            best = (score, intent, device)

    if best is None:
        return WakeArbitrationOut(
            candidates=candidates,
            state="idle",
            blocked=True,
            block_reason="no_eligible_device",
        )

    score, intent, device = best
    if quiet_hours_active(now) and intent.priority < settings.runtime_urgent_priority_threshold:
        for candidate in candidates:
            if candidate.device_id == device.id:
                candidate.reason = "quiet_hours"
        return WakeArbitrationOut(
            candidates=candidates,
            state="idle",
            blocked=True,
            block_reason="quiet_hours",
        )

    await expire_stale(session, now)
    prior = await active_session(session)
    if prior is not None:
        await transition(session, prior, "idle", reason="superseded_by_new_wake")

    runtime_session = RuntimeSession(
        state="verifying",
        device_id=device.id,
        wake_signal=intent.signal_score,
        priority=intent.priority,
        payload=intent.payload,
        started_at=now,
        updated_at=now,
    )
    session.add(runtime_session)
    await session.flush()

    for candidate in candidates:
        candidate.selected = candidate.device_id == device.id and candidate.score == score
        if candidate.selected:
            candidate.reason = "winner"

    return WakeArbitrationOut(
        winner=WakeCandidateOut(
            device_id=device.id,
            name=device.name,
            score=score,
            selected=True,
            reason="winner",
        ),
        candidates=candidates,
        state="verifying",
        session_id=runtime_session.id,
    )


async def record_heartbeat(
    session: AsyncSession,
    data: RuntimeHeartbeatCreate,
    now: datetime | None = None,
) -> RuntimeHeartbeat:
    now = now or utcnow()
    device = await session.get(Device, data.device_id)
    if device is None or device.revoked_at is not None:
        raise KeyError(f"Device {data.device_id} not found or revoked")
    device.last_seen_at = now
    heartbeat = RuntimeHeartbeat(
        device_id=device.id,
        reported_at=now,
        status=data.status,
        listener_state=data.listener_state,
        battery_percent=data.battery_percent,
        latency_ms=data.latency_ms,
        details=data.details,
    )
    session.add(heartbeat)
    current = await active_session(session)
    if current is not None and current.device_id == device.id:
        current.last_heartbeat_at = now
        current.updated_at = now  # liveness: a heartbeat refreshes the session timeout
        await session.flush()
    return heartbeat


async def route_action(
    session: AsyncSession,
    data: ApprovedActionCreate,
    *,
    requested_by: str,
    device_id: UUID | None = None,
    force_requires_approval: bool = False,
) -> ApprovedAction:
    requires_approval = force_requires_approval or ACTION_PERMISSIONS.get(data.action_type, True)
    current = await active_session(session)
    approved = data.auto_approve and not requires_approval
    action = ApprovedAction(
        action_type=data.action_type,
        title=data.title,
        payload=data.payload,
        requires_approval=requires_approval,
        status="approved" if approved else "pending",
        requested_by=requested_by,
        device_id=device_id,
        session_id=current.id if current else None,
        approved_at=utcnow() if approved else None,
        approved_by="system" if approved else None,
    )
    session.add(action)
    await session.flush()
    return action


async def decide_action(
    session: AsyncSession,
    action_id: UUID,
    *,
    actor: str,
    decision: Literal["approve", "deny"],
    reason: str | None = None,
) -> ApprovedAction:
    action = await session.get(ApprovedAction, action_id)
    if action is None:
        raise KeyError(f"Action {action_id} not found")
    if action.status != "pending":
        raise ValueError(f"Action is already {action.status}")
    now = utcnow()
    if decision == "approve":
        action.status = "approved"
        action.approved_at = now
        action.approved_by = actor
    else:
        action.status = "denied"
        action.denied_at = now
        action.denied_reason = reason or "denied"
    action.updated_at = now
    await session.flush()
    return action


async def execute_action(
    session: AsyncSession,
    action_id: UUID,
    *,
    actor: str,
    result: dict | None = None,
) -> ApprovedAction:
    action = await session.get(ApprovedAction, action_id)
    if action is None:
        raise KeyError(f"Action {action_id} not found")
    if action.status != "approved":
        raise ValueError("Only approved actions can be executed")
    action.status = "executed"
    action.executed_at = utcnow()
    action.result = result or {}
    action.updated_at = utcnow()
    await session.flush()
    return action


async def fail_action(session: AsyncSession, action_id: UUID, *, error: str) -> ApprovedAction:
    action = await session.get(ApprovedAction, action_id)
    if action is None:
        raise KeyError(f"Action {action_id} not found")
    action.status = "failed"
    action.error = error
    action.updated_at = utcnow()
    await session.flush()
    return action


async def record_dead_letter(
    session: AsyncSession,
    *,
    queue: str,
    payload: dict,
    error: str,
    job_id: str | None = None,
) -> DeadLetter:
    now = utcnow()
    if job_id:
        existing = (
            await session.execute(
                select(DeadLetter).where(
                    DeadLetter.queue == queue,
                    DeadLetter.job_id == job_id,
                    DeadLetter.status.in_(["new", "retrying"]),
                )
            )
        ).scalars().first()
        if existing is not None:
            existing.attempts += 1
            existing.error = error
            existing.last_error_at = now
            existing.status = (
                "discarded"
                if existing.attempts >= settings.runtime_dlq_max_attempts
                else "new"
            )
            await session.flush()
            return existing
    letter = DeadLetter(
        queue=queue,
        job_id=job_id,
        payload=payload,
        error=error,
        attempts=1,
        status="new",
        last_error_at=now,
    )
    session.add(letter)
    await session.flush()
    return letter


async def retry_dead_letter(session: AsyncSession, letter_id: UUID) -> DeadLetter:
    letter = await session.get(DeadLetter, letter_id)
    if letter is None:
        raise KeyError(f"Dead letter {letter_id} not found")
    if letter.status == "resolved":
        raise ValueError("Resolved dead letters cannot be retried")
    letter.status = "retrying"
    await session.flush()
    return letter


async def discard_dead_letter(session: AsyncSession, letter_id: UUID) -> DeadLetter:
    letter = await session.get(DeadLetter, letter_id)
    if letter is None:
        raise KeyError(f"Dead letter {letter_id} not found")
    letter.status = "discarded"
    await session.flush()
    return letter


async def resolve_dead_letter(session: AsyncSession, letter_id: UUID) -> DeadLetter:
    letter = await session.get(DeadLetter, letter_id)
    if letter is None:
        raise KeyError(f"Dead letter {letter_id} not found")
    letter.status = "resolved"
    letter.resolved_at = utcnow()
    await session.flush()
    return letter


async def dead_letter_summary(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(DeadLetter.status, func.count(DeadLetter.id)).group_by(DeadLetter.status)
        )
    ).all()
    summary = {"new": 0, "retrying": 0, "discarded": 0, "resolved": 0}
    summary.update({status: count for status, count in rows})
    return summary


async def attention_usage(session: AsyncSession) -> dict:
    now = utcnow()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    delivered_today = int(
        (
            await session.execute(
                select(func.count(Prediction.id)).where(
                    Prediction.created_at >= start_of_day,
                    Prediction.intervention_score >= 0.35,
                )
            )
        ).scalar_one()
    )
    return {
        "delivered_today": delivered_today,
        "budget": settings.daily_alert_budget,
        "remaining": max(0, settings.daily_alert_budget - delivered_today),
    }


async def runtime_status(session: AsyncSession) -> RuntimeStatusOut:
    await expire_stale(session)
    current = await active_session(session)
    if current is None:
        latest = (
            await session.execute(
                select(RuntimeSession).order_by(RuntimeSession.started_at.desc()).limit(1)
            )
        ).scalars().first()
    else:
        latest = current
    device_rows = list(
        (
            await session.execute(
                select(Device).where(Device.revoked_at.is_(None))
            )
        ).scalars().all()
    )
    now = utcnow()
    grace = timedelta(seconds=settings.runtime_heartbeat_grace_seconds)
    devices: list[RuntimeDeviceOut] = []
    online_count = 0
    for device in device_rows:
        heartbeat = (
            await session.execute(
                select(RuntimeHeartbeat)
                .where(RuntimeHeartbeat.device_id == device.id)
                .order_by(RuntimeHeartbeat.reported_at.desc())
                .limit(1)
            )
        ).scalars().first()
        last_seen = _aware(device.last_seen_at)
        if last_seen is None:
            presence: Literal["online", "away", "unknown"] = "unknown"
        elif now - last_seen <= grace:
            presence = "online"
            online_count += 1
        elif now - last_seen <= timedelta(days=1):
            presence = "away"
        else:
            presence = "unknown"
        devices.append(
            RuntimeDeviceOut(
                device_id=device.id,
                name=device.name,
                presence=presence,
                listener_state=heartbeat.listener_state if heartbeat else None,
                battery_percent=heartbeat.battery_percent if heartbeat else None,
                last_seen_at=device.last_seen_at,
                last_heartbeat_at=heartbeat.reported_at if heartbeat else None,
            )
        )

    pending_actions = int(
        (
            await session.execute(
                select(func.count(ApprovedAction.id)).where(ApprovedAction.status == "pending")
            )
        ).scalar_one()
    )
    return RuntimeStatusOut(
        state=current.state if current else "idle",
        session=latest,
        devices=devices,
        online_count=online_count,
        quiet_hours_active=quiet_hours_active(now),
        attention=await attention_usage(session),
        actions_pending=pending_actions,
        dead_letters=await dead_letter_summary(session),
        generated_at=now,
    )


def record_dead_letter_sync(*, queue: str, payload: dict, error: str, job_id: str | None = None) -> None:
    """Sync helper for RQ worker entrypoints (no running event loop there)."""
    import asyncio

    from app.db import SessionLocal

    async def _go() -> None:
        async with SessionLocal() as db_session:
            await record_dead_letter(
                db_session, queue=queue, payload=payload, error=error, job_id=job_id
            )
            await db_session.commit()

    asyncio.run(_go())
