"""Evie OS G1 core-state service.

Laws enforced here:
- ONE STATE AUTHORITY: Project/Goal/GoalStep/Commitment rows are canonical
  operational state. memory(type=goal) is recall, never authority.
- ONE DURABLE HISTORY: every state transition emits a canonical `events`
  row inside the same transaction (COMMAND → VALIDATE → STATE → EVENT).
- Model-agnostic: plain AsyncSession services, callable from Realtime tool
  dispatch, DeepSeek manager, background jobs, or REST — no layer owns them.

Event vocabulary: project.created / project.updated / project.priority_changed
/ project.paused / project.completed · goal.created / goal.activated /
goal.updated / goal.blocked / goal.unblocked / goal.progressed /
goal.completed / goal.cancelled · goal_step.created / goal_step.completed ·
commitment.created / commitment.fulfilled / commitment.cancelled /
commitment.missed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.everywhere.owner import owner_scope
from app.models import Commitment, DecisionOutcome, Event, Goal, GoalStep, Project
from app.utils.text import sha256_hex, utcnow

PRIORITIES = ("CRITICAL", "HIGH", "NORMAL", "LOW")
PRIORITY_RANK = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}
PROJECT_STATUSES = ("ACTIVE", "PAUSED", "COMPLETED", "ARCHIVED")
GOAL_STATES = ("PLANNED", "ACTIVE", "BLOCKED", "PAUSED", "COMPLETED", "CANCELLED")
COMMITMENT_STATUSES = ("OPEN", "FULFILLED", "CANCELLED", "MISSED")

SOURCE = "life"
EVENT_SOURCE = "life"
PRIVACY_DEFAULT = "normal"


def _sha(content: dict) -> str:
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, default=str).encode()
    ).hexdigest()


def _scope(actor: str | None) -> str:
    """G2 ONE-OWNER LAW: every caller maps to the canonical owner scope so
    device origin never fragments state. Sandbox callers stay isolated."""
    return owner_scope(actor)


async def _emit(
    session: AsyncSession,
    *,
    event_type: str,
    actor: str,
    content: dict,
    device_id: str | None = None,
    privacy_level: str = PRIVACY_DEFAULT,
    command_id: str | None = None,
    command_key: str | None = None,
) -> None:
    """Append one canonical durable domain event (same transaction).

    G2 ONE-EVIE idempotency law: when a cross-device command carries a
    ``command_id``, its hash is stored on the emitted event (unique column).
    Retries/replays of the same command resolve against this ledger and must
    produce exactly ONE canonical mutation. ``command_key`` lets multi-shape
    operations (one command, several possible event subtypes) pin ONE stable
    ledger identity.
    """
    key = command_key or (
        f"{event_type}:{command_id}" if command_id else None
    )
    session.add(
        Event(
            source=EVENT_SOURCE,
            event_type=event_type,
            content={"actor": actor, **content},
            device_id=device_id,
            privacy_level=privacy_level,
            sha256=_sha({"t": event_type, **content}),
            occurred_at=utcnow(),
            idempotency_key_hash=sha256_hex(key) if key else None,
        )
    )


async def _replay_by_command(
    session: AsyncSession, *, event_type: str | None = None, command_id: str | None = None, key: str | None = None
) -> Event | None:
    """Return the prior canonical event for a retried command, if any."""
    if key is None:
        if not command_id or not event_type:
            return None
        key = f"{event_type}:{command_id}"
    from sqlalchemy import select

    row = (
        await session.execute(
            select(Event).where(
                Event.idempotency_key_hash == sha256_hex(key),
                Event.tombstoned_at.is_(None),
            )
        )
    ).scalars().first()
    return row


def _public_project(row: Project) -> dict:
    return {
        "id": str(row.id),
        "title": row.title,
        "description": row.description,
        "status": row.status,
        "priority": row.priority,
        "privacy_level": row.privacy_level,
        "version": int(getattr(row, "version", 0) or 0),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _public_goal(row: Goal) -> dict:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id) if row.project_id else None,
        "parent_goal_id": str(row.parent_goal_id) if row.parent_goal_id else None,
        "title": row.title,
        "state": row.state,
        "priority": row.priority,
        "success_criteria": row.success_criteria,
        "progress_note": row.progress_note,
        "next_action": row.next_action,
        "blocked_reason": row.blocked_reason,
        "version": int(getattr(row, "version", 0) or 0),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "updated_at": row.updated_at.isoformat(),
    }


def _public_commitment(row: Commitment) -> dict:
    return {
        "id": str(row.id),
        "description": row.description,
        "status": row.status,
        "due_at": row.due_at.isoformat() if row.due_at else None,
        "project_id": str(row.project_id) if row.project_id else None,
        "goal_id": str(row.goal_id) if row.goal_id else None,
    }


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------


async def create_project(
    session: AsyncSession,
    *,
    actor: str,
    title: str,
    priority: str = "NORMAL",
    description: str = "",
    privacy_level: str = PRIVACY_DEFAULT,
    device_id: str | None = None,
    command_id: str | None = None,
) -> dict:
    origin_actor, actor = actor, _scope(actor)
    title = title.strip()
    priority = (priority or "NORMAL").upper()
    if not title:
        return {"ok": False, "error": "missing_title"}
    # G2 idempotency law: a retried command resolves to the original result.
    prior = await _replay_by_command(session, event_type="project.created", command_id=command_id)
    if prior is not None:
        prior_id = (prior.content or {}).get("project_id")
        row = await session.get(Project, UUID(prior_id)) if prior_id else None
        if row is not None:
            return {"ok": True, "project": _public_project(row), "duplicate": True}
    if priority not in PRIORITIES:
        priority = "NORMAL"
    row = Project(
        actor=actor,
        title=title,
        description=description or "",
        status="ACTIVE",
        priority=priority,
        privacy_level=privacy_level,
        source="owner",
    )
    session.add(row)
    await session.flush()
    await _emit(
        session,
        event_type="project.created",
        actor=origin_actor,
        content={
            "project_id": str(row.id),
            "title": title,
            "priority": priority,
        },
        device_id=device_id,
        privacy_level=privacy_level,
        command_id=command_id,
    )
    return {"ok": True, "project": _public_project(row), "spoken": f"Project {title} created."}


async def find_project(session: AsyncSession, *, actor: str, query: str) -> Project | None:
    actor = _scope(actor)
    q = (query or "").strip().lower()
    if not q:
        return None
    rows = (
        await session.execute(
            select(Project)
            .where(Project.actor == actor, Project.status.in_(("ACTIVE", "PAUSED")))
            .order_by(Project.updated_at.desc())
        )
    ).scalars().all()
    for row in rows:
        if q in row.title.lower():
            return row
    return None


async def get_project(session: AsyncSession, *, actor: str, project_id: str) -> dict:
    actor = _scope(actor)
    row = await session.get(Project, __import__("uuid").UUID(project_id))
    if row is None or row.actor != actor:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "project": _public_project(row)}


async def list_projects(session: AsyncSession, *, actor: str, active_only: bool = True) -> list[dict]:
    actor = _scope(actor)
    rows = (
        await session.execute(
            select(Project).where(Project.actor == actor).order_by(Project.updated_at.desc())
        )
    ).scalars().all()
    out = [_public_project(r) for r in rows if not active_only or r.status == "ACTIVE"]
    out.sort(key=lambda p: (PRIORITY_RANK.get(p["priority"], 2), p["updated_at"]), reverse=False)
    return out


async def update_project(
    session: AsyncSession,
    *,
    actor: str,
    project_id: str,
    status: str | None = None,
    priority: str | None = None,
    title: str | None = None,
    description: str | None = None,
    device_id: str | None = None,
    expected_version: int | None = None,
    command_id: str | None = None,
) -> dict:
    origin_actor, actor = actor, _scope(actor)
    row = await session.get(Project, UUID(project_id))
    if row is None or row.actor != actor:
        return {"ok": False, "error": "not_found"}
    # G2 idempotency law: a retried update command resolves to the original
    # canonical outcome (same event ledger as creates/cancels). The key is
    # subtype-independent because one update may emit different subtypes.
    prior = await _replay_by_command(
        session, key=f"project.update:{command_id}" if command_id else None
    )
    if prior is not None:
        return {
            "ok": True,
            "project": _public_project(row),
            "duplicate": True,
        }
    # G2 Phase 6 — version/conflict law. Never last-write-wins silently for
    # consequential edits; stale writers get the current canonical state back.
    if expected_version is not None and int(expected_version) != int(getattr(row, "version", 0) or 0):
        return {
            "ok": False,
            "error": "CONFLICT",
            "conflict": {
                "entity": "project",
                "expected_version": int(expected_version),
                "current_version": int(getattr(row, "version", 0) or 0),
                "current_state": _public_project(row),
            },
        }
    changes: dict[str, Any] = {}
    event_type = "project.updated"
    if status and status.upper() in PROJECT_STATUSES and status.upper() != row.status:
        row.status = status.upper()
        changes["status"] = row.status
        if row.status == "PAUSED":
            event_type = "project.paused"
        elif row.status == "COMPLETED":
            event_type = "project.completed"
        elif row.status == "ARCHIVED":
            row.archived_at = utcnow()
    if priority and priority.upper() in PRIORITIES and priority.upper() != row.priority:
        row.priority = priority.upper()
        changes["priority"] = row.priority
        event_type = "project.priority_changed"
    if title and title.strip():
        row.title = title.strip()
        changes["title"] = row.title
    if description is not None:
        row.description = description
        changes["description"] = True
    if not changes:
        return {"ok": True, "project": _public_project(row), "unchanged": True}
    await _emit(
        session,
        event_type=event_type,
        actor=origin_actor,
        content={"project_id": str(row.id), **changes},
        device_id=device_id,
        command_key=f"project.update:{command_id}" if command_id else None,
    )
    row.version = int(getattr(row, "version", 0) or 0) + 1
    return {"ok": True, "project": _public_project(row), "changes": changes}


# --------------------------------------------------------------------------
# Goals
# --------------------------------------------------------------------------


async def create_goal(
    session: AsyncSession,
    *,
    actor: str,
    title: str,
    project_ref: str | None = None,
    parent_goal_id: str | None = None,
    priority: str = "NORMAL",
    success_criteria: str = "",
    privacy_level: str = PRIVACY_DEFAULT,
    device_id: str | None = None,
    command_id: str | None = None,
) -> dict:
    origin_actor, actor = actor, _scope(actor)
    title = title.strip()
    if not title:
        return {"ok": False, "error": "missing_title"}
    prior = await _replay_by_command(session, event_type="goal.created", command_id=command_id)
    if prior is not None:
        prior_id = (prior.content or {}).get("goal_id")
        row0 = await session.get(Goal, UUID(prior_id)) if prior_id else None
        if row0 is not None:
            return {"ok": True, "goal": _public_goal(row0), "duplicate": True}
    project_id: UUID | None = None
    if project_ref:
        ref = project_ref.strip()
        try:
            pid = UUID(ref)
            proj = await session.get(Project, pid)
            project_id = pid if proj is not None else None
        except ValueError:
            proj = await find_project(session, actor=actor, query=ref)
            project_id = proj.id if proj is not None else None
        if project_id is None:
            # Auto-create the referenced project rather than dropping linkage.
            created = await create_project(session, actor=actor, title=ref)
            if created.get("ok"):
                project_id = UUID(created["project"]["id"])
    row = Goal(
        actor=actor,
        project_id=project_id,
        parent_goal_id=UUID(parent_goal_id) if parent_goal_id else None,
        title=title,
        state="ACTIVE",
        priority=(priority or "NORMAL").upper(),
        success_criteria=success_criteria or "",
        started_at=utcnow(),
        privacy_level=privacy_level,
        source="owner",
    )
    session.add(row)
    await session.flush()
    await _emit(
        session,
        event_type="goal.created",
        actor=origin_actor,
        content={
            "goal_id": str(row.id),
            "title": title,
            "project_id": str(project_id) if project_id else None,
        },
        device_id=device_id,
        privacy_level=privacy_level,
        command_id=command_id,
    )
    return {"ok": True, "goal": _public_goal(row)}


async def get_goal(session: AsyncSession, *, actor: str, goal_id: str) -> dict:
    actor = _scope(actor)
    row = await session.get(Goal, UUID(goal_id))
    if row is None or row.actor != actor:
        return {"ok": False, "error": "not_found"}
    steps = (
        await session.execute(
            select(GoalStep)
            .where(GoalStep.goal_id == row.id)
            .order_by(GoalStep.position, GoalStep.created_at)
        )
    ).scalars().all()
    out = _public_goal(row)
    out["steps"] = [
        {
            "id": str(s.id),
            "title": s.title,
            "status": s.status,
            "position": s.position,
        }
        for s in steps
    ]
    return {"ok": True, "goal": out}


async def list_goals(
    session: AsyncSession, *, actor: str, state: str | None = None, project_id: str | None = None
) -> list[dict]:
    actor = _scope(actor)
    stmt = select(Goal).where(Goal.actor == actor).order_by(Goal.updated_at.desc())
    if state:
        stmt = stmt.where(Goal.state == state.upper())
    if project_id:
        stmt = stmt.where(Goal.project_id == _uuid(project_id))
    rows = (await session.execute(stmt)).scalars().all()
    return [_public_goal(r) for r in rows]


def _uuid(value: str) -> UUID:
    return UUID(value)


async def update_goal(
    session: AsyncSession,
    *,
    actor: str,
    goal_id: str,
    state: str | None = None,
    progress_note: str | None = None,
    next_action: str | None = None,
    blocked_reason: str | None = None,
    priority: str | None = None,
    title: str | None = None,
    device_id: str | None = None,
    expected_version: int | None = None,
    command_id: str | None = None,
) -> dict:
    origin_actor, actor = actor, _scope(actor)
    row = await session.get(Goal, _uuid(goal_id))
    if row is None or row.actor != actor:
        return {"ok": False, "error": "not_found"}
    # G2 idempotency law (subtype-independent ledger key — one update may
    # emit updated/blocked/completed/... subtypes).
    prior = await _replay_by_command(
        session, key=f"goal.update:{command_id}" if command_id else None
    )
    if prior is not None:
        return {"ok": True, "goal": _public_goal(row), "duplicate": True}
    # G2 Phase 6 — version/conflict law (see update_project).
    if expected_version is not None and int(expected_version) != int(getattr(row, "version", 0) or 0):
        return {
            "ok": False,
            "error": "CONFLICT",
            "conflict": {
                "entity": "goal",
                "expected_version": int(expected_version),
                "current_version": int(getattr(row, "version", 0) or 0),
                "current_state": _public_goal(row),
            },
        }
    changes: dict[str, Any] = {}
    event_type = "goal.updated"
    if state and state.upper() in GOAL_STATES and state.upper() != row.state:
        prev = row.state
        row.state = new = state.upper()
        changes["state"] = new
        if new == "BLOCKED":
            event_type = "goal.blocked"
            row.blocked_reason = blocked_reason or row.blocked_reason or "unspecified"
        elif prev == "BLOCKED" and new == "ACTIVE":
            event_type = "goal.unblocked"
            row.blocked_reason = None
        elif new == "COMPLETED":
            event_type = "goal.completed"
            row.completed_at = utcnow()
        elif new == "CANCELLED":
            event_type = "goal.cancelled"
        elif new == "ACTIVE" and prev in ("PLANNED", "PAUSED"):
            event_type = "goal.activated"
            if row.started_at is None:
                row.started_at = utcnow()
    if progress_note is not None and progress_note != row.progress_note:
        row.progress_note = progress_note
        changes["progress"] = True
        if event_type == "goal.updated":
            event_type = "goal.progressed"
    if next_action is not None:
        row.next_action = next_action
        changes["next_action"] = True
    if priority and priority.upper() in PRIORITIES:
        row.priority = priority.upper()
        changes["priority"] = row.priority
    if title and title.strip():
        row.title = title.strip()
        changes["title"] = row.title
    if blocked_reason is not None:
        row.blocked_reason = blocked_reason
    if not changes:
        return {"ok": True, "goal": _public_goal(row), "unchanged": True}
    await _emit(
        session,
        event_type=event_type,
        actor=origin_actor,
        content={"goal_id": str(row.id), **changes},
        device_id=device_id,
        command_key=f"goal.update:{command_id}" if command_id else None,
    )
    row.version = int(getattr(row, "version", 0) or 0) + 1
    return {"ok": True, "goal": _public_goal(row), "changes": changes}


# --------------------------------------------------------------------------
# Goal steps
# --------------------------------------------------------------------------


async def add_step(
    session: AsyncSession, *, actor: str, goal_id: str, title: str, position: int | None = None
) -> dict:
    origin_actor, actor = actor, _scope(actor)
    goal = await session.get(Goal, _uuid(goal_id))
    if goal is None or goal.actor != actor:
        return {"ok": False, "error": "goal_not_found"}
    count = (
        await session.execute(
            select(func.count()).select_from(GoalStep).where(GoalStep.goal_id == goal.id)
        )
    ).scalar_one()
    row = GoalStep(
        goal_id=goal.id,
        title=title.strip(),
        position=int(position if position is not None else count),
    )
    session.add(row)
    await session.flush()
    await _emit(
        session,
        event_type="goal_step.created",
        actor=origin_actor,
        content={"step_id": str(row.id), "goal_id": str(goal.id), "title": row.title},
    )
    return {"ok": True, "step": {"id": str(row.id), "title": row.title, "status": row.status}}


async def complete_step(session: AsyncSession, *, actor: str, step_id: str) -> dict:
    origin_actor, actor = actor, _scope(actor)
    row = await session.get(GoalStep, _uuid(step_id))
    if row is None:
        return {"ok": False, "error": "not_found"}
    if row.status == "DONE":
        # Idempotent completion: a re-complete is a safe no-op, never a
        # duplicate canonical state-transition event.
        return {"ok": True, "unchanged": True}
    row.status = "DONE"
    await _emit(
        session,
        event_type="goal_step.completed",
        actor=origin_actor,
        content={"step_id": str(row.id), "goal_id": str(row.goal_id)},
    )
    return {"ok": True}


# --------------------------------------------------------------------------
# Commitments
# --------------------------------------------------------------------------


async def create_commitment(
    session: AsyncSession,
    *,
    actor: str,
    description: str,
    due_at: datetime | None = None,
    project_ref: str | None = None,
    goal_id: str | None = None,
    privacy_level: str = PRIVACY_DEFAULT,
    source_event_id=None,
    device_id: str | None = None,
    command_id: str | None = None,
) -> dict:
    origin_actor, actor = actor, _scope(actor)
    description = (description or "").strip()
    if not description:
        return {"ok": False, "error": "missing_description"}
    prior = await _replay_by_command(
        session, event_type="commitment.created", command_id=command_id
    )
    if prior is not None:
        prior_id = (prior.content or {}).get("commitment_id")
        row0 = await session.get(Commitment, _uuid(prior_id)) if prior_id else None
        if row0 is not None:
            return {"ok": True, "commitment": _public_commitment(row0), "duplicate": True}
    project_id = None
    if project_ref:
        proj = await find_project(session, actor=actor, query=project_ref)
        project_id = proj.id if proj else None
    row = Commitment(
        actor=actor,
        description=description,
        status="OPEN",
        due_at=due_at,
        project_id=project_id,
        goal_id=_uuid(goal_id) if goal_id else None,
        source_event_id=source_event_id,
        privacy_level=privacy_level,
    )
    session.add(row)
    await session.flush()
    await _emit(
        session,
        event_type="commitment.created",
        actor=origin_actor,
        content={
            "commitment_id": str(row.id),
            "description": description,
            "due_at": due_at.isoformat() if due_at else None,
        },
        device_id=device_id,
        privacy_level=privacy_level,
        command_id=command_id,
    )
    return {"ok": True, "commitment": _public_commitment(row)}


async def update_commitment(
    session: AsyncSession,
    *,
    actor: str,
    commitment_id: str,
    status: str,
    device_id: str | None = None,
    command_id: str | None = None,
) -> dict:
    origin_actor, actor = actor, _scope(actor)
    row = await session.get(Commitment, _uuid(commitment_id))
    if row is None or row.actor != actor:
        return {"ok": False, "error": "not_found"}
    status = status.upper()
    if status not in COMMITMENT_STATUSES:
        return {"ok": False, "error": "bad_status"}
    event_type = f"commitment.{status.lower()}"
    # G2 idempotency law: a retried cancel/complete command resolves to the
    # original canonical outcome even from a different device.
    prior = await _replay_by_command(session, event_type=event_type, command_id=command_id)
    if prior is not None:
        return {"ok": True, "commitment": _public_commitment(row), "duplicate": True}
    if row.status == status:
        # G2 Phase 6 idempotency law: re-complete / re-cancel is a safe no-op,
        # never a duplicate canonical transition.
        return {"ok": True, "commitment": _public_commitment(row), "unchanged": True}
    row.status = status
    if status == "FULFILLED":
        row.fulfilled_at = utcnow()
    await _emit(
        session,
        event_type=event_type,
        actor=origin_actor,
        content={"commitment_id": str(row.id), "description": row.description},
        device_id=device_id,
        command_id=command_id,
    )
    return {"ok": True, "commitment": _public_commitment(row)}


def _aware(value: datetime) -> datetime:
    """Normalize DB-read timestamps (SQLite drops tzinfo) to aware UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def list_commitments(
    session: AsyncSession, *, actor: str, open_only: bool = True
) -> list[dict]:
    actor = _scope(actor)
    stmt = select(Commitment).where(Commitment.actor == actor).order_by(Commitment.due_at.asc())
    rows = (await session.execute(stmt)).scalars().all()
    out = []
    now = utcnow()
    for r in rows:
        if open_only and r.status != "OPEN":
            continue
        item = _public_commitment(r)
        item["overdue"] = bool(
            r.status == "OPEN" and r.due_at and _aware(r.due_at) < now
        )
        out.append(item)
    return out


