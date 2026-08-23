"""Evie OS G1 — Core State API (projects / goals / commitments / mission control).

Typed backend surface shared by Realtime tools, DeepSeek manager, mobile, and
future agents. Auth uses the existing owner/device actor model — no parallel
auth.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext, require_actor_context
from app.db import get_session
from app.life import people
from app.life import service as life
from app.life.situation import changes_since_text, snapshot, summarize

router = APIRouter(prefix="/v1/life")


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    priority: str = "NORMAL"
    description: str = ""
    privacy_level: str = "normal"


class ProjectUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None
    title: str | None = None
    description: str | None = None
    expected_version: int | None = None


class GoalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    project: str | None = None  # id or title reference
    parent_goal_id: str | None = None
    priority: str = "NORMAL"
    success_criteria: str = ""
    privacy_level: str = "normal"


class GoalUpdate(BaseModel):
    state: str | None = None
    progress_note: str | None = None
    next_action: str | None = None
    blocked_reason: str | None = None
    priority: str | None = None
    title: str | None = None
    expected_version: int | None = None


class StepCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    position: int | None = None


class CommitmentCreate(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    due_at: datetime | None = None
    project: str | None = None
    goal_id: str | None = None
    privacy_level: str = "normal"


class CommitmentUpdate(BaseModel):
    status: str


@router.post("/projects")
async def create_project(
    body: ProjectCreate,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    result = await life.create_project(
        session,
        actor=ctx.actor,
        title=body.title,
        priority=body.priority,
        description=body.description,
        privacy_level=body.privacy_level,
        device_id=str(ctx.device_id) if ctx.device_id else None,
    )
    await session.commit()
    return result


@router.get("/projects")
async def list_projects(
    active_only: bool = True,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> list[dict]:
    return await life.list_projects(session, actor=ctx.actor, active_only=active_only)


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    result = await life.update_project(
        session, actor=ctx.actor, project_id=project_id,
        status=body.status, priority=body.priority,
        title=body.title, description=body.description,
        device_id=str(ctx.device_id) if ctx.device_id else None,
        expected_version=body.expected_version,
    )
    await session.commit()
    return result


@router.post("/goals")
async def create_goal(
    body: GoalCreate,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    result = await life.create_goal(
        session,
        actor=ctx.actor,
        title=body.title,
        project_ref=body.project,
        parent_goal_id=body.parent_goal_id,
        priority=body.priority,
        success_criteria=body.success_criteria,
        privacy_level=body.privacy_level,
        device_id=str(ctx.device_id) if ctx.device_id else None,
    )
    await session.commit()
    return result


@router.get("/goals")
async def list_goals(
    state: str | None = None,
    project_id: str | None = None,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> list[dict]:
    return await life.list_goals(session, actor=ctx.actor, state=state, project_id=project_id)


@router.get("/goals/{goal_id}")
async def get_goal(
    goal_id: str,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    return await life.get_goal(session, actor=ctx.actor, goal_id=goal_id)


@router.patch("/goals/{goal_id}")
async def update_goal(
    goal_id: str,
    body: GoalUpdate,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    result = await life.update_goal(
        session, actor=ctx.actor, goal_id=goal_id,
        state=body.state, progress_note=body.progress_note,
        next_action=body.next_action, blocked_reason=body.blocked_reason,
        priority=body.priority, title=body.title,
        device_id=str(ctx.device_id) if ctx.device_id else None,
        expected_version=body.expected_version,
    )
    await session.commit()
    return result


@router.post("/goals/{goal_id}/steps")
async def add_step(
    goal_id: str,
    body: StepCreate,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    result = await life.add_step(session, actor=ctx.actor, goal_id=goal_id, title=body.title)
    await session.commit()
    return result


@router.post("/commitments")
async def create_commitment(
    body: CommitmentCreate,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    result = await life.create_commitment(
        session,
        actor=ctx.actor,
        description=body.description,
        due_at=body.due_at,
        project_ref=body.project,
        goal_id=body.goal_id,
        privacy_level=body.privacy_level,
        device_id=str(ctx.device_id) if ctx.device_id else None,
    )
    await session.commit()
    return result


@router.get("/commitments")
async def list_commitments(
    q: str | None = Query(default=None, max_length=512),
    project: str | None = Query(default=None, max_length=256),
    status: str | None = Query(default=None),
    open_only: bool = True,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> list[dict]:
    commitments = await life.list_commitments(session, actor=ctx.actor, open_only=open_only and status is None)
    if status and status.upper() in ("OPEN", "FULFILLED", "CANCELLED", "MISSED"):
        commitments = [c for c in commitments if c["status"] == status.upper()]
    if project:
        proj = await life.find_project(session, actor=ctx.actor, query=project)
        if proj is not None:
            commitments = [c for c in commitments if c.get("project_id") == str(proj.id)]
    if q:
        q_lower = q.lower()
        commitments = [c for c in commitments if q_lower in c["description"].lower()]
    return commitments


@router.patch("/commitments/{commitment_id}")
async def update_commitment(
    commitment_id: str,
    body: CommitmentUpdate,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    result = await life.update_commitment(
        session,
        actor=ctx.actor,
        commitment_id=commitment_id,
        status=body.status,
        device_id=str(ctx.device_id) if ctx.device_id else None,
    )
    await session.commit()
    return result


@router.get("/situation")
async def situation(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    snap = await snapshot(session, actor=ctx.actor)
    return {"snapshot": snap, "summary": summarize(snap)}


@router.get("/changes")
async def changes(
    since: datetime | None = Query(default=None),
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    if since is None:
        since = await life.last_checkpoint(session, actor=ctx.actor)
    if since is None:
        since = datetime.now().astimezone() - timedelta(hours=24)
    events = await life.changes_since(session, actor=ctx.actor, since=since, limit=limit)
    return {
        "count": len(events),
        "since": since.isoformat(),
        "events": events,
        "summary": changes_since_text(events, since),
    }


class RelationshipSet(BaseModel):
    person: str = Field(min_length=1, max_length=256)
    relation: str
    note: str | None = Field(default=None, max_length=2000)
    privacy_level: str = "normal"


@router.get("/people/relationships")
async def get_relationships(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    return {"relationships": await people.list_relationships(session)}


@router.put("/people/relationships")
async def put_relationship(
    body: RelationshipSet,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    result = await people.set_relationship(
        session,
        actor=ctx.actor,
        person_name=body.person,
        relation=body.relation,
        note=body.note,
        privacy_level=body.privacy_level,
        device_id=str(ctx.device_id) if ctx.device_id else None,
    )
    if result.get("ok"):
        result["relationships"] = await people.list_relationships(session)
    await session.commit()
    return result


@router.post("/checkpoint")
async def checkpoint(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    """Advance the owner's Mission Control 'last checked' cursor to now."""
    at = await life.checkpoint(
        session, actor=ctx.actor,
        device_id=str(ctx.device_id) if ctx.device_id else None,
    )
    await session.commit()
    return {"ok": True, "checked_at": at.isoformat()}


@router.get("/checkpoint")
async def last_checkpoint(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    at = await life.last_checkpoint(session, actor=ctx.actor)
    return {"ok": True, "checked_at": at.isoformat() if at else None}
