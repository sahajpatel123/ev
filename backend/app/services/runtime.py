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
from app.ev.actions import ACTION_SPECS, get_action_spec, validate_action_payload
from app.ev.ev_sense import quiet_hours_active
from app.models import (
    ApprovedAction,
    DeadLetter,
    Device,
    Prediction,
    RuntimeEvent,
    RuntimeHeartbeat,
    RuntimeSession,
    VoiceAttemptLog,
)
from app.schemas import (
    ApprovedActionCreate,
    RuntimeDeviceOut,
    RuntimeHeartbeatCreate,
    RuntimeSessionOut,
    RuntimeStatusOut,
    WakeArbitrationOut,
    WakeCandidateOut,
    WakeIntent,
)
from app.services.access_log import log_access
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

# Action type -> requires approval, derived from the formal action registry.
# Unknown action types are rejected at routing time; nothing can invoke a
# capability that is not explicitly declared.
ACTION_PERMISSIONS: dict[str, bool] = {
    str(spec["name"]): bool(spec["requires_approval"]) for spec in ACTION_SPECS
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


async def record_runtime_event(
    session: AsyncSession,
    *,
    kind: str,
    payload: dict | None = None,
    device_id: UUID | None = None,
    session_id: UUID | None = None,
    action_id: UUID | None = None,
) -> RuntimeEvent:
    """Append one immutable runtime observability event."""
    event = RuntimeEvent(
        kind=kind,
        device_id=device_id,
        session_id=session_id,
        action_id=action_id,
        payload=payload or {},
    )
    session.add(event)
    await session.flush()
    return event


async def list_runtime_events(
    session: AsyncSession,
    *,
    since: datetime | None = None,
    limit: int = 100,
    kind: str | None = None,
) -> list[RuntimeEvent]:
    stmt = (
        select(RuntimeEvent)
        .order_by(RuntimeEvent.occurred_at.desc())
        .limit(min(limit, 1000))
    )
    if since is not None:
        stmt = stmt.where(RuntimeEvent.occurred_at > since)
    if kind is not None:
        stmt = stmt.where(RuntimeEvent.kind == kind)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


def runtime_policy() -> dict:
    """Runtime arbitration/attention policy snapshot for clients and observers."""
    return {
        "quiet_hours_start": settings.quiet_hours_start,
        "quiet_hours_end": settings.quiet_hours_end,
        "daily_alert_budget": settings.daily_alert_budget,
        "urgent_priority_threshold": settings.runtime_urgent_priority_threshold,
        "verify_timeout_seconds": settings.runtime_verify_timeout_seconds,
        "awake_timeout_seconds": settings.runtime_awake_timeout_seconds,
        "processing_timeout_seconds": settings.runtime_processing_timeout_seconds,
        "respond_timeout_seconds": settings.runtime_respond_timeout_seconds,
        "followup_timeout_seconds": settings.runtime_followup_timeout_seconds,
        "heartbeat_grace_seconds": settings.runtime_heartbeat_grace_seconds,
        "dlq_max_attempts": settings.runtime_dlq_max_attempts,
        "daemon_tick_seconds": settings.runtime_daemon_tick_seconds,
    }


async def runtime_latency(
    session: AsyncSession,
    *,
    session_id: UUID | None = None,
) -> dict:
    """Wake-to-reply stage latencies for the latest wake cycle, from the event log.

    Returns None for stages that have not happened yet (e.g. the session is
    still verifying), so clients can show partial progress without guessing.
    """
    if session_id is None:
        latest = (
            await session.execute(
                select(RuntimeSession).order_by(RuntimeSession.started_at.desc()).limit(1)
            )
        ).scalars().first()
        session_id = latest.id if latest else None
    if session_id is None:
        return {
            "session_id": None,
            "wake_to_awake_ms": None,
            "wake_to_processing_ms": None,
            "wake_to_responding_ms": None,
            "wake_to_follow_up_ms": None,
        }

    events = list(
        (
            await session.execute(
                select(RuntimeEvent)
                .where(RuntimeEvent.session_id == session_id)
                .order_by(RuntimeEvent.occurred_at.asc())
            )
        ).scalars().all()
    )
    markers: dict[str, datetime] = {}
    for event in events:
        if event.kind == "wake" and "wake" not in markers:
            markers["wake"] = event.occurred_at
        if event.kind == "transition":
            to_state = (event.payload or {}).get("to_state")
            if to_state in ("awake", "processing", "responding", "follow_up"):
                markers.setdefault(to_state, event.occurred_at)
    wake_at = _aware(markers.get("wake"))
    if wake_at is None:
        return {
            "session_id": str(session_id),
            "wake_to_awake_ms": None,
            "wake_to_processing_ms": None,
            "wake_to_responding_ms": None,
            "wake_to_follow_up_ms": None,
        }

    def _ms(target: datetime | None) -> int | None:
        if target is None:
            return None
        delta = (_aware(target) or wake_at) - wake_at
        return max(0, int(delta.total_seconds() * 1000))

    return {
        "session_id": str(session_id),
        "wake_to_awake_ms": _ms(markers.get("awake")),
        "wake_to_processing_ms": _ms(markers.get("processing")),
        "wake_to_responding_ms": _ms(markers.get("responding")),
        "wake_to_follow_up_ms": _ms(markers.get("follow_up")),
    }


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
    if to_state == "awake" and not runtime_session.owner_verified:
        raise ValueError(
            "Owner speaker verification required before entering awake"
        )
    runtime_session.state = to_state
    runtime_session.updated_at = utcnow()
    if to_state == "idle":
        runtime_session.ended_at = utcnow()
        runtime_session.end_reason = reason or "done"
    await record_runtime_event(
        session,
        kind="transition",
        payload={"to_state": to_state, "reason": reason, "end_reason": runtime_session.end_reason},
        device_id=runtime_session.device_id,
        session_id=runtime_session.id,
    )
    await session.flush()
    return runtime_session


async def mark_verified(
    session: AsyncSession,
    runtime_session: RuntimeSession,
    *,
    confidence: float,
    verifier_name: str,
) -> RuntimeSession:
    """Mark a runtime session owner-verified and move it to awake."""
    if runtime_session.state != "verifying":
        raise ValueError(f"Cannot verify in state {runtime_session.state}")
    runtime_session.owner_verified = True
    runtime_session.speaker_confidence = confidence
    runtime_session.verifier_name = verifier_name
    runtime_session.verified_at = utcnow()
    await record_runtime_event(
        session,
        kind="verify",
        payload={
            "accepted": True,
            "confidence": confidence,
            "verifier": verifier_name,
        },
        device_id=runtime_session.device_id,
        session_id=runtime_session.id,
    )
    return await transition(session, runtime_session, "awake", reason="owner_verified")


async def _log_voice_attempt(
    session: AsyncSession,
    *,
    kind: str,
    outcome: str,
    session_id,
    device_id: str | None = None,
    reason: str | None = None,
    **metadata,
) -> None:
    session.add(
        VoiceAttemptLog(
            device_id=device_id,
            kind=kind,
            outcome=outcome,
            session_id=session_id,
            reason=reason,
            metadata_=metadata,
        )
    )
    await session.flush()


async def verify_owner(
    session: AsyncSession,
    runtime_session: RuntimeSession,
    *,
    nonce: str,
    samples: list[str],
    phrase: str | None = None,
    liveness_proof: str | None = None,
    live_score: float | None = None,
    audio_sha256: str | None = None,
) -> dict:
    """Anti-spoof + owner speaker verification for one runtime wake cycle."""
    from app.voice.anti_spoof import LivenessGate, ReplayError, ReplayGuard
    from app.voice.lifecycle import VoiceRuntime

    device_id = str(runtime_session.device_id) if runtime_session.device_id else None
    guard = ReplayGuard(session)
    try:
        await guard.consume(nonce, purpose="verify", session_id=runtime_session.id)
    except ReplayError as exc:
        await _log_voice_attempt(
            session,
            kind="replay",
            outcome="rejected",
            session_id=runtime_session.id,
            device_id=device_id,
            reason=str(exc),
            purpose="verify",
        )
        await record_runtime_event(
            session,
            kind="verify",
            payload={"accepted": False, "reason": f"replay:{exc}"},
            device_id=runtime_session.device_id,
            session_id=runtime_session.id,
        )
        raise

    if audio_sha256 and await guard.fingerprint_replayed(
        audio_sha256, device_id=device_id
    ):
        await _log_voice_attempt(
            session,
            kind="replay",
            outcome="rejected",
            session_id=runtime_session.id,
            device_id=device_id,
            reason="audio fingerprint already accepted",
            purpose="verify",
        )
        await record_runtime_event(
            session,
            kind="verify",
            payload={"accepted": False, "reason": "replay:audio_fingerprint"},
            device_id=runtime_session.device_id,
            session_id=runtime_session.id,
        )
        raise ReplayError("audio fingerprint already accepted")

    live_ok, live_conf, live_reason = await LivenessGate().check(
        sample={
            "liveness_proof": liveness_proof,
            "live_score": live_score,
        },
        challenge_phrase=phrase,
        expected_phrase=runtime_session.challenge_phrase,
    )
    if not live_ok:
        await _log_voice_attempt(
            session,
            kind="verify",
            outcome="rejected",
            session_id=runtime_session.id,
            device_id=device_id,
            reason=live_reason,
            liveness_confidence=live_conf,
            audio_sha256=audio_sha256,
        )
        await record_runtime_event(
            session,
            kind="verify",
            payload={"accepted": False, "reason": f"liveness:{live_reason}"},
            device_id=runtime_session.device_id,
            session_id=runtime_session.id,
        )
        return {"verified": False, "confidence": live_conf, "reason": live_reason}

    runtime = VoiceRuntime(session, master_key=settings.master_key)
    result = await runtime.verify_samples([{"audio_b64": sample} for sample in samples])
    if not result["accepted"]:
        await _log_voice_attempt(
            session,
            kind="refusal",
            outcome="refused",
            session_id=runtime_session.id,
            device_id=device_id,
            reason="unknown voice",
            confidence=result["score"],
            threshold=result["threshold"],
            audio_sha256=audio_sha256,
        )
        await record_runtime_event(
            session,
            kind="verify",
            payload={
                "accepted": False,
                "reason": "score_below_threshold",
                "score": result["score"],
                "threshold": result["threshold"],
            },
            device_id=runtime_session.device_id,
            session_id=runtime_session.id,
        )
        return {
            "verified": False,
            "confidence": result["score"],
            "reason": "Unknown voice — polite refusal. Only the owner can activate EVIE.",
        }

    await mark_verified(
        session,
        runtime_session,
        confidence=result["score"],
        verifier_name=runtime.verifier.name,
    )
    await _log_voice_attempt(
        session,
        kind="verify",
        outcome="accepted",
        session_id=runtime_session.id,
        device_id=device_id,
        reason="owner verified",
        confidence=result["score"],
        verifier=runtime.verifier.name,
        liveness_confidence=live_conf,
        audio_sha256=audio_sha256,
    )
    return {"verified": True, "confidence": result["score"], "reason": "owner_verified"}


async def handle_utterance(
    session: AsyncSession,
    runtime_session: RuntimeSession,
    *,
    text: str | None = None,
    audio_b64: str | None = None,
    audio_ref: str | None = None,
    language: str = "en",
    conversation_id=None,
    follow_up: bool = False,
    reverify_token: str | None = None,
    ctx=None,
) -> dict:
    """Process one utterance on the centralized runtime session.

    Runs the same voice pipeline as the voice-session lifecycle (ASR → chat/
    intelligence/memory → TTS) and drives the runtime state machine through
    processing → responding → follow_up. Owner-only activation and sensitive
    re-verification are enforced here, not in the client.
    """
    from app.voice.lifecycle import VoiceError, VoiceRuntime
    from app.voice.pipeline import run_chat_tts_pipeline, transcribe_input
    from app.voice.sensitive import REVERIFY_PURPOSE, classify_sensitive

    now = utcnow()
    if runtime_session.ended_at is not None:
        raise VoiceError(
            "Runtime voice session ended — wake EVIE again",
            status=428,
            code="session_ended",
        )
    if not runtime_session.owner_verified:
        raise VoiceError(
            "Owner speaker verification required before voice utterance",
            status=403,
            code="not_verified",
        )
    if follow_up:
        if runtime_session.state != "follow_up":
            raise VoiceError(
                f"Follow-up only valid from follow_up state (current: {runtime_session.state})",
                status=409,
                code="invalid_state",
            )
        updated_at = _aware(runtime_session.updated_at) or now
        if now - updated_at > timedelta(seconds=settings.runtime_followup_timeout_seconds):
            await transition(session, runtime_session, "idle", reason="follow_up_expired")
            raise VoiceError(
                "30-second follow-up window expired — wake EVIE again",
                status=428,
                code="follow_up_expired",
            )
    elif runtime_session.state != "awake":
        raise VoiceError(
            f"Utterance only valid from awake state (current: {runtime_session.state})",
            status=409,
            code="invalid_state",
        )

    runtime = VoiceRuntime(session, master_key=settings.master_key)
    transcript = await transcribe_input(
        runtime.transcriber,
        text=text,
        audio_b64=audio_b64,
        audio_ref=audio_ref,
        language=language,
    )
    sensitive_purpose = classify_sensitive(transcript.text)
    reverified = False
    if sensitive_purpose is not None:
        if not reverify_token or ctx is None:
            raise VoiceError(
                "Re-verification required for sensitive voice command "
                f"({sensitive_purpose}). Issue a proof via "
                "POST /v1/identity/reverification with purpose "
                f"{REVERIFY_PURPOSE!r}, then retry with the token.",
                status=403,
                code="reverification_required",
            )
        from app.identity.service import IdentityError, consume_reverification

        try:
            await consume_reverification(
                session,
                token=reverify_token,
                purpose=REVERIFY_PURPOSE,
                ctx=ctx,
            )
        except IdentityError as exc:
            raise VoiceError(exc.message, status=exc.status, code=exc.code) from exc
        reverified = True

    await transition(session, runtime_session, "processing", reason="utterance_start")
    outcome = await run_chat_tts_pipeline(
        session,
        actor="voice",
        device_id=str(runtime_session.device_id) if runtime_session.device_id else None,
        transcript=transcript,
        conversation_id=conversation_id,
        synthesizer=runtime.synthesizer,
    )
    await transition(session, runtime_session, "responding", reason="reply_ready")
    await transition(session, runtime_session, "follow_up", reason="reply_delivered")
    device_id = str(runtime_session.device_id) if runtime_session.device_id else None
    await _log_voice_attempt(
        session,
        kind="utterance" if not follow_up else "follow_up",
        outcome="accepted",
        session_id=runtime_session.id,
        device_id=device_id,
        transcript_chars=len(outcome.transcript.text),
        reply_chars=len(outcome.reply),
        asr_provider=outcome.transcript.provider,
        tts_provider=outcome.tts.provider,
        model=outcome.model,
        sensitive_purpose=sensitive_purpose,
        reverified=reverified,
    )
    await record_runtime_event(
        session,
        kind="utterance",
        payload={
            "follow_up": follow_up,
            "transcript_chars": len(outcome.transcript.text),
            "reply_chars": len(outcome.reply),
            "sensitive_purpose": sensitive_purpose,
            "reverified": reverified,
        },
        device_id=runtime_session.device_id,
        session_id=runtime_session.id,
    )
    return {
        "transcript": outcome.transcript,
        "reply": outcome.reply,
        "conversation_id": outcome.conversation_id,
        "tts": outcome.tts,
        "style": outcome.style,
        "model": outcome.model,
        "context_tokens": outcome.context_tokens,
        "memory_deltas": outcome.memory_deltas,
    }


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
        await record_runtime_event(
            session,
            kind="wake",
            payload={
                "blocked": True,
                "block_reason": "no_eligible_device",
                "candidate_count": len(intents),
            },
        )
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
        await record_runtime_event(
            session,
            kind="wake",
            payload={
                "blocked": True,
                "block_reason": "quiet_hours",
                "candidate_count": len(intents),
                "best_device_id": str(device.id),
            },
            device_id=device.id,
        )
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

    # Owner-only activation: issue a single-use challenge bound to this wake.
    from app.voice.anti_spoof import ReplayGuard

    guard = ReplayGuard(session)
    challenge = await guard.issue(
        purpose="verify",
        session_id=runtime_session.id,
        ttl_seconds=settings.runtime_verify_timeout_seconds,
    )
    runtime_session.challenge_nonce = challenge.nonce
    runtime_session.challenge_phrase = challenge.phrase
    runtime_session.wake_word = "evie"
    await session.flush()

    for candidate in candidates:
        candidate.selected = candidate.device_id == device.id and candidate.score == score
        if candidate.selected:
            candidate.reason = "winner"

    await record_runtime_event(
        session,
        kind="wake",
        payload={
            "winner_device_id": str(device.id),
            "winner_score": score,
            "candidate_count": len(intents),
            "blocked": False,
        },
        device_id=device.id,
        session_id=runtime_session.id,
    )
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
        challenge_nonce=runtime_session.challenge_nonce,
        challenge_phrase=runtime_session.challenge_phrase,
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
    await record_runtime_event(
        session,
        kind="heartbeat",
        payload={
            "status": data.status,
            "listener_state": data.listener_state,
            "battery_percent": data.battery_percent,
            "latency_ms": data.latency_ms,
        },
        device_id=device.id,
        session_id=current.id if current is not None else None,
    )
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
    spec = get_action_spec(data.action_type)
    if spec is None:
        raise ValueError(f"Unknown action type '{data.action_type}'")
    issues = validate_action_payload(data.action_type, data.payload)
    if issues:
        raise ValueError(f"Invalid action payload: {'; '.join(issues)}")
    requires_approval = force_requires_approval or bool(spec["requires_approval"])
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
    await record_runtime_event(
        session,
        kind="action",
        payload={
            "action_type": action.action_type,
            "status": action.status,
            "requires_approval": action.requires_approval,
        },
        device_id=action.device_id,
        session_id=action.session_id,
        action_id=action.id,
    )
    await log_access(
        session,
        actor=requested_by,
        action="action.route",
        endpoint="POST /v1/runtime/actions",
        resource_type="action",
        resource_ids=[action.id],
        details={
            "action_type": action.action_type,
            "requires_approval": action.requires_approval,
            "undoable": bool(spec["undoable"]),
            "permission": spec["permission"],
            "read_only": bool(spec["read_only"]),
        },
    )
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
    await record_runtime_event(
        session,
        kind="action",
        payload={"action_type": action.action_type, "decision": decision, "status": action.status},
        device_id=action.device_id,
        session_id=action.session_id,
        action_id=action.id,
    )
    await log_access(
        session,
        actor=actor,
        action="action.decide",
        endpoint=f"POST /v1/runtime/actions/{{id}}/{decision}",
        resource_type="action",
        resource_ids=[action.id],
        details={"decision": decision, "reason": reason},
    )
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
    await record_runtime_event(
        session,
        kind="action",
        payload={"action_type": action.action_type, "status": action.status},
        device_id=action.device_id,
        session_id=action.session_id,
        action_id=action.id,
    )
    await log_access(
        session,
        actor=actor,
        action="action.execute",
        endpoint="POST /v1/runtime/actions/{id}/execute",
        resource_type="action",
        resource_ids=[action.id],
        details={"action_type": action.action_type},
    )
    return action


async def rollback_action(
    session: AsyncSession,
    action_id: UUID,
    *,
    actor: str,
    reason: str | None = None,
) -> ApprovedAction:
    """Roll back an executed, undoable action through the declared registry."""
    action = await session.get(ApprovedAction, action_id)
    if action is None:
        raise KeyError(f"Action {action_id} not found")
    if action.status != "executed":
        raise ValueError(f"Only executed actions can be rolled back (status is {action.status!r})")
    spec = get_action_spec(action.action_type)
    if spec is None or not spec["undoable"]:
        raise ValueError(f"Action type '{action.action_type}' is not undoable")
    action.status = "rolled_back"
    action.rolled_back_at = utcnow()
    action.rolled_back_reason = reason or "rolled back by user"
    action.updated_at = utcnow()
    await session.flush()
    await record_runtime_event(
        session,
        kind="action",
        payload={"action_type": action.action_type, "status": action.status},
        device_id=action.device_id,
        session_id=action.session_id,
        action_id=action.id,
    )
    await log_access(
        session,
        actor=actor,
        action="action.rollback",
        endpoint="POST /v1/runtime/actions/{id}/rollback",
        resource_type="action",
        resource_ids=[action.id],
        details={
            "action_type": action.action_type,
            "reason": action.rolled_back_reason,
        },
    )
    return action


async def fail_action(session: AsyncSession, action_id: UUID, *, error: str) -> ApprovedAction:
    action = await session.get(ApprovedAction, action_id)
    if action is None:
        raise KeyError(f"Action {action_id} not found")
    action.status = "failed"
    action.error = error
    action.updated_at = utcnow()
    await session.flush()
    await record_runtime_event(
        session,
        kind="action",
        payload={"action_type": action.action_type, "status": action.status, "error": error},
        device_id=action.device_id,
        session_id=action.session_id,
        action_id=action.id,
    )
    await log_access(
        session,
        actor="system",
        action="action.fail",
        resource_type="action",
        resource_ids=[action.id],
        details={"action_type": action.action_type, "error": error},
    )
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
            await record_runtime_event(
                session,
                kind="dead_letter",
                payload={
                    "queue": queue,
                    "job_id": job_id,
                    "attempts": existing.attempts,
                    "status": existing.status,
                    "repeated": True,
                },
            )
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
    await record_runtime_event(
        session,
        kind="dead_letter",
        payload={"queue": queue, "job_id": job_id, "attempts": letter.attempts, "status": letter.status},
    )
    return letter


async def retry_dead_letter(session: AsyncSession, letter_id: UUID) -> DeadLetter:
    letter = await session.get(DeadLetter, letter_id)
    if letter is None:
        raise KeyError(f"Dead letter {letter_id} not found")
    if letter.status == "resolved":
        raise ValueError("Resolved dead letters cannot be retried")
    letter.status = "retrying"
    await session.flush()
    await record_runtime_event(
        session,
        kind="dead_letter",
        payload={"queue": letter.queue, "job_id": letter.job_id, "status": letter.status},
    )
    _re_enqueue_dead_letter(letter)
    return letter


def _re_enqueue_dead_letter(letter: DeadLetter) -> bool:
    """Best-effort re-enqueue of a retrying dead letter back onto its queue.

    Only applies in queue processing mode and only for letters whose payload
    carries an explicit entrypoint. Failures are non-fatal: the letter stays in
    ``retrying`` so the runtime daemon can try again on a later tick.
    """
    if settings.processing_mode != "queue":
        return False
    entrypoint = (letter.payload or {}).get("entrypoint")
    if not entrypoint:
        return False
    args = (letter.payload or {}).get("args") or []
    kwargs = (letter.payload or {}).get("kwargs") or {}
    try:
        from redis import Redis
        from rq import Queue

        queue = Queue(letter.queue, connection=Redis.from_url(settings.redis_url))
        queue.enqueue(entrypoint, *args, **kwargs)
        return True
    except Exception:  # noqa: BLE001 - best-effort recovery; daemon retries later
        return False


async def discard_dead_letter(session: AsyncSession, letter_id: UUID) -> DeadLetter:
    letter = await session.get(DeadLetter, letter_id)
    if letter is None:
        raise KeyError(f"Dead letter {letter_id} not found")
    letter.status = "discarded"
    await session.flush()
    await record_runtime_event(
        session,
        kind="dead_letter",
        payload={"queue": letter.queue, "job_id": letter.job_id, "status": letter.status},
    )
    return letter


async def resolve_dead_letter(session: AsyncSession, letter_id: UUID) -> DeadLetter:
    letter = await session.get(DeadLetter, letter_id)
    if letter is None:
        raise KeyError(f"Dead letter {letter_id} not found")
    letter.status = "resolved"
    letter.resolved_at = utcnow()
    await session.flush()
    await record_runtime_event(
        session,
        kind="dead_letter",
        payload={"queue": letter.queue, "job_id": letter.job_id, "status": letter.status},
    )
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


async def _asr_tts_checks() -> list[dict]:
    """ASR/TTS provider health: local providers get a functional probe,
    network providers are checked for configuration (no probe traffic)."""
    from app.voice.asr import get_transcriber
    from app.voice.contracts import SpeechStyle
    from app.voice.tts import get_synthesizer

    checks: list[dict] = []
    try:
        transcriber = get_transcriber()
        if transcriber.name == "echo":
            await transcriber.transcribe(text_hint="ev health probe")
            asr_status = "ok"
            asr_detail: dict = {"probe": "echo"}
        elif settings.voice_asr_base_url:
            asr_status = "ok"
            asr_detail = {}
        else:
            asr_status = "degraded"
            asr_detail = {"reason": "base_url not configured"}
    except Exception as exc:  # noqa: BLE001 - health boundary
        asr_status = "degraded"
        asr_detail = {"error": f"{type(exc).__name__}: {exc}"}
    checks.append(
        {
            "name": "asr",
            "status": asr_status,
            "provider": settings.voice_asr_provider,
            "model": settings.voice_asr_model,
            **asr_detail,
        }
    )

    try:
        synthesizer = get_synthesizer()
        if synthesizer.name == "meta":
            await synthesizer.synthesize("ev health probe", style=SpeechStyle())
            tts_status = "ok"
            tts_detail: dict = {"probe": "meta"}
        elif settings.voice_tts_base_url:
            tts_status = "ok"
            tts_detail = {}
        else:
            tts_status = "degraded"
            tts_detail = {"reason": "base_url not configured"}
    except Exception as exc:  # noqa: BLE001 - health boundary
        tts_status = "degraded"
        tts_detail = {"error": f"{type(exc).__name__}: {exc}"}
    checks.append(
        {
            "name": "tts",
            "status": tts_status,
            "provider": settings.voice_tts_provider,
            "model": settings.voice_tts_model,
            **tts_detail,
        }
    )
    return checks


async def runtime_health(session: AsyncSession) -> dict:
    """Structured runtime health: DB, state machine, listeners, queue, DLQ."""
    checks: list[dict] = []
    try:
        await session.execute(select(1))
        checks.append({"name": "database", "status": "ok"})
    except Exception as exc:  # noqa: BLE001 - health boundary
        checks.append(
            {
                "name": "database",
                "status": "failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        )

    current = await active_session(session)
    state = current.state if current else "idle"
    checks.append({"name": "state_machine", "status": "ok", "state": state})

    now = utcnow()
    grace = timedelta(seconds=settings.runtime_heartbeat_grace_seconds)
    device_rows = list(
        (
            await session.execute(select(Device).where(Device.revoked_at.is_(None)))
        ).scalars().all()
    )
    online = 0
    listening = 0
    for device in device_rows:
        last_seen = _aware(device.last_seen_at)
        if last_seen is None or now - last_seen > grace:
            continue
        online += 1
        heartbeat = (
            await session.execute(
                select(RuntimeHeartbeat)
                .where(RuntimeHeartbeat.device_id == device.id)
                .order_by(RuntimeHeartbeat.reported_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if heartbeat is not None and heartbeat.listener_state == "listening":
            listening += 1
    listener_status = "ok" if (not device_rows or online > 0) else "degraded"
    checks.append(
        {
            "name": "listeners",
            "status": listener_status,
            "registered_devices": len(device_rows),
            "online_devices": online,
            "listening_devices": listening,
        }
    )

    dlq = await dead_letter_summary(session)
    dlq_status = "degraded" if dlq["discarded"] > 0 or dlq["new"] > 0 else "ok"
    checks.append({"name": "dead_letters", "status": dlq_status, "counts": dlq})

    if settings.processing_mode == "queue":
        try:
            from redis import Redis

            Redis.from_url(settings.redis_url).ping()
            queue_status = "ok"
        except Exception:  # noqa: BLE001 - health boundary
            queue_status = "degraded"
    else:
        queue_status = "ok"
    checks.append(
        {"name": "queue", "status": queue_status, "mode": settings.processing_mode}
    )
    checks.append(
        {
            "name": "chat_provider",
            "status": "ok",
            "provider": settings.chat_provider,
        }
    )
    checks.extend(await _asr_tts_checks())

    statuses = [check["status"] for check in checks]
    if "failed" in statuses:
        overall = "failed"
    elif any(status != "ok" for status in statuses):
        overall = "degraded"
    else:
        overall = "ok"
    return {
        "schema_version": "ev.runtime.health.v1",
        "generated_at": now.isoformat(),
        "overall": overall,
        "state": state,
        "quiet_hours_active": quiet_hours_active(now),
        "attention": await attention_usage(session),
        "dead_letters": dlq,
        "checks": checks,
    }


async def daemon_tick(session: AsyncSession) -> dict:
    """One 24/7 runtime daemon tick: expire stale sessions, retry DLQs, report health."""
    before = await active_session(session)
    await expire_stale(session)
    after = await active_session(session)
    expired_session_id = before.id if (before is not None and after is None) else None

    retrying_rows = list(
        (
            await session.execute(
                select(DeadLetter).where(DeadLetter.status == "retrying")
            )
        ).scalars().all()
    )
    re_enqueued = sum(1 for letter in retrying_rows if _re_enqueue_dead_letter(letter))
    health = await runtime_health(session)
    digest = await maybe_build_digest(session)
    await record_runtime_event(
        session,
        kind="daemon",
        payload={
            "expired_session_id": str(expired_session_id) if expired_session_id else None,
            "re_enqueued": re_enqueued,
            "digest_delivered": bool(digest),
            "overall": health["overall"],
        },
    )

    return {
        "expired_session_id": str(expired_session_id) if expired_session_id else None,
        "re_enqueued": re_enqueued,
        "digest": digest,
        "health": health,
    }


async def maybe_build_digest(session: AsyncSession) -> dict | None:
    """Build the quiet-hours alert digest once per day, if it is due.

    Runs only during quiet hours and only when no digest has already been
    delivered today (deduped through the append-only runtime event log), so the
    daemon never spams or double-delivers.
    """
    from app.ev import alert_radar

    if not quiet_hours_active():
        return None
    start_of_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    delivered_today = int(
        (
            await session.execute(
                select(func.count(RuntimeEvent.id)).where(
                    RuntimeEvent.kind == "digest",
                    RuntimeEvent.occurred_at >= start_of_day,
                )
            )
        ).scalar_one()
    )
    if delivered_today > 0:
        return None
    result = await alert_radar.build_digest(session)
    await record_runtime_event(
        session,
        kind="digest",
        payload={
            "digest_id": result["digest_id"],
            "delivered": result["delivered"],
            "source": "runtime_daemon",
        },
    )
    return result


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
        session=RuntimeSessionOut.model_validate(latest) if latest else None,
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


def resolve_dead_letter_sync(*, queue: str, job_id: str) -> None:
    """Sync helper marking a dead letter resolved after a successful retry."""
    import asyncio

    from app.db import SessionLocal

    async def _go() -> None:
        async with SessionLocal() as db_session:
            rows = (
                await db_session.execute(
                    select(DeadLetter).where(
                        DeadLetter.queue == queue,
                        DeadLetter.job_id == job_id,
                        DeadLetter.status.in_(["new", "retrying"]),
                    )
                )
            ).scalars().all()
            for row in rows:
                await resolve_dead_letter(db_session, row.id)
            await db_session.commit()

    asyncio.run(_go())