# --------------------------------------------------------------------------
# Mission Control v0.1 + What Changed
# --------------------------------------------------------------------------

G1_EVENT_PREFIXES = (
    "project.",
    "goal.",
    "goal_step.",
    "commitment.",
)

# Bookkeeping/checkpoint events are canonical history but NEVER owner-facing
# semantic changes. "What changed?" must not report our own cursor writes.
BOOKKEEPING_EVENT_TYPES = frozenset({"mission_control.checked"})


async def situation_snapshot(session: AsyncSession, *, actor: str) -> dict:
    """Derived Situation Model v0.1. A view — NEVER authoritative truth."""
    actor = _scope(actor)
    projects = await list_projects(session, actor=actor, active_only=True)
    goals_active = await list_goals(session, actor=actor, state="ACTIVE")
    goals_blocked = await list_goals(session, actor=actor, state="BLOCKED")
    commitments = await list_commitments(session, actor=actor, open_only=True)
    overdue = [c for c in commitments if c.get("overdue")]
    since = utcnow() - timedelta(hours=24)
    ev_rows = (
        await session.execute(
            select(Event)
            .where(
                Event.source == EVENT_SOURCE,
                Event.occurred_at >= since,
                Event.event_type.not_in(BOOKKEEPING_EVENT_TYPES),
            )
            .order_by(Event.occurred_at.desc())
            .limit(25)
        )
    ).scalars().all()
    recent_changes = [
        {"type": e.event_type, "at": e.occurred_at.isoformat(), "content": e.content}
        for e in ev_rows
    ]
    top = projects[0] if projects else None
    from sqlalchemy import func

    pending_decisions = (
        await session.execute(
            select(func.count())
            .select_from(DecisionOutcome)
            .where(DecisionOutcome.status == "pending")
        )
    ).scalar_one()
    return {
        "top_focus": top,
        "active_projects": projects,
        "active_goals": [g for g in goals_active if g["state"] == "ACTIVE"],
        "blocked_goals": goals_blocked,
        "open_commitments": commitments,
        "overdue_commitments": overdue,
        "recent_changes": recent_changes[:10],
        "pending_decisions": pending_decisions,
        "capability_issues": [],
        "system_health": {"core": "READY"},
    }


