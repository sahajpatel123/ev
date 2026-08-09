"""Routines & automations API: definitions, scheduler tick, run history, and
approval/undo workflows."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor
from app.db import get_session
from app.routines import service as routines_service
from app.schemas import (
    RoutineCreate,
    RoutineManualRunRequest,
    RoutineOut,
    RoutineOverviewOut,
    RoutineRunDecisionRequest,
    RoutineRunOut,
    RoutineTemplateInstantiateRequest,
    RoutineTemplateOut,
    RoutineTickOut,
    RoutineUpdate,
)

router = APIRouter(prefix="/v1/routines")


@router.get("/templates", response_model=list[RoutineTemplateOut])
async def list_templates(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[RoutineTemplateOut]:
    from app.routines.templates import list_templates

    return [RoutineTemplateOut(**vars(t)) for t in list_templates()]


@router.post(
    "/templates/{slug}/instantiate",
    response_model=RoutineOut,
    status_code=201,
)
async def instantiate_template(
    slug: str,
    data: RoutineTemplateInstantiateRequest | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineOut:
    from pydantic import ValidationError

    try:
        routine = await routines_service.instantiate_template(
            session,
            slug,
            actor=actor,
            name=data.name if data else None,
            overrides=data.overrides if data else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    return RoutineOut.model_validate(routine)


@router.get("/overview", response_model=RoutineOverviewOut)
async def overview(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineOverviewOut:
    result = await routines_service.overview(session)
    await session.commit()
    return result


@router.post("", response_model=RoutineOut, status_code=201)
async def create_routine(
    data: RoutineCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineOut:
    try:
        routine = await routines_service.create_routine(session, data, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    return RoutineOut.model_validate(routine)


@router.get("", response_model=list[RoutineOut])
async def list_routines(
    kind: Literal["scheduled", "trigger"] | None = None,
    enabled: bool | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[RoutineOut]:
    rows = await routines_service.list_routines(session, kind=kind, enabled=enabled)
    return [RoutineOut.model_validate(row) for row in rows]


@router.patch("/{routine_id}", response_model=RoutineOut)
async def update_routine(
    routine_id: UUID,
    data: RoutineUpdate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineOut:
    try:
        routine = await routines_service.update_routine(session, routine_id, data, actor=actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await session.commit()
    return RoutineOut.model_validate(routine)


@router.post("/{routine_id}/disable", response_model=RoutineOut)
async def disable_routine(
    routine_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineOut:
    try:
        routine = await routines_service.set_enabled(
            session, routine_id, False, actor=actor
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    await session.commit()
    return RoutineOut.model_validate(routine)


@router.post("/{routine_id}/enable", response_model=RoutineOut)
async def enable_routine(
    routine_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineOut:
    try:
        routine = await routines_service.set_enabled(
            session, routine_id, True, actor=actor
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    await session.commit()
    return RoutineOut.model_validate(routine)


@router.post("/{routine_id}/run", response_model=RoutineRunOut, status_code=201)
async def run_routine_now(
    routine_id: UUID,
    data: RoutineManualRunRequest | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineRunOut:
    try:
        run = await routines_service.manual_run(
            session,
            routine_id,
            actor=actor,
            reason=data.reason if data else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    await session.commit()
    return RoutineRunOut.model_validate(run)


@router.post("/tick", response_model=RoutineTickOut)
async def tick(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineTickOut:
    outcome = await routines_service.tick(session)
    await session.commit()
    return outcome


@router.get("/runs", response_model=list[RoutineRunOut])
async def list_all_runs(
    routine_id: UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[RoutineRunOut]:
    rows = await routines_service.list_runs(
        session, routine_id=routine_id, status_filter=status_filter, limit=limit
    )
    return [RoutineRunOut.model_validate(row) for row in rows]


@router.get("/{routine_id}/runs", response_model=list[RoutineRunOut])
async def list_routine_runs(
    routine_id: UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[RoutineRunOut]:
    rows = await routines_service.list_runs(
        session, routine_id=routine_id, status_filter=status_filter, limit=limit
    )
    return [RoutineRunOut.model_validate(row) for row in rows]


@router.post("/runs/{run_id}/approve", response_model=RoutineRunOut)
async def approve_run(
    run_id: UUID,
    data: RoutineRunDecisionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineRunOut:
    try:
        run = await routines_service.approve_run(session, run_id, actor=actor, data=data)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return RoutineRunOut.model_validate(run)


@router.post("/runs/{run_id}/deny", response_model=RoutineRunOut)
async def deny_run(
    run_id: UUID,
    data: RoutineRunDecisionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineRunOut:
    try:
        run = await routines_service.deny_run(session, run_id, actor=actor, data=data)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return RoutineRunOut.model_validate(run)


@router.post("/runs/{run_id}/execute", response_model=RoutineRunOut)
async def execute_run(
    run_id: UUID,
    data: RoutineRunDecisionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineRunOut:
    try:
        run = await routines_service.execute_run(session, run_id, actor=actor, data=data)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return RoutineRunOut.model_validate(run)


@router.post("/runs/{run_id}/cancel", response_model=RoutineRunOut)
async def cancel_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineRunOut:
    try:
        run = await routines_service.cancel_run(session, run_id, actor=actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return RoutineRunOut.model_validate(run)


@router.post("/runs/{run_id}/retry", response_model=RoutineRunOut)
async def retry_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineRunOut:
    try:
        run = await routines_service.retry_run(session, run_id, actor=actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return RoutineRunOut.model_validate(run)


@router.post("/runs/{run_id}/rollback", response_model=RoutineRunOut)
async def rollback_run(
    run_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RoutineRunOut:
    try:
        run = await routines_service.rollback_run(session, run_id, actor=actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await session.commit()
    return RoutineRunOut.model_validate(run)
