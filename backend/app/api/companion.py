"""Companion API: research, maker, HUD, route, personality, guardrails, self-eval, tools."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.core import _memory_out
from app.auth import require_actor
from app.db import get_session
from app.ev import (
    companionship,
    conversation,
    hud,
    maker,
    memory_ops,
    navigation,
    personality,
    research,
    self_eval,
    tool_select,
    tools,
)
from app.ev.rollup import build_rollup
from app.ev.user_state import build_user_state
from app.models import Event
from app.schemas import (
    BomItemCreate,
    BomItemOut,
    ContinueResponse,
    EvaluationUpdate,
    HudAlertOut,
    HudCardOut,
    HudOpsCardOut,
    IsolationScanOut,
    MakerNextStepOut,
    MakerProjectCreate,
    MakerProjectOut,
    MakerProjectStatusUpdate,
    MemoryCorrectionCreate,
    MemoryForgetCreate,
    MemoryOut,
    PersonalityOut,
    PersonalityUpdate,
    PrintJobCreate,
    PrintJobOut,
    PrintJobStatusUpdate,
    RelationshipOut,
    ResearchConclude,
    ResearchNoteCreate,
    ResearchNoteOut,
    ResearchSessionCreate,
    ResearchSessionDetail,
    ResearchSessionOut,
    ResponseLogOut,
    RouteBriefingOut,
    SelfEvalAggregate,
    ToolCallRequest,
    ToolCallResponse,
    ToolSelectionRequest,
    ToolSelectionResponse,
    ToolSpecOut,
)

router = APIRouter(prefix="/v1")


# --------------------------------------------------------------------------- #
# Research assistant
# --------------------------------------------------------------------------- #


@router.post("/research/sessions", response_model=ResearchSessionOut, status_code=201)
async def create_research_session(
    data: ResearchSessionCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ResearchSessionOut:
    service = research.ResearchService(session, actor=actor)
    row = await service.create_session(data)
    await session.commit()
    return ResearchSessionOut.model_validate(row)


@router.get("/research/sessions", response_model=list[ResearchSessionOut])
async def list_research_sessions(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[ResearchSessionOut]:
    rows = await research.list_sessions(session, status=status, limit=limit)
    return [ResearchSessionOut.model_validate(r) for r in rows]


@router.get("/research/sessions/{session_id}", response_model=ResearchSessionDetail)
async def get_research_session(
    session_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ResearchSessionDetail:
    service = research.ResearchService(session, actor=actor)
    row = await service.detail(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Research session not found")
    notes = await research.list_notes(session, session_id)
    return ResearchSessionDetail(
        **ResearchSessionOut.model_validate(row).model_dump(),
        notes=[ResearchNoteOut.model_validate(n) for n in notes],
    )


@router.post("/research/sessions/{session_id}/notes", response_model=ResearchNoteOut, status_code=201)
async def add_research_note(
    session_id: UUID,
    data: ResearchNoteCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ResearchNoteOut:
    service = research.ResearchService(session, actor=actor)
    try:
        note = await service.add_note(session_id, data)
    except KeyError:
        raise HTTPException(status_code=404, detail="Research session not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await session.commit()
    return ResearchNoteOut.model_validate(note)


@router.post("/research/sessions/{session_id}/conclude", response_model=ResearchSessionOut)
async def conclude_research(
    session_id: UUID,
    data: ResearchConclude,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ResearchSessionOut:
    service = research.ResearchService(session, actor=actor)
    try:
        row, _memory = await service.conclude(session_id, data)
    except KeyError:
        raise HTTPException(status_code=404, detail="Research session not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await session.commit()
    return ResearchSessionOut.model_validate(row)


# --------------------------------------------------------------------------- #
# Maker companion
# --------------------------------------------------------------------------- #


@router.post("/projects", response_model=MakerProjectOut, status_code=201)
async def create_project(
    data: MakerProjectCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> MakerProjectOut:
    project = await maker.create_project(session, data)
    await session.commit()
    return MakerProjectOut.model_validate(project)


@router.get("/projects", response_model=list[MakerProjectOut])
async def list_projects(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[MakerProjectOut]:
    rows = await maker.list_projects(session, status=status, limit=limit)
    return [MakerProjectOut.model_validate(r) for r in rows]


@router.get("/projects/{project_id}", response_model=dict)
async def get_project(
    project_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    project = await maker.get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "project": MakerProjectOut.model_validate(project).model_dump(),
        "bom": [BomItemOut.model_validate(item).model_dump() for item in await maker.list_bom(session, project_id)],
        "print_jobs": [
            PrintJobOut.model_validate(job).model_dump()
            for job in await maker.list_print_jobs(session, project_id)
        ],
        "next_step": maker.next_step(project),
    }


@router.patch("/projects/{project_id}/status", response_model=MakerProjectOut)
async def update_project_status(
    project_id: UUID,
    data: MakerProjectStatusUpdate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> MakerProjectOut:
    try:
        project = await maker.update_status(session, project_id, data)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await session.commit()
    return MakerProjectOut.model_validate(project)


@router.get("/projects/{project_id}/next-step", response_model=MakerNextStepOut)
async def project_next_step(
    project_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> MakerNextStepOut:
    project = await maker.get_project(session, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return MakerNextStepOut.model_validate(maker.next_step(project))


@router.post("/projects/{project_id}/bom", response_model=BomItemOut, status_code=201)
async def add_bom_item(
    project_id: UUID,
    data: BomItemCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> BomItemOut:
    try:
        item = await maker.add_bom_item(session, project_id, data)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found") from None
    await session.commit()
    return BomItemOut.model_validate(item)


@router.get("/projects/{project_id}/bom", response_model=list[BomItemOut])
async def list_bom_items(
    project_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[BomItemOut]:
    items = await maker.list_bom(session, project_id)
    return [BomItemOut.model_validate(item) for item in items]


@router.delete("/projects/{project_id}/bom/{item_id}", status_code=204)
async def delete_bom_item(
    project_id: UUID,
    item_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> None:
    try:
        await maker.delete_bom_item(session, item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="BOM item not found") from None
    await session.commit()


@router.post("/projects/{project_id}/print-jobs", response_model=PrintJobOut, status_code=201)
async def create_print_job(
    project_id: UUID,
    data: PrintJobCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PrintJobOut:
    try:
        job = await maker.create_print_job(session, project_id, data)
    except KeyError:
        raise HTTPException(status_code=404, detail="Project not found") from None
    await session.commit()
    return PrintJobOut.model_validate(job)


@router.get("/projects/{project_id}/print-jobs", response_model=list[PrintJobOut])
async def list_print_jobs(
    project_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[PrintJobOut]:
    jobs = await maker.list_print_jobs(session, project_id)
    return [PrintJobOut.model_validate(job) for job in jobs]


@router.post("/print-jobs/{job_id}/status", response_model=PrintJobOut)
async def update_print_job_status(
    job_id: UUID,
    data: PrintJobStatusUpdate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PrintJobOut:
    try:
        job = await maker.update_print_job(session, job_id, data)
    except KeyError:
        raise HTTPException(status_code=404, detail="Print job not found") from None
    await session.commit()
    return PrintJobOut.model_validate(job)


# --------------------------------------------------------------------------- #
# HUD & navigation
# --------------------------------------------------------------------------- #


@router.get("/hud/card", response_model=HudCardOut)
async def hud_card(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> HudCardOut:
    return await hud.status_card(session)


@router.get("/hud/alerts", response_model=list[HudAlertOut])
async def hud_alerts(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[HudAlertOut]:
    """Pending alerts rendered as strict HUD cards (ev.hud.alert.v1)."""
    return await hud.alerts_card(session)


@router.get("/hud/ops", response_model=HudOpsCardOut)
async def hud_ops(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> HudOpsCardOut:
    """Unified ops center as a strict HUD command card (ev.hud.ops.v1)."""
    from app.ev.edith import ops_card

    return await ops_card(session)


@router.get("/hud/route", response_model=RouteBriefingOut)
async def hud_route(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RouteBriefingOut:
    return await navigation.route_briefing(session)


# --------------------------------------------------------------------------- #
# Personality & relationship
# --------------------------------------------------------------------------- #


@router.get("/personality", response_model=PersonalityOut)
async def get_personality(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PersonalityOut:
    profile = await personality.get_current(session)
    await session.commit()
    return PersonalityOut.model_validate(profile)


@router.post("/personality", response_model=PersonalityOut)
async def update_personality(
    data: PersonalityUpdate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PersonalityOut:
    profile = await personality.update(session, data)
    await session.commit()
    return PersonalityOut.model_validate(profile)


@router.get("/relationship", response_model=RelationshipOut)
async def relationship(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RelationshipOut:
    return await companionship.relationship_stats(session)


@router.post("/companionship/scan", response_model=IsolationScanOut)
async def companionship_scan(
    window_days: int = Query(default=14, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> IsolationScanOut:
    result = await companionship.scan_isolation(session, window_days=window_days)
    await session.commit()
    return result


# --------------------------------------------------------------------------- #
# Self-evaluation
# --------------------------------------------------------------------------- #


@router.get("/evaluations", response_model=list[ResponseLogOut])
async def list_evaluations(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[ResponseLogOut]:
    rows = await self_eval.list_logs(session, limit=limit)
    return [ResponseLogOut.model_validate(r) for r in rows]


@router.get("/evaluations/summary", response_model=SelfEvalAggregate)
async def evaluation_summary(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> SelfEvalAggregate:
    return await self_eval.aggregate(session)


@router.post("/evaluations/{response_id}", response_model=ResponseLogOut)
async def update_evaluation(
    response_id: UUID,
    data: EvaluationUpdate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ResponseLogOut:
    try:
        row = await self_eval.update_evaluation(session, response_id, data)
    except KeyError:
        raise HTTPException(status_code=404, detail="Response log not found") from None
    await session.commit()
    return ResponseLogOut.model_validate(row)


# --------------------------------------------------------------------------- #
# Tool orchestration
# --------------------------------------------------------------------------- #


@router.get("/tools", response_model=list[ToolSpecOut])
async def list_tools(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[ToolSpecOut]:
    return [ToolSpecOut.model_validate(spec) for spec in tools.list_tools()]


@router.post("/gateway/tools", response_model=ToolCallResponse)
async def call_tool(
    data: ToolCallRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ToolCallResponse:
    """Declarative tool dispatch used by the orchestrator/gateway.

    Every invocation is validated against the registry schema, checked against
    the permission matrix (sensitive tools need an explicit gate), executed,
    and written to the access log before commit.
    """
    response = await tools.dispatch(
        session,
        data.name,
        data.arguments,
        actor=actor,
        allow_sensitive=data.allow_sensitive,
        request_id=data.request_id,
    )
    await session.commit()
    return response


@router.post("/gateway/select-tool", response_model=ToolSelectionResponse)
async def select_tool(
    data: ToolSelectionRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ToolSelectionResponse:
    """Rule-based tool selection for simple intents."""
    return tool_select.select_tool(data.message)


# --------------------------------------------------------------------------- #
# Memory correction & forgetting
# --------------------------------------------------------------------------- #


@router.post("/memories/{memory_id}/correct", response_model=MemoryOut, status_code=201)
async def correct_memory(
    memory_id: UUID,
    data: MemoryCorrectionCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> MemoryOut:
    try:
        memory = await memory_ops.correct_memory(
            session,
            memory_id,
            corrected_text=data.corrected_text,
            reason=data.reason,
            actor=actor,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Memory not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await session.commit()
    return await _memory_out(session, memory)


@router.post("/memories/{memory_id}/forget", response_model=MemoryOut)
async def forget_memory(
    memory_id: UUID,
    data: MemoryForgetCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> MemoryOut:
    try:
        memory = await memory_ops.forget_memory(
            session,
            memory_id,
            reason=data.reason,
            actor=actor,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Memory not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await session.commit()
    return await _memory_out(session, memory)


@router.post("/memories/{memory_id}/restore", response_model=MemoryOut)
async def restore_memory(
    memory_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> MemoryOut:
    try:
        memory = await memory_ops.restore_memory(session, memory_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Memory not found") from None
    await session.commit()
    return await _memory_out(session, memory)


# --------------------------------------------------------------------------- #
# Cognitive load: "continue where we left off"
# --------------------------------------------------------------------------- #


@router.post("/continue", response_model=ContinueResponse)
async def continue_session(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ContinueResponse:
    thread = await conversation.get_default_thread(session)
    rollup = await build_rollup(session, thread.id)
    thread_messages = await conversation.history(session, thread.id, limit=5)
    state = await build_user_state(session)
    rows = (
        await session.execute(
            select(Event)
            .where(
                Event.tombstoned_at.is_(None),
                Event.event_type.in_(["note", "voice", "share"]),
            )
            .order_by(Event.occurred_at.desc())
            .limit(5)
        )
    ).scalars().all()
    combined = {event.id: event for event in [*thread_messages, *rows]}
    recent_context = [
        {
            "event_id": str(event.id),
            "occurred_at": event.occurred_at.isoformat(),
            "text": ((event.content or {}).get("text") or "")[:300],
        }
        for event in sorted(combined.values(), key=lambda e: e.occurred_at, reverse=True)[:5]
    ]

    next_actions: list[str] = []
    if state.current_task:
        next_actions.append(f"Continue: {state.current_task}")
    if state.open_decisions:
        next_actions.append(f"Settle the open decision: {state.open_decisions[0]['text']}")
    conv_state = await conversation.get_or_create_state(session, thread.id)
    if conv_state.pending_questions:
        next_actions.append(f"Answer the open question: {conv_state.pending_questions[0]}")
    if state.recent_failures:
        next_actions.append("Review the last blocker before pushing forward.")
    if state.active_goal:
        next_actions.append(f"Keep the active goal moving: {state.active_goal}")
    if not next_actions:
        next_actions.append("No active task detected. Tell EV what you want to pick up.")

    focus = (
        state.current_task
        or state.active_project
        or state.active_goal
        or "No active focus"
    )
    return ContinueResponse(
        resolved=bool(state.current_task or state.active_project or state.active_goal),
        focus=focus,
        conversation_id=thread.id,
        state=state,
        summary=rollup.summary,
        recent_context=recent_context,
        next_actions=next_actions,
    )
