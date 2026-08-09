"""Core v1 API: events, chat, memory, audit, export, devices, gateway."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    ActorContext,
    require_actor,
    require_actor_context,
    require_master,
    require_reverification,
)
from app.config import settings
from app.contracts import ChatMessage, ChatResult, MemoryRef, RequestEnvelope
from app.db import get_session
from app.ev import alert_radar, conversation
from app.ev import rollup as rollup_service
from app.ev.calibration import proactive_tuning
from app.ev.interaction import build_strategy, strategy_block
from app.ev.personality import get_current, identity_block, to_dict
from app.ev.self_eval import log_response
from app.ev.user_state import build_user_state
from app.filter.envelope import (
    OutputReport,
    SpeakerIdentity,
    compute_envelope_hash,
)
from app.filter.input_filter import InputFilter
from app.filter.ledger import record_decision
from app.filter.output_filter import run_output_filter
from app.gateway.providers import get_chat_provider
from app.gateway.service import ModelGateway, tool_specs_from_dicts
from app.memory.patterns import PatternEngine
from app.memory.retrieval import Retriever
from app.models import (
    AccessLog,
    Attachment,
    Conflict,
    Device,
    Entity,
    EntityRelationship,
    Event,
    Memory,
    MemoryEntity,
    MemoryEvent,
    OwnerIdentity,
)
from app.schemas import (
    AccessLogOut,
    AttachmentCreateResponse,
    AttachmentOut,
    AuditOut,
    ChatRequest,
    ChatResponse,
    ConflictOut,
    ConsolidationOut,
    DeviceCreate,
    DeviceCreateResponse,
    DeviceOut,
    EntityRefOut,
    EventCreate,
    EventCreateResponse,
    EventOut,
    EventRef,
    ExportBundle,
    FilterReportOut,
    GatewayChatRequest,
    GatewayChatResponse,
    GatewayToolCall,
    ImportResponse,
    MemoryChangeGroup,
    MemoryChangesResponse,
    MemoryDelta,
    MemoryListResponse,
    MemoryOut,
    ModelCallOut,
    PrivacyLevel,
    ProvenanceItem,
    RebuildOut,
    TimelineResponse,
    UserStateOut,
    WeekRecallOut,
)
from app.services.access_log import log_access
from app.services.consolidation import next_period_start, run_consolidation
from app.services.event_service import EventService
from app.services.importer import import_bundle
from app.services.model_call import list_model_calls, log_model_call
from app.services.processor import ensure_processed
from app.services.rebuild import rebuild_derived_state
from app.storage.object_store import get_object_store, sha256_bytes
from app.utils.text import sha256_hex, utcnow

router = APIRouter(prefix="/v1")


def _event_ref(event: Event) -> EventRef:
    return EventRef(
        id=event.id,
        occurred_at=event.occurred_at,
        source=event.source,
        event_type=event.event_type,
        text=(event.content or {}).get("text"),
    )


async def _memory_out(session: AsyncSession, memory: Memory) -> MemoryOut:
    source_rows = (
        await session.execute(
            select(Event)
            .join(MemoryEvent, MemoryEvent.event_id == Event.id)
            .where(MemoryEvent.memory_id == memory.id)
            .order_by(Event.occurred_at.desc())
        )
    ).scalars().all()
    entity_rows = (
        await session.execute(
            select(Entity, MemoryEntity.role, MemoryEntity.weight)
            .join(MemoryEntity, MemoryEntity.entity_id == Entity.id)
            .where(MemoryEntity.memory_id == memory.id)
        )
    ).all()
    return MemoryOut(
        id=memory.id,
        memory_type=memory.memory_type,
        text=memory.text,
        payload=memory.payload,
        importance=memory.importance,
        confidence=memory.confidence,
        source_type=memory.source_type,
        privacy_level=memory.privacy_level,
        event_time=memory.event_time,
        created_time=memory.created_time,
        updated_time=memory.updated_time,
        valid_from=memory.valid_from,
        valid_until=memory.valid_until,
        version_group=memory.version_group,
        version=memory.version,
        supersedes_id=memory.supersedes_id,
        superseded_by_id=memory.superseded_by_id,
        reason_for_change=memory.reason_for_change,
        is_current=memory.is_current,
        redacted=memory.redacted,
        source_events=[_event_ref(e) for e in source_rows],
        entities=[
            EntityRefOut(id=entity.id, name=entity.name, entity_type=entity.entity_type, role=role, weight=weight)
            for entity, role, weight in entity_rows
        ],
    )


@router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "environment": settings.environment,
        "version": "0.1.0",
        "capabilities": [
            "events",
            "chat",
            "memory",
            "audit",
            "export",
            "devices",
            "diagnostics",
            "tactical",
            "ev_sense",
            "health_radar",
            "alert_radar",
            "person_finder",
            "interaction_modes",
            "decision_intelligence",
            "pattern_engine",
            "gear_telemetry",
            "fleet_lifecycle",
            "command_ledger",
            "routines_automations",
            "integrations",
            "plugins",
        ],
        "providers": {
            "chat": settings.chat_provider,
            "embeddings": settings.embedding_provider,
            "storage": settings.object_store_backend,
        },
    }


@router.post("/events", response_model=EventCreateResponse, status_code=201)
async def create_event(
    data: EventCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> EventCreateResponse | JSONResponse:
    request_id = request.headers.get("X-Request-Id") or str(uuid4())
    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key:
        existing = (
            await session.execute(
                select(Event).where(Event.idempotency_key_hash == sha256_hex(idempotency_key))
            )
        ).scalar_one_or_none()
        if existing is not None:
            await log_access(
                session,
                actor=actor,
                action="duplicate",
                endpoint="POST /v1/events",
                resource_type="event",
                resource_ids=[existing.id],
                request_id=request_id,
                details={"idempotency_key_sha256": sha256_hex(idempotency_key)},
            )
            await session.commit()
            return JSONResponse(
                status_code=409,
                content=EventCreateResponse(
                    event=EventOut.model_validate(existing),
                    memory_delta=[],
                ).model_dump(mode="json"),
            )
    service = EventService(session, actor=actor)
    event = await service.create(
        data,
        request_id=request_id,
        idempotency_key=idempotency_key,
    )
    from app.routines.service import consider_event

    await consider_event(session, event=event)
    await session.commit()
    deltas = await ensure_processed(event.id)
    return EventCreateResponse(
        event=EventOut.model_validate(event),
        memory_delta=[MemoryDelta.model_validate(d) for d in deltas],
    )


@router.get("/events/{event_id}", response_model=EventOut)
async def get_event(
    event_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> EventOut:
    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    await log_access(
        session,
        actor=actor,
        action="read",
        endpoint="GET /v1/events/{id}",
        resource_type="event",
        resource_ids=[event.id],
    )
    await session.commit()
    return EventOut.model_validate(event)


@router.delete("/events/{event_id}", response_model=EventOut)
async def tombstone_event(
    event_id: UUID,
    reason: str = Query(default="user-requested", max_length=200),
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_reverification("memory.delete")),
) -> EventOut:
    from app.memory.writer import redact_memories_for_event

    actor = ctx.actor
    service = EventService(session, actor=actor)
    try:
        event = await service.tombstone(event_id, reason)
    except KeyError:
        raise HTTPException(status_code=404, detail="Event not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    redacted = await redact_memories_for_event(session, event.id)
    if event.conversation_id is not None:
        await rollup_service.rebuild_rollup(session, event.conversation_id)
    if redacted:
        await log_access(
            session,
            actor=actor,
            action="redact",
            endpoint="DELETE /v1/events/{id}",
            resource_type="memory",
            resource_ids=[],
            details={"redacted_memories": redacted},
        )
    await session.commit()
    return EventOut.model_validate(event)


@router.get("/timeline", response_model=TimelineResponse)
async def timeline(
    limit: int = Query(default=50, ge=1, le=500),
    cursor: datetime | None = None,
    source: str | None = None,
    event_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    include_tombstoned: bool = False,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> TimelineResponse:
    service = EventService(session, actor=actor)
    events = await service.timeline(
        limit=limit,
        cursor=cursor,
        source=source,
        event_type=event_type,
        since=since,
        until=until,
        include_tombstoned=include_tombstoned,
    )
    next_cursor = events[-1].occurred_at if len(events) == limit else None
    await log_access(
        session,
        actor=actor,
        action="read",
        endpoint="GET /v1/timeline",
        resource_type="event",
        resource_ids=[e.id for e in events[:50]],
        details={"count": len(events)},
    )
    await session.commit()
    return TimelineResponse(events=[EventOut.model_validate(e) for e in events], next_cursor=next_cursor)


@router.get("/recall/week", response_model=WeekRecallOut)
async def recall_week(
    week_start: datetime = Query(...),
    limit_events: int = Query(default=500, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> WeekRecallOut:
    """Reconstruct a past week: events, end-of-week memory state, consolidation."""
    from app.services.recall import reconstruct_week

    if week_start.tzinfo is None:
        week_start = week_start.replace(tzinfo=UTC)
    result = await reconstruct_week(session, week_start=week_start, limit_events=limit_events)
    await log_access(
        session,
        actor=actor,
        action="read",
        endpoint="GET /v1/recall/week",
        resource_type="event",
        resource_ids=[e.id for e in result.events[:50]],
        details={
            "week_start": week_start.isoformat(),
            "events": result.event_count,
            "memories": result.memory_count,
        },
    )
    await session.commit()
    return result


@router.get("/memories", response_model=MemoryListResponse)
async def list_memories(
    memory_type: str | None = None,
    is_current: bool | None = None,
    q: str | None = None,
    as_of: datetime | None = None,
    source_type: str | None = None,
    privacy_level: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> MemoryListResponse:
    stmt = select(Memory)
    if memory_type:
        stmt = stmt.where(Memory.memory_type == memory_type)
    if is_current is not None:
        stmt = stmt.where(Memory.is_current.is_(is_current))
    if source_type:
        stmt = stmt.where(Memory.source_type == source_type)
    if privacy_level:
        stmt = stmt.where(Memory.privacy_level == privacy_level)
    if as_of is not None:
        stmt = stmt.where(
            Memory.valid_from <= as_of,
            (Memory.valid_until.is_(None)) | (Memory.valid_until >= as_of),
        )

    rows = list((await session.execute(stmt)).scalars().all())
    if q:
        retriever = Retriever(session)
        hits = await retriever.search(q, k=limit, access="master", as_of=as_of, memory_types=[memory_type] if memory_type else None)
        hit_ids = {UUID(h.memory_id) for h in hits}
        rows = [m for m in rows if m.id in hit_ids]
        rows.sort(key=lambda m: next(h.score for h in hits if h.memory_id == str(m.id)), reverse=True)
    rows = rows[:limit]
    await log_access(
        session,
        actor=actor,
        action="read",
        endpoint="GET /v1/memories",
        resource_type="memory",
        resource_ids=[m.id for m in rows],
        details={"count": len(rows), "q": q},
    )
    await session.commit()
    return MemoryListResponse(
        memories=[await _memory_out(session, m) for m in rows],
        total=len(rows),
    )


@router.get("/memories/changes", response_model=MemoryChangesResponse)
async def memory_changes(
    since: datetime,
    memory_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> MemoryChangesResponse:
    """Version-chain query: what has the user changed their mind about since ``since``."""
    if since.tzinfo is not None:
        since = since.astimezone(UTC).replace(tzinfo=None)
    stmt = select(Memory).where(Memory.valid_from >= since)
    if memory_type:
        stmt = stmt.where(Memory.memory_type == memory_type)
    changed = list((await session.execute(stmt.limit(limit * 4))).scalars().all())
    group_ids = {m.version_group for m in changed}
    if not group_ids:
        return MemoryChangesResponse(since=since, memory_type=memory_type, total=0, groups=[])

    versions = list(
        (
            await session.execute(
                select(Memory)
                .where(Memory.version_group.in_(group_ids))
                .order_by(Memory.version_group, Memory.version.asc())
            )
        ).scalars().all()
    )
    grouped: dict[UUID, list[Memory]] = {}
    for memory in versions:
        grouped.setdefault(memory.version_group, []).append(memory)

    groups: list[MemoryChangeGroup] = []
    for version_group, rows in grouped.items():
        # "How has my thinking changed" = version chains that were revised after
        # `since` (a new single-version memory is new knowledge, not a change).
        if len(rows) < 2 or rows[-1].valid_from < since:
            continue
        groups.append(
            MemoryChangeGroup(
                version_group=version_group,
                memory_type=rows[-1].memory_type,
                versions=[await _memory_out(session, m) for m in rows],
            )
        )
    groups.sort(key=lambda g: g.versions[-1].valid_from, reverse=True)
    groups = groups[:limit]
    await log_access(
        session,
        actor=actor,
        action="read",
        endpoint="GET /v1/memories/changes",
        resource_type="memory",
        resource_ids=[m.id for g in groups for m in g.versions],
        details={"since": since.isoformat(), "groups": len(groups)},
    )
    await session.commit()
    return MemoryChangesResponse(
        since=since,
        memory_type=memory_type,
        total=len(groups),
        groups=groups,
    )


@router.get("/memories/{memory_id}", response_model=MemoryOut)
async def get_memory(
    memory_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> MemoryOut:
    memory = await session.get(Memory, memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    await log_access(
        session,
        actor=actor,
        action="read",
        endpoint="GET /v1/memories/{id}",
        resource_type="memory",
        resource_ids=[memory.id],
    )
    await session.commit()
    return await _memory_out(session, memory)


@router.get("/audit/{memory_id}", response_model=AuditOut)
async def audit_memory(
    memory_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> AuditOut:
    memory = await session.get(Memory, memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    versions = (
        await session.execute(
            select(Memory)
            .where(Memory.version_group == memory.version_group)
            .order_by(Memory.version.asc())
        )
    ).scalars().all()
    conflicts = (
        await session.execute(
            select(Conflict).where(
                (Conflict.memory_id_a == memory_id) | (Conflict.memory_id_b == memory_id)
            )
        )
    ).scalars().all()
    access_rows = list((await session.execute(select(AccessLog))).scalars().all())
    access_log = [
        row
        for row in access_rows
        if any(str(memory_id) in str(rid) for rid in (row.resource_ids or []))
    ][-20:]
    source_rows = (
        await session.execute(
            select(Event)
            .join(MemoryEvent, MemoryEvent.event_id == Event.id)
            .where(MemoryEvent.memory_id == memory_id)
            .order_by(Event.occurred_at.desc())
        )
    ).scalars().all()
    await log_access(
        session,
        actor=actor,
        action="read",
        endpoint="GET /v1/audit/{id}",
        resource_type="memory",
        resource_ids=[memory_id],
    )
    await session.commit()
    return AuditOut(
        memory=await _memory_out(session, memory),
        versions=[await _memory_out(session, v) for v in versions],
        source_events=[EventOut.model_validate(e) for e in source_rows],
        conflicts=[ConflictOut.model_validate(c) for c in conflicts],
        access_log=[AccessLogOut.model_validate(a) for a in access_log],
    )


@router.get("/conflicts", response_model=list[ConflictOut])
async def list_conflicts(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[ConflictOut]:
    stmt = select(Conflict).order_by(Conflict.created_time.desc())
    if status:
        stmt = stmt.where(Conflict.status == status)
    rows = list((await session.execute(stmt)).scalars().all())
    return [ConflictOut.model_validate(r) for r in rows]


@router.post("/export", response_model=ExportBundle)
async def export_all(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> ExportBundle:
    events = list((await session.execute(select(Event))).scalars().all())
    memories = list((await session.execute(select(Memory))).scalars().all())
    entities = list((await session.execute(select(Entity))).scalars().all())
    relationships = list((await session.execute(select(EntityRelationship))).scalars().all())
    conflicts = list((await session.execute(select(Conflict))).scalars().all())
    await log_access(
        session,
        actor=actor,
        action="export",
        endpoint="POST /v1/export",
        resource_type="all",
        resource_ids=[],
        details={"events": len(events), "memories": len(memories)},
    )
    await session.commit()
    return ExportBundle(
        exported_at=utcnow(),
        events=[EventOut.model_validate(e) for e in events],
        memories=[await _memory_out(session, m) for m in memories],
        entities=[_entity_dict(e) for e in entities],
        relationships=[_relationship_dict(r) for r in relationships],
        conflicts=[ConflictOut.model_validate(c) for c in conflicts],
    )


@router.post("/import", response_model=ImportResponse)
async def import_bundle_endpoint(
    bundle: ExportBundle,
    mode: Literal["merge", "replace"] = Query(default="merge"),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> ImportResponse:
    """Import an export bundle: insert raw events, then rebuild derived state."""
    try:
        result = await import_bundle(session, bundle, mode=mode, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return ImportResponse(**result)


def _entity_dict(entity: Entity) -> dict:
    return {
        "id": str(entity.id),
        "entity_type": entity.entity_type,
        "name": entity.name,
        "aliases": entity.aliases,
        "summary": entity.summary,
        "canonical_key": entity.canonical_key,
        "created_at": entity.created_at.isoformat(),
        "updated_at": entity.updated_at.isoformat(),
    }


def _relationship_dict(rel: EntityRelationship) -> dict:
    return {
        "id": str(rel.id),
        "from_entity_id": str(rel.from_entity_id),
        "to_entity_id": str(rel.to_entity_id),
        "relationship_type": rel.relationship_type,
        "weight": rel.weight,
        "valid_from": rel.valid_from.isoformat(),
        "valid_until": rel.valid_until.isoformat() if rel.valid_until else None,
        "source_type": rel.source_type,
        "source_event_id": str(rel.source_event_id) if rel.source_event_id else None,
    }


@router.post("/chat", response_model=ChatResponse)
async def chat(
    data: ChatRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
):
    if data.stream:
        thread = await conversation.resolve_thread(session, data.conversation_id)
        return StreamingResponse(
            _stream_chat(data, session, actor, thread_id=thread.id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        thread = await conversation.resolve_thread(session, data.conversation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found") from None
    pipeline = await run_chat_pipeline(data, session, actor, thread_id=thread.id)
    return ChatResponse(
        reply=pipeline["result"].text,
        conversation_id=pipeline["conversation_id"],
        model=pipeline["result"].model,
        context_tokens=pipeline["context_tokens"],
        context_depth=pipeline["context_depth"],
        request_id=pipeline["request_id"],
        memory_delta=[MemoryDelta.model_validate(d) for d in pipeline["memory_deltas"]],
        provenance=pipeline["provenance"],
        filter_report=pipeline.get("filter_report"),
    )


def _sse(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"


async def _stream_chat(data: ChatRequest, session: AsyncSession, actor: str, *, thread_id: UUID):
    try:
        pipeline = await run_chat_pipeline(data, session, actor, thread_id=thread_id)
        for delta in pipeline["memory_deltas"]:
            yield _sse("memory-delta", delta)
        for item in pipeline["provenance"]:
            yield _sse("provenance", item.model_dump())
        if pipeline.get("filter_report") is not None:
            yield _sse("filter-report", pipeline["filter_report"].model_dump())
        yield _sse("delta", {"text": pipeline["result"].text})
        yield _sse(
            "done",
            {
                "conversation_id": pipeline["conversation_id"],
                "context_tokens": pipeline["context_tokens"],
                "context_depth": pipeline["context_depth"],
                "request_id": pipeline["request_id"],
                "model": pipeline["result"].model,
            },
        )
    except Exception as exc:  # noqa: BLE001 - stream boundary
        yield _sse("error", {"message": str(exc)})
        yield _sse("done", {})


async def run_chat_pipeline(
    data: ChatRequest,
    session: AsyncSession,
    actor: str,
    *,
    thread_id: UUID,
    source: str = "chat",
    user_event_type: str = "message.user",
    event_privacy: PrivacyLevel = "normal",
) -> dict:
    retriever = Retriever(session)
    request_id = str(uuid4())
    service = EventService(session, actor=actor)

    depth = _resolve_depth(data.context_depth, data.message)
    history_limit, retrieval_k, budget = _depth_profile(depth)

    # Input filter: identity gate + privacy guard run before anything is stored
    # or sent. Credentials never cross to the provider; high-severity injection
    # attempts block the turn entirely.
    input_filter = InputFilter(session)
    input_decision = input_filter.guard(
        message=data.message,
        speaker=SpeakerIdentity(
            actor_id=actor,
            verified=True,
            confidence=1.0,
            method="auth_token",
        ),
    )
    effective_event_privacy: PrivacyLevel = (
        event_privacy
        if event_privacy != "normal"
        else cast(PrivacyLevel, input_decision.privacy_level)
    )

    # Capture the user message as an immutable event.
    user_event = await service.create(
        EventCreate(
            source=source,
            event_type=user_event_type,
            text=data.message,
            conversation_id=thread_id,
            device_id=data.device_id,
            privacy_level=effective_event_privacy,
        ),
        request_id=request_id,
    )
    await session.commit()
    memory_deltas = await ensure_processed(user_event.id)

    history_events = [
        event
        for event in await conversation.history(
            session, thread_id, limit=history_limit, access="model"
        )
        if event.id != user_event.id
    ]
    memories = await retriever.search(
        input_decision.provider_message, k=retrieval_k, access="model"
    )
    user_state = await build_user_state(session, access="model")
    if depth != "standard":
        secondary_query = (
            user_state.current_task or user_state.active_project or input_decision.provider_message
        )
        if secondary_query != input_decision.provider_message:
            extra = await retriever.search(secondary_query, k=retrieval_k, access="model")
            seen = {m.memory_id for m in memories}
            memories = [*memories, *[m for m in extra if m.memory_id not in seen]]
            memories.sort(key=lambda m: m.score, reverse=True)
    decision, memories, grounding, _strategy_hint = await input_filter.run(
        message=data.message,
        speaker=SpeakerIdentity(
            actor_id=actor,
            verified=True,
            confidence=1.0,
            method="auth_token",
        ),
        decision=input_decision,
        memories=memories,
        k=retrieval_k,
    )
    await record_decision(
        session,
        request_id=request_id,
        conversation_id=thread_id,
        stage="input",
        action="block" if decision.blocked else "run",
        name="input_filter",
        severity="high" if decision.blocked else "info",
        detail={
            "flags": [f.to_dict() for f in decision.flags],
            "privacy_level": decision.privacy_level,
        },
        draft=data.message,
        final_text=decision.provider_message,
    )
    for flag in decision.flags:
        if flag.action != "allow":
            await record_decision(
                session,
                request_id=request_id,
                conversation_id=thread_id,
                stage="input",
                action=flag.action,
                name=flag.name,
                severity=flag.severity,
                detail={"flag": flag.detail},
                draft=data.message,
                final_text=decision.provider_message,
            )
    await session.commit()
    loops = await PatternEngine(session).decision_loops(min_count=2)
    pattern_confidence = max((p.confidence for p in memories if p.memory_type == "pattern"), default=0.0)
    loop_count = max((loop["count"] for loop in loops), default=0)
    profile = await get_current(session)
    tuning = await proactive_tuning(session)
    pending_alerts = await alert_radar.list_alerts(session, status="pending", limit=10)
    alert_priority = max((a.priority for a in pending_alerts), default=0.0)
    alert_tier = next(
        (a.tier for a in pending_alerts if a.priority == alert_priority),
        None,
    )
    strategy = build_strategy(
        data.message,
        decision_loop_count=loop_count,
        pattern_confidence=pattern_confidence,
        evidence_count=len(memories),
        profile=to_dict(profile),
        pending_alert_priority=alert_priority,
        pending_alert_tier=alert_tier,
        challenge_ceiling=tuning.challenge_ceiling,
    )
    model_rollup = await rollup_service.model_safe_rollup(session, thread_id)
    state = await conversation.get_or_create_state(session, thread_id)
    open_questions = list(
        dict.fromkeys([*(model_rollup.open_questions or []), *(state.pending_questions or [])])
    )[:5]
    context, context_tokens = _assemble_context(
        memories,
        user_state=user_state,
        strategy_text=strategy_block(strategy),
        budget=budget,
        rollup_summary=model_rollup.summary,
        open_questions=open_questions,
        history=[
            {
                "role": "assistant" if e.event_type == "message.assistant" else "user",
                "text": (e.content or {}).get("text") or "",
            }
            for e in history_events
        ],
    )

    if decision.blocked:
        final_draft = (
            "I can't process that request — it was blocked by EV's input filter "
            "before anything reached the model."
        )
        report = OutputReport(
            draft=final_draft,
            final_text=final_draft,
            flags=decision.flags,
        )
        result = ChatResult(text=final_draft)
        envelope_hash = None
    else:
        envelope_hash = compute_envelope_hash(
            message=decision.provider_message,
            context=context,
            strategy=strategy.model_dump(),
            privacy_level=decision.privacy_level,
            speaker_method="auth_token",
        )
        envelope = RequestEnvelope(
            request_id=request_id,
            strategy=strategy.model_dump(),
            memories=[
                MemoryRef(
                    memory_id=m.memory_id,
                    memory_type=m.memory_type,
                    text=m.text,
                    score=m.score,
                    event_time=m.event_time.isoformat() if m.event_time else None,
                )
                for m in memories
            ],
            conversation_id=str(thread_id),
            device_id=data.device_id,
            context_tokens=context_tokens,
            metadata={
                "context_depth": depth,
                "open_questions": open_questions,
                "rollup_summary": (model_rollup.summary[:500] if model_rollup.summary else None),
                "user_state": {
                    "activity": user_state.activity,
                    "current_task": user_state.current_task,
                    "active_project": user_state.active_project,
                    "active_goal": user_state.active_goal,
                },
                "privacy_level": decision.privacy_level,
                "envelope_hash": envelope_hash,
            },
        )
        system_prompt = (
            f"{identity_block(settings.persona_name, settings.persona_description, to_dict(profile))}\n\n"
            "You reason over memory that EV's system has retrieved for you; never invent memories. "
            "Be honest about uncertainty, cite dates/sources when you use them, and keep the user's "
            "goals in mind.\n\n"
            f"{context}"
        )
        provider = get_chat_provider()
        gateway = ModelGateway(provider)
        call = await gateway.chat(
            [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=decision.provider_message),
            ],
            envelope=envelope,
            model=data.model,
        )
        result = call.result
        if call.status == "blocked":
            raise HTTPException(
                status_code=403,
                detail=f"Model boundary blocked this request: {call.error}",
            )
        if call.status == "error":
            raise HTTPException(
                status_code=503,
                detail=f"Model provider unavailable: {call.error}",
            )
        if settings.model_call_log_enabled:
            await log_model_call(session, call=call, actor=actor)

        critic = None
        if settings.filter_critic_enabled and strategy.mode in settings.filter_critic_modes:
            from app.filter.critic import GatewayCritic

            critic = GatewayCritic(gateway, request_id=request_id, envelope=envelope)
        report = await run_output_filter(
            result.text,
            strategy=strategy,
            grounding=grounding,
            max_iterations=settings.filter_critic_max_iterations,
            critic=critic,
        )
        result.text = report.final_text
        for flag in report.flags:
            if flag.action != "allow":
                await record_decision(
                    session,
                    request_id=request_id,
                    conversation_id=thread_id,
                    stage="output",
                    action=flag.action,
                    name=flag.name,
                    severity=flag.severity,
                    detail={"flag": flag.detail},
                    draft=report.draft,
                    final_text=report.final_text,
                    scores=report.critic,
                    iterations=report.iterations,
                    envelope_hash=envelope_hash,
                    model=result.model,
                )
        seen_flags = {(f.stage, f.name) for f in report.flags}
        for flag in decision.flags:
            if (flag.stage, flag.name) not in seen_flags:
                report.flags.append(flag)
                seen_flags.add((flag.stage, flag.name))
        await record_decision(
            session,
            request_id=request_id,
            conversation_id=thread_id,
            stage="pipeline",
            action="run",
            name="intelligence_filter",
            detail={
                "context_tokens": context_tokens,
                "provider": provider.name,
                "claims": [c.to_dict() for c in report.claims],
                "iterations": report.iterations,
                "passed": report.passed,
                "critic_costs": [
                    edit["costs"]
                    for edit in report.edits
                    if edit.get("type") == "critic_revision"
                ],
            },
            final_text=report.final_text,
            scores=report.critic,
            iterations=report.iterations,
            envelope_hash=envelope_hash,
            model=result.model,
        )
        await session.commit()

    assistant_event = await service.create(
        EventCreate(
            source=source,
            event_type="message.assistant",
            text=result.text,
            conversation_id=thread_id,
            device_id=data.device_id,
            privacy_level=effective_event_privacy,
        ),
        request_id=request_id,
    )
    await session.commit()
    await rollup_service.build_rollup(session, thread_id)
    await log_response(
        session,
        request_text=data.message,
        reply_text=result.text,
        mode=strategy.mode,
        strategy=strategy.model_dump(),
        provenance_ids=[m.memory_id for m in memories[:10]],
        context_tokens=context_tokens,
        model=result.model,
    )
    await session.commit()
    focus = None
    from app.ev.edith import active_focus

    designated = await active_focus(session)
    focus = designated.label if designated is not None else user_state.current_task
    topics = [t for t in user_state.recent_topics[:3]]
    await conversation.update_state(
        session,
        thread_id,
        focus=focus,
        topics=topics,
        pending_questions=open_questions,
        working_context={
            "last_user_message": data.message[:1000],
            "last_assistant_message": result.text[:1000],
            "context_tokens": context_tokens,
            "context_depth": depth,
        },
    )
    await session.commit()

    provenance = [
        ProvenanceItem(
            memory_id=UUID(m.memory_id),
            text=m.text,
            memory_type=m.memory_type,
            score=m.score,
            components=m.components,
        )
        for m in memories[:10]
    ] if not decision.blocked else []
    return {
        "result": result,
        "request_id": request_id,
        "conversation_id": user_event.conversation_id or assistant_event.conversation_id,
        "context_tokens": context_tokens,
        "context_depth": depth,
        "memory_deltas": memory_deltas,
        "provenance": provenance,
        "strategy": strategy,
        "filter_report": FilterReportOut.model_validate(report.to_dict()),
    }


@router.post("/attachments", response_model=AttachmentCreateResponse, status_code=201)
async def create_attachment(
    file: UploadFile = File(...),
    event_type: str = Form(default="file"),
    source: str = Form(default="attachment"),
    privacy_level: Literal["private", "normal", "sensitive", "never_send_to_model"] = Form(
        default="normal"
    ),
    device_id: str | None = Form(default=None),
    occurred_at: datetime | None = Form(default=None),
    metadata: str = Form(default="{}"),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> AttachmentCreateResponse:
    data = await file.read()
    try:
        meta = json.loads(metadata) if metadata else {}
    except json.JSONDecodeError:
        meta = {"raw": metadata}
    store = get_object_store()
    storage_key = f"attachments/{uuid4()}.bin"
    await store.put(storage_key, data, file.content_type)

    service = EventService(session, actor=actor)
    event = await service.create(
        EventCreate(
            source=source,
            event_type=event_type,
            content={
                "filename": file.filename,
                "content_type": file.content_type,
                "size_bytes": len(data),
                "storage_key": storage_key,
            },
            metadata=meta,
            device_id=device_id,
            privacy_level=privacy_level,
            occurred_at=occurred_at,
        )
    )
    attachment = Attachment(
        event_id=event.id,
        filename=file.filename or "unnamed",
        content_type=file.content_type,
        size_bytes=len(data),
        storage_key=storage_key,
        sha256=sha256_bytes(data),
    )
    session.add(attachment)
    await session.commit()
    await ensure_processed(event.id)
    return AttachmentCreateResponse(
        attachment=AttachmentOut.model_validate(attachment),
        event=EventOut.model_validate(event),
    )


@router.get("/attachments/{attachment_id}")
async def download_attachment(
    attachment_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> Response:
    attachment = await session.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    data = await get_object_store().get(attachment.storage_key)
    return Response(
        content=data,
        media_type=attachment.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{attachment.filename}"'},
    )


def _assemble_context(
    memories,
    *,
    user_state,
    strategy_text: str,
    budget: int,
    history: list[dict] | None = None,
    rollup_summary: str | None = None,
    open_questions: list[str] | None = None,
) -> tuple[str, int]:
    """Compile the request window through the ContextCompiler (plan 2.4)."""
    from app.context.compiler import ContextCompiler

    plan = ContextCompiler().compile(
        memories=memories,
        user_state=user_state,
        strategy_text=strategy_text,
        budget=budget,
        history=history,
        rollup_summary=rollup_summary,
        open_questions=open_questions,
    )
    return plan.text, plan.used_tokens


def _resolve_depth(requested: str, message: str) -> str:
    """'auto' promotes continuity phrasings to deep context."""
    if requested != "auto":
        return requested
    return "deep" if rollup_service.wants_deep_context(message) else "standard"


def _depth_profile(depth: str) -> tuple[int, int, int]:
    """(history_turns, retrieval_memories, token_budget) per depth."""
    if depth == "deepest":
        return (
            settings.deepest_history_turns,
            settings.deepest_retrieval_memories,
            min(settings.context_budget_tokens * 3, 60_000),
        )
    if depth == "deep":
        return (
            settings.deep_history_turns,
            settings.deep_retrieval_memories,
            min(settings.context_budget_tokens * 2, 40_000),
        )
    return (
        settings.standard_history_turns,
        settings.max_retrieval_memories,
        settings.context_budget_tokens,
    )


@router.post("/devices", response_model=DeviceCreateResponse, status_code=201)
async def create_device(
    data: DeviceCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> DeviceCreateResponse:
    token = secrets.token_urlsafe(32)
    owner = (
        await session.execute(select(OwnerIdentity).order_by(OwnerIdentity.created_at.asc()).limit(1))
    ).scalar_one_or_none()
    device = Device(
        name=data.name,
        token_hash=sha256_hex(token),
        capabilities=data.capabilities,
        trust_level=data.trust_level,
        owner_id=owner.id if owner else None,
    )
    session.add(device)
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="write",
        endpoint="POST /v1/devices",
        resource_type="device",
        resource_ids=[device.id],
    )
    await session.commit()
    return DeviceCreateResponse(device=DeviceOut.model_validate(device), token=token)


@router.get("/devices", response_model=list[DeviceOut])
async def list_devices(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> list[DeviceOut]:
    rows = list((await session.execute(select(Device).order_by(Device.created_at.asc()))).scalars().all())
    return [DeviceOut.model_validate(d) for d in rows]


@router.delete("/devices/{device_id}", response_model=DeviceOut)
async def revoke_device(
    device_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> DeviceOut:
    device = await session.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    device.revoked_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="delete",
        endpoint="DELETE /v1/devices/{id}",
        resource_type="device",
        resource_ids=[device.id],
    )
    await session.commit()
    return DeviceOut.model_validate(device)


@router.get("/gateway/models")
async def gateway_models(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    provider = get_chat_provider()
    return {"provider": provider.name, "models": await provider.list_models()}


@router.post("/gateway/chat", response_model=GatewayChatResponse)
async def gateway_chat(
    data: GatewayChatRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> GatewayChatResponse:
    actor = ctx.actor
    sensitive_allowed = data.allow_sensitive_tools
    if sensitive_allowed and not (
        ctx.is_master or (ctx.device is not None and ctx.device.trust_level == "owner")
    ):
        raise HTTPException(
            status_code=403,
            detail="Sensitive tools require owner-level trust",
            headers={"X-Error-Code": "owner_trust_required"},
        )
    request_id = data.request_id or str(uuid4())
    memories = [
        MemoryRef(
            memory_id=str(m["memory_id"]),
            memory_type=str(m.get("memory_type", "")),
            text=str(m.get("text", "")),
            score=float(m.get("score", 0.0) or 0.0),
            event_time=str(m["event_time"]) if m.get("event_time") else None,
        )
        for m in data.memories
        if m.get("memory_id")
    ]
    envelope = RequestEnvelope(
        request_id=request_id,
        strategy=data.strategy or {},
        memories=memories,
        conversation_id=str(data.conversation_id) if data.conversation_id else None,
        device_id=data.device_id,
        context_tokens=int(data.context.get("context_tokens", 0) or 0),
        metadata=data.context,
    )
    gateway = ModelGateway(get_chat_provider())
    call = await gateway.chat(
        [ChatMessage(role=m.role, content=m.content, name=m.name) for m in data.messages],
        envelope=envelope,
        tools=tool_specs_from_dicts(data.tools),
        model=data.model,
        temperature=data.temperature,
        allow_sensitive_tools=sensitive_allowed,
    )
    if settings.model_call_log_enabled:
        await log_model_call(session, call=call, actor=actor)
    await session.commit()
    return GatewayChatResponse(
        text=call.result.text,
        tool_calls=[
            GatewayToolCall(id=t.id, name=t.name, arguments=t.arguments)
            for t in call.result.tool_calls
        ],
        usage=call.result.usage,
        request_id=call.request_id,
        provider=call.provider,
        model=call.model,
        latency_ms=call.latency_ms,
        status=call.status,
        error=call.error,
        tool_validation=call.tool_calls_dict(),
        envelope=call.envelope.to_dict(memory_text_limit=160),
    )


@router.get("/gateway/calls", response_model=list[ModelCallOut])
async def gateway_calls(
    limit: int = Query(default=50, ge=1, le=200),
    request_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[ModelCallOut]:
    """Audit view of model calls: provider, model, latency, usage, envelope."""

    rows = await list_model_calls(session, limit=limit, request_id=request_id)
    return [ModelCallOut.model_validate(row) for row in rows]


@router.get("/people", response_model=MemoryListResponse)
async def list_people(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> MemoryListResponse:
    rows = list(
        (
            await session.execute(
                select(Memory)
                .join(MemoryEntity, MemoryEntity.memory_id == Memory.id)
                .join(Entity, Entity.id == MemoryEntity.entity_id)
                .where(Entity.entity_type == "person", Memory.is_current.is_(True))
                .order_by(Memory.event_time.desc())
                .limit(limit)
            )
        ).scalars().all()
    )
    return MemoryListResponse(memories=[await _memory_out(session, m) for m in rows], total=len(rows))


def _typed_browse(memory_type: str):
    async def browse(
        limit: int = Query(default=50, ge=1, le=200),
        session: AsyncSession = Depends(get_session),
        actor: str = Depends(require_actor),
    ) -> MemoryListResponse:
        rows = list(
            (
                await session.execute(
                    select(Memory)
                    .where(Memory.memory_type == memory_type, Memory.is_current.is_(True))
                    .order_by(Memory.event_time.desc())
                    .limit(limit)
                )
            ).scalars().all()
        )
        return MemoryListResponse(
            memories=[await _memory_out(session, m) for m in rows],
            total=len(rows),
        )

    return browse


router.get("/decisions", response_model=MemoryListResponse)(_typed_browse("decision"))
router.get("/goals", response_model=MemoryListResponse)(_typed_browse("goal"))
router.get("/preferences", response_model=MemoryListResponse)(_typed_browse("preference"))
router.get("/patterns", response_model=MemoryListResponse)(_typed_browse("pattern"))


@router.post("/patterns/analyze")
async def analyze_patterns(
    window_days: int = Query(default=30, ge=1, le=365),
    min_count: int = Query(default=3, ge=2, le=20),
    recent_days: int = Query(default=7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    executed_at = utcnow()
    await EventService(session, actor=actor).create(
        EventCreate(
            source="system",
            event_type="pattern.analyze",
            text=f"Pattern analysis: window {window_days} days, min_count {min_count}",
            metadata={
                "window_days": window_days,
                "min_count": min_count,
                "executed_at": executed_at.isoformat(),
            },
        )
    )
    engine = PatternEngine(session)
    written = await engine.analyze(
        window_days=window_days,
        min_count=min_count,
        recent_days=recent_days,
    )
    loops = await engine.decision_loops(window_days=window_days)
    await session.commit()
    return {"written": written, "decision_loops": loops}


@router.post("/consolidate", response_model=ConsolidationOut)
async def consolidate(
    granularity: Literal["day", "week", "month"] = Query(default="day"),
    period_start: datetime = Query(...),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ConsolidationOut:
    """Derive a deterministic period summary over the raw event log."""
    if period_start.tzinfo is None:
        period_start = period_start.replace(tzinfo=UTC)
    period_end = next_period_start(period_start, granularity)
    executed_at = utcnow()
    await EventService(session, actor=actor).create(
        EventCreate(
            source="system",
            event_type="consolidation.run",
            text=f"Consolidation: {granularity} starting {period_start.isoformat()}",
            metadata={
                "granularity": granularity,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "executed_at": executed_at.isoformat(),
            },
        )
    )
    written = await run_consolidation(
        session,
        granularity=granularity,
        period_start=period_start,
        period_end=period_end,
        as_of=executed_at,
    )
    await session.commit()
    return ConsolidationOut(
        granularity=granularity,
        period_start=period_start,
        period_end=period_end,
        executed_at=executed_at,
        written=[UUID(w) for w in written],
    )


@router.post("/memory/rebuild", response_model=RebuildOut)
async def rebuild_memory(
    reason: str = Query(default="manual rebuild", max_length=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RebuildOut:
    result = await rebuild_derived_state(session, actor=actor, reason=reason)
    await session.commit()
    return RebuildOut(**result)


@router.get("/state", response_model=UserStateOut)
async def current_user_state(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> UserStateOut:
    return await build_user_state(session)
