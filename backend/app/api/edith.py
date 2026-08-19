"""E.D.I.T.H. + continuous conversation + live data API."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    ActorContext,
    require_actor,
    require_actor_context,
    require_reverification,
)
from app.db import get_session
from app.ev import conversation, edith, live, vision
from app.ev.rollup import build_rollup
from app.models import LiveChannel, LiveEvent
from app.schemas import (
    CommandOut,
    ConfirmRecognitionRequest,
    ConversationDetail,
    ConversationMessageOut,
    ConversationOut,
    ConversationResetRequest,
    ConversationRollupOut,
    ConversationStateOut,
    FleetStatusOut,
    FleetTaskCompleteRequest,
    FleetTaskCreate,
    FleetTaskFailRequest,
    FleetTaskOut,
    FocusDesignationCreate,
    FocusDesignationOut,
    FocusSuggestResponse,
    HudFocusOut,
    LiveChannelCreate,
    LiveChannelOut,
    LiveEventBatchRequest,
    LiveEventCreate,
    LiveEventOut,
    LiveRebuildOut,
    LiveRetentionOut,
    LiveStatusOut,
    OpsCenterOut,
    RecognitionCreate,
    RecognitionOut,
    TwinOut,
    VisionAnalyzeRequest,
    VisionPerceptionOut,
)
from app.services.access_log import log_access
from app.utils.text import utcnow

router = APIRouter(prefix="/v1")


# --------------------------------------------------------------------------- #
# Single continuous conversation
# --------------------------------------------------------------------------- #


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[ConversationOut]:
    rows = await conversation.list_threads(session)
    return [ConversationOut.model_validate(row) for row in rows]


@router.get("/conversation", response_model=ConversationDetail)
async def get_conversation(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ConversationDetail:
    thread = await conversation.get_default_thread(session)
    messages = await conversation.history(session, thread.id, limit=limit)
    state = await conversation.get_or_create_state(session, thread.id)
    rollup = await build_rollup(session, thread.id)
    next_actions = []
    if state.focus:
        next_actions.append(f"Continue focusing on: {state.focus}")
    if state.pending_questions:
        next_actions.append(f"Answer pending question: {state.pending_questions[0]}")
    if not next_actions:
        next_actions.append("Say 'continue' to pick up where you left off.")
    await session.commit()
    return ConversationDetail(
        conversation=ConversationOut.model_validate(thread),
        messages=[
            ConversationMessageOut(
                id=event.id,
                role="assistant" if event.event_type == "message.assistant" else "user",
                text=(event.content or {}).get("text") or "",
                occurred_at=event.occurred_at,
            )
            for event in messages
        ],
        state=ConversationStateOut(
            focus=state.focus,
            recent_topics=state.recent_topics or [],
            pending_questions=state.pending_questions or [],
            working_context=state.working_context or {},
            updated_at=state.updated_at,
        ),
        rollup=ConversationRollupOut(
            summary=rollup.summary,
            covered_turn_count=rollup.covered_turn_count,
            token_count=rollup.token_count,
            updated_at=rollup.updated_at,
        ),
        next_actions=next_actions,
    )


@router.post("/conversation/reset", response_model=ConversationStateOut)
async def reset_conversation(
    data: ConversationResetRequest | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ConversationStateOut:
    thread = await conversation.get_default_thread(session)
    state = await conversation.reset_state(
        session,
        thread.id,
        reason=data.reason if data else "start fresh",
        actor=actor,
    )
    await session.commit()
    return ConversationStateOut(
        focus=state.focus,
        recent_topics=state.recent_topics or [],
        pending_questions=state.pending_questions or [],
        working_context=state.working_context or {},
        updated_at=state.updated_at,
    )


# --------------------------------------------------------------------------- #
# Live data recording
# --------------------------------------------------------------------------- #


@router.post("/live/channels", response_model=LiveChannelOut, status_code=201)
async def create_live_channel(
    data: LiveChannelCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> LiveChannelOut:
    channel = await live.create_channel(session, data)
    await session.commit()
    return LiveChannelOut.model_validate(channel)


@router.get("/live/channels", response_model=list[LiveChannelOut])
async def list_live_channels(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[LiveChannelOut]:
    rows = await live.list_channels(session)
    return [LiveChannelOut.model_validate(row) for row in rows]


@router.post("/live/channels/{channel_id}/events", response_model=list[LiveEventOut], status_code=201)
async def ingest_channel_events(
    channel_id: UUID,
    events: list[LiveEventCreate],
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[LiveEventOut]:
    channel = await session.get(LiveChannel, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Live channel not found")
    rows = await live.ingest_events(session, channel, events)
    from app.routines.service import consider_event

    for row in rows:
        await consider_event(session, live_event=row, channel=channel)
    await session.commit()
    return [LiveEventOut.model_validate(row) for row in rows]


@router.get("/live/channels/{channel_id}/events", response_model=list[LiveEventOut])
async def list_live_events(
    channel_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    since: datetime | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[LiveEventOut]:
    rows = await live.list_events(session, channel_id, limit=limit, since=since)
    return [LiveEventOut.model_validate(row) for row in rows]


@router.post("/live/events", response_model=list[LiveEventOut], status_code=201)
async def ingest_live_batch(
    data: LiveEventBatchRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[LiveEventOut]:
    """Batch ingestion from a user-managed collector (screen/audio/health/app)."""
    channel = await live.get_or_create_channel(
        session,
        name=data.channel,
        kind=data.kind,
        privacy_level=data.privacy_level,
    )
    rows = await live.ingest_events(session, channel, data.events)
    from app.routines.service import consider_event

    for row in rows:
        await consider_event(session, live_event=row, channel=channel)
    await session.commit()
    return [LiveEventOut.model_validate(row) for row in rows]


@router.get("/live/status", response_model=LiveStatusOut)
async def live_status(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> LiveStatusOut:
    return await live.status(session)


@router.get("/live/stream")
async def live_event_stream(
    access: str = Query(default="user", pattern="^(user|model)$"),
    since: datetime | None = Query(default=None),
    poll_interval: float = Query(default=1.0, ge=0.1, le=30.0),
    timeout_seconds: float | None = Query(default=None, ge=0.1, le=3600.0),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> StreamingResponse:
    """SSE tail of newly ingested live events; ``since=`` replays first."""
    from app.services.live_stream import stream_live_events

    async def event_source():
        try:
            async for item in stream_live_events(
                session,
                access=access,
                since=since,
                poll_interval=poll_interval,
                timeout_seconds=timeout_seconds,
            ):
                yield f"event: live\ndata: {json.dumps(item, default=str)}\n\n"
            yield "event: done\ndata: {}\n\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/live/rebuild", response_model=LiveRebuildOut)
async def rebuild_live_derived(
    reason: str = Query(default="manual rebuild", max_length=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> LiveRebuildOut:
    """Deterministically rebuild per-channel live derived state from the stream."""
    from app.services.live_rebuild import rebuild_live_derived_state

    result = await rebuild_live_derived_state(session, actor=actor, reason=reason)
    await session.commit()
    return LiveRebuildOut(**result)


@router.post("/live/retention", response_model=LiveRetentionOut)
async def apply_live_retention_policy(
    days: int | None = Query(default=None, ge=1, le=3650),
    dry_run: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> LiveRetentionOut:
    """Apply the live-event retention window (dry-run by default)."""
    from app.services.live_retention import apply_live_retention

    result = await apply_live_retention(
        session,
        days=days,
        dry_run=dry_run,
        actor=actor,
    )
    await session.commit()
    return LiveRetentionOut(**result)


# --------------------------------------------------------------------------- #
# E.D.I.T.H.-inspired modules
# --------------------------------------------------------------------------- #


@router.get("/focus/suggest", response_model=FocusSuggestResponse)
async def focus_suggest(
    limit: int = Query(default=5, ge=1, le=10),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> FocusSuggestResponse:
    """E.D.I.T.H.-style lock-on suggestions, pointed at goals/tasks — never people to harm."""
    return await edith.suggest_focus(session, limit=limit)


@router.post("/focus", response_model=FocusDesignationOut, status_code=201)
async def designate_focus(
    data: FocusDesignationCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> FocusDesignationOut:
    focus = await edith.designate_focus(session, data, actor=actor)
    await session.commit()
    return FocusDesignationOut.model_validate(focus)


@router.get("/focus", response_model=FocusDesignationOut | None)
async def get_focus(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> FocusDesignationOut | None:
    focus = await edith.active_focus(session)
    return FocusDesignationOut.model_validate(focus) if focus else None


@router.post("/focus/{focus_id}/end", response_model=FocusDesignationOut)
async def end_focus(
    focus_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> FocusDesignationOut:
    try:
        focus = await edith.end_focus(session, focus_id, actor=actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="Focus not found") from None
    await session.commit()
    return FocusDesignationOut.model_validate(focus)


@router.get("/fleet", response_model=FleetStatusOut)
async def fleet_status(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> FleetStatusOut:
    return await edith.fleet_status(session)


@router.post("/fleet/tasks", response_model=FleetTaskOut, status_code=201)
async def create_fleet_task(
    data: FleetTaskCreate,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> FleetTaskOut:
    try:
        task = await edith.create_fleet_task(
            session,
            data,
            actor=ctx.actor,
            device_id=ctx.device_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Device not found") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="A device can only dispatch tasks to itself") from None
    except ValueError as exc:
        await session.commit()  # persist the rejected-command ledger entry before surfacing the error
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await session.commit()
    return FleetTaskOut.model_validate(task)


@router.get("/fleet/tasks", response_model=list[FleetTaskOut])
async def list_fleet_tasks(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[FleetTaskOut]:
    rows = await edith.list_fleet_tasks(session, limit=limit)
    return [FleetTaskOut.model_validate(row) for row in rows]


@router.get("/fleet/tasks/pending", response_model=list[FleetTaskOut])
async def list_pending_fleet_tasks(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> list[FleetTaskOut]:
    """Device-facing queue: a device only sees its own pending tasks; master sees all."""
    rows = await edith.list_pending_fleet_tasks(
        session,
        actor=ctx.actor,
        device_id=ctx.device_id,
        limit=limit,
    )
    await log_access(
        session,
        actor=ctx.actor,
        action="read",
        endpoint="GET /v1/fleet/tasks/pending",
        resource_type="fleet_task",
        resource_ids=[r.id for r in rows],
        details={"count": len(rows)},
    )
    await session.commit()
    return [FleetTaskOut.model_validate(row) for row in rows]


@router.get("/fleet/tasks/{task_id}", response_model=FleetTaskOut)
async def get_fleet_task(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> FleetTaskOut:
    try:
        task = await edith.get_fleet_task(
            session,
            task_id,
            actor=ctx.actor,
            device_id=ctx.device_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Fleet task not found") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Fleet task is not visible to this actor") from None
    await log_access(
        session,
        actor=ctx.actor,
        action="read",
        endpoint="GET /v1/fleet/tasks/{id}",
        resource_type="fleet_task",
        resource_ids=[task.id],
    )
    await session.commit()
    return FleetTaskOut.model_validate(task)


@router.post("/fleet/tasks/{task_id}/accept", response_model=FleetTaskOut)
async def accept_fleet_task(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> FleetTaskOut:
    try:
        task = await edith.accept_fleet_task(
            session,
            task_id,
            actor=ctx.actor,
            device_id=ctx.device_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Fleet task not found") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Fleet task is not visible to this actor") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return FleetTaskOut.model_validate(task)


@router.post("/fleet/tasks/{task_id}/start", response_model=FleetTaskOut)
async def start_fleet_task(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> FleetTaskOut:
    try:
        task = await edith.start_fleet_task(
            session,
            task_id,
            actor=ctx.actor,
            device_id=ctx.device_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Fleet task not found") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Fleet task is not visible to this actor") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return FleetTaskOut.model_validate(task)


@router.post("/fleet/tasks/{task_id}/complete", response_model=FleetTaskOut)
async def complete_fleet_task(
    task_id: UUID,
    data: FleetTaskCompleteRequest | None = None,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> FleetTaskOut:
    try:
        task = await edith.complete_fleet_task(
            session,
            task_id,
            actor=ctx.actor,
            device_id=ctx.device_id,
            result=data.result if data else {},
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Fleet task not found") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Fleet task is not visible to this actor") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return FleetTaskOut.model_validate(task)


@router.post("/fleet/tasks/{task_id}/fail", response_model=FleetTaskOut)
async def fail_fleet_task(
    task_id: UUID,
    data: FleetTaskFailRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> FleetTaskOut:
    try:
        task = await edith.fail_fleet_task(
            session,
            task_id,
            actor=ctx.actor,
            device_id=ctx.device_id,
            error=data.error,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Fleet task not found") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Fleet task is not visible to this actor") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return FleetTaskOut.model_validate(task)


@router.post("/fleet/tasks/{task_id}/cancel", response_model=FleetTaskOut)
async def cancel_fleet_task(
    task_id: UUID,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> FleetTaskOut:
    try:
        task = await edith.cancel_fleet_task(
            session,
            task_id,
            actor=ctx.actor,
            device_id=ctx.device_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Fleet task not found") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Fleet task is not visible to this actor") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return FleetTaskOut.model_validate(task)


@router.post("/vision/annotate", response_model=RecognitionOut, status_code=201)
async def annotate_recognition(
    data: RecognitionCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RecognitionOut:
    row = await edith.annotate(session, data, actor=actor)
    await session.commit()
    return RecognitionOut.model_validate(row)


@router.get("/vision/log", response_model=list[RecognitionOut])
async def recognition_log(
    limit: int = Query(default=50, ge=1, le=200),
    source: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[RecognitionOut]:
    rows = await edith.list_recognition(session, limit=limit, source=source)
    await log_access(
        session,
        actor=actor,
        action="read",
        endpoint="GET /v1/vision/log",
        resource_type="recognition",
        resource_ids=[r.id for r in rows],
        details={"count": len(rows), "source": source},
    )
    await session.commit()
    return [RecognitionOut.model_validate(row) for row in rows]


def _perception_out(row: LiveEvent) -> VisionPerceptionOut:
    payload = row.payload or {}
    return VisionPerceptionOut(
        id=row.id,
        attachment_id=UUID(payload["attachment_id"]) if payload.get("attachment_id") else None,
        source_event_id=UUID(payload["source_event_id"]) if payload.get("source_event_id") else None,
        summary=payload.get("summary") or "",
        labels=payload.get("labels") or [],
        confidence=payload.get("confidence") or 0.0,
        provider=payload.get("provider") or "",
        raw_sent=bool(payload.get("raw_sent")),
        permission_granted_by=payload.get("permission_granted_by"),
        content_type=payload.get("content_type"),
        size_bytes=payload.get("size_bytes"),
        ocr_text=payload.get("ocr_text"),
        ocr_provider=payload.get("ocr_provider"),
        derived_text_used=bool(payload.get("derived_text_used")),
        request_id=payload.get("request_id"),
        created_at=row.occurred_at,
    )


@router.post("/vision/analyze", response_model=VisionPerceptionOut, status_code=201)
async def analyze_vision(
    data: VisionAnalyzeRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_reverification("camera.analyze")),
) -> VisionPerceptionOut:
    if not data.permission:
        raise HTTPException(
            status_code=403,
            detail="Explicit permission is required before any perception analysis",
        )
    from app.ev.policy import Confirmation, authorize
    from app.ev.tools import get_spec

    now = utcnow()
    confirmation = Confirmation(
        factor="master_key" if ctx.is_master else "reverify",
        confirmed=True,
        target=str(data.attachment_id),
        issued_at=now,
    )
    decision = await authorize(
        session,
        "camera_replay",
        actor=ctx.actor,
        arguments={"camera": str(data.attachment_id)},
        device_id=ctx.device_id,
        channel="action",
        confirmation=confirmation,
        spec=get_spec("camera_replay"),
        provider_connected_override=True,
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=decision.reason)
    try:
        row = await vision.analyze_attachment(
            session,
            data.attachment_id,
            actor=ctx.actor,
            permission=data.permission,
            allow_raw=data.allow_raw,
            prompt=data.prompt,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Attachment not found") from None
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    await session.commit()
    return _perception_out(row)


@router.post("/vision/recognitions/{recognition_id}/confirm", response_model=RecognitionOut)
async def confirm_vision_recognition(
    recognition_id: UUID,
    data: ConfirmRecognitionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RecognitionOut:
    try:
        row = await vision.confirm_recognition(
            session,
            recognition_id,
            actor=actor,
            entity_type=data.entity_type if data else "thing",
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Recognition not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return RecognitionOut.model_validate(row)


@router.get("/vision/perceptions", response_model=list[VisionPerceptionOut])
async def list_vision_perceptions(
    limit: int = Query(default=50, ge=1, le=200),
    attachment_id: UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[VisionPerceptionOut]:
    rows = await vision.list_perceptions(session, limit=limit, attachment_id=attachment_id)
    await log_access(
        session,
        actor=actor,
        action="read",
        endpoint="GET /v1/vision/perceptions",
        resource_type="perception",
        resource_ids=[row.id for row in rows],
        details={"count": len(rows), "attachment_id": str(attachment_id) if attachment_id else None},
    )
    await session.commit()
    return [_perception_out(row) for row in rows]


@router.get("/vision/perceptions/{perception_id}", response_model=VisionPerceptionOut)
async def get_vision_perception(
    perception_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> VisionPerceptionOut:
    try:
        row = await vision.get_perception(session, perception_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Perception not found") from None
    await log_access(
        session,
        actor=actor,
        action="read",
        endpoint="GET /v1/vision/perceptions/{id}",
        resource_type="perception",
        resource_ids=[row.id],
    )
    await session.commit()
    return _perception_out(row)


@router.get("/commands", response_model=list[CommandOut])
async def list_commands(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> list[CommandOut]:
    """Auditable command surface: master sees all commands; devices see their own."""
    rows = await edith.list_commands(
        session,
        actor=ctx.actor,
        device_id=str(ctx.device_id) if ctx.device_id else None,
        limit=limit,
    )
    await log_access(
        session,
        actor=ctx.actor,
        action="read",
        endpoint="GET /v1/commands",
        resource_type="command",
        resource_ids=[r.id for r in rows],
        details={"count": len(rows)},
    )
    await session.commit()
    return [CommandOut.model_validate(row) for row in rows]


@router.get("/commands/{command_id}", response_model=CommandOut)
async def get_command(
    command_id: UUID,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> CommandOut:
    try:
        command = await edith.get_command(session, command_id, actor=ctx.actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="Command not found") from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="Command is not visible to this actor") from None
    await log_access(
        session,
        actor=ctx.actor,
        action="read",
        endpoint="GET /v1/commands/{id}",
        resource_type="command",
        resource_ids=[command.id],
    )
    await session.commit()
    return CommandOut.model_validate(command)


@router.get("/ops/center", response_model=OpsCenterOut)
async def ops_center(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> OpsCenterOut:
    return await edith.ops_center(session)


@router.get("/twin", response_model=TwinOut)
async def digital_twin(
    as_of: datetime | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> TwinOut:
    return await edith.twin(session, as_of=as_of)


@router.get("/hud/focus", response_model=HudFocusOut)
async def hud_focus(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> HudFocusOut:
    return await edith.hud_focus(session)