async def changes_since(
    session: AsyncSession, *, actor: str, since: datetime, limit: int = 50
) -> list[dict]:
    """Owner-facing semantic changes since a point in time.

    Bookkeeping/checkpoint events (see BOOKKEEPING_EVENT_TYPES) stay in the
    canonical history but are excluded here — the checkpoint cursor must never
    answer its own question.
    """
    actor = _scope(actor)
    rows = (
        await session.execute(
            select(Event)
            .where(
                Event.source == EVENT_SOURCE,
                Event.occurred_at >= since,
                Event.event_type.not_in(BOOKKEEPING_EVENT_TYPES),
            )
            .order_by(Event.occurred_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "type": e.event_type,
            "at": e.occurred_at.isoformat(),
            "content": e.content,
        }
        for e in rows
    ]


async def last_checkpoint(session: AsyncSession, *, actor: str) -> datetime | None:
    """Owner's 'last checked' cursor for Mission Control, event-sourced.

    Stored as a canonical `mission_control.checked` event — no second history
    store, and 'when did I last look' is itself part of the durable record.
    """
    # Checkpoints are owner-level cursor state over the canonical life
    # history; situation_snapshot/changes_since read the same source-scoped
    # history without per-actor filtering (single-owner v0.1).
    rows = (
        await session.execute(
            select(Event)
            .where(
                Event.source == EVENT_SOURCE,
                Event.event_type == "mission_control.checked",
            )
            .order_by(Event.occurred_at.desc())
            .limit(5)
        )
    ).scalars().all()
    if not rows or rows[0].occurred_at is None:
        return None
    return _aware(rows[0].occurred_at)


async def checkpoint(
    session: AsyncSession, *, actor: str, device_id: str | None = None
) -> datetime:
    """Advance the owner's Mission Control cursor to now (canonical event)."""
    at = utcnow()
    await _emit(
        session,
        event_type="mission_control.checked",
        actor=actor,
        content={"checked_at": at.isoformat()},
        device_id=device_id,
    )
    return at
