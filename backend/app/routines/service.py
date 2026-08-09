"""Routines & automations engine.

Owns deterministic scheduling (cron), trigger matching over events and live
data, duplicate prevention via unique run keys, run history/failure states,
approval handoff to the existing runtime action layer, one-tap disable, and
undo/rollback records.  Permission authority stays in
``runtime.ACTION_PERMISSIONS``; a routine can only require *more* approval.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.ev_sense import quiet_hours_active
from app.models import Alert, Event, LiveChannel, LiveEvent, Routine, RoutineRun
from app.routines.schedule import next_run_after, validate_cron
from app.routines.templates import get_template
from app.schemas import (
    ApprovedActionCreate,
    RoutineCreate,
    RoutineOverviewOut,
    RoutineRunDecisionRequest,
    RoutineRunOut,
    RoutineTickOut,
    RoutineUpdate,
)
from app.services import runtime as runtime_service
from app.services.access_log import log_access
from app.utils.text import sha256_hex, utcnow

TRIGGER_OPS = {"eq", "ne", "lt", "lte", "gt", "gte", "contains", "in", "exists"}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=utcnow().tzinfo)


def _validate_trigger_spec(spec: dict) -> None:
    if not isinstance(spec, dict):
        raise ValueError("Trigger spec must be an object")
    for key in ("event_type", "source", "channel_kind"):
        if key in spec and not isinstance(spec[key], (str, list)):
            raise ValueError(f"Trigger field {key!r} must be a string or list")
    for condition in spec.get("conditions") or []:
        if not isinstance(condition, dict) or not isinstance(condition.get("path"), str):
            raise ValueError("Each trigger condition needs a string 'path'")
        op = condition.get("op", "eq")
        if op not in TRIGGER_OPS:
            raise ValueError(f"Unsupported trigger condition op {op!r}")


def _validate_routine(
    *,
    kind: str,
    schedule: str | None,
    trigger: dict,
) -> None:
    if kind == "scheduled":
        if not schedule:
            raise ValueError("Scheduled routines require a 5-field cron 'schedule'")
        validate_cron(schedule)
    elif kind == "trigger":
        if not trigger:
            raise ValueError("Trigger routines require a 'trigger' spec")
        _validate_trigger_spec(trigger)


def _requires_approval(routine: Routine) -> bool:
    return bool(
        routine.requires_approval
        or runtime_service.ACTION_PERMISSIONS.get(routine.action_type, True)
    )


def _new_run(
    routine: Routine,
    *,
    dedupe_key: str,
    status: str,
    scheduled_for: datetime | None = None,
    triggered_at: datetime | None = None,
    trigger_event_id=None,
    trigger_live_event_id=None,
    trigger_snapshot: dict | None = None,
) -> RoutineRun:
    return RoutineRun(
        routine_id=routine.id,
        kind=routine.kind,
        status=status,
        scheduled_for=scheduled_for,
        triggered_at=triggered_at,
        trigger_event_id=trigger_event_id,
        trigger_live_event_id=trigger_live_event_id,
        trigger_snapshot=trigger_snapshot or {},
        dedupe_key=dedupe_key,
        undoable=routine.undoable,
        started_at=utcnow(),
    )


async def _insert_run(session: AsyncSession, run: RoutineRun) -> RoutineRun | None:
    """Insert with a savepoint; a unique-key conflict returns None (duplicate)."""
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
        return run
    except IntegrityError:
        return None


async def _route_action_for_run(
    session: AsyncSession,
    run: RoutineRun,
    routine: Routine,
    *,
    actor: str,
) -> RoutineRun:
    """Route the routine's action through the runtime permission layer.

    Sensitive actions stop at ``awaiting_approval``; non-sensitive actions are
    executed immediately and recorded, so nothing acts silently.
    """
    action = await runtime_service.route_action(
        session,
        ApprovedActionCreate(
            action_type=routine.action_type,
            title=routine.action_title or routine.name,
            payload=routine.action_payload,
            auto_approve=True,
        ),
        requested_by=actor,
        force_requires_approval=_requires_approval(routine),
    )
    run.action_id = action.id
    if action.requires_approval:
        run.status = "awaiting_approval"
    else:
        action = await runtime_service.execute_action(
            session,
            action.id,
            actor="automation",
            result={
                "dispatched": True,
                "routine_id": str(routine.id),
                "action_type": routine.action_type,
                "payload": routine.action_payload,
            },
        )
        run.status = "executed"
        run.result = action.result
        run.finished_at = utcnow()
    await session.flush()
    return run


async def create_routine(
    session: AsyncSession, data: RoutineCreate, *, actor: str = "user"
) -> Routine:
    _validate_routine(
        kind=data.kind,
        schedule=data.schedule,
        trigger=data.trigger or {},
    )
    routine = Routine(
        name=data.name,
        kind=data.kind,
        schedule=data.schedule if data.kind == "scheduled" else None,
        timezone=data.timezone,
        quiet_hours_skip=data.quiet_hours_skip,
        backfill_max=data.backfill_max,
        cooldown_seconds=data.cooldown_seconds,
        trigger=data.trigger if data.kind == "trigger" else {},
        action_type=data.action_type,
        action_title=data.action_title,
        action_payload=data.action_payload,
        requires_approval=data.requires_approval,
        undoable=data.undoable,
        metadata_=data.metadata,
    )
    if data.kind == "scheduled":
        routine.next_run_at = next_run_after(
            data.schedule or "", utcnow(), timezone=data.timezone
        )
    session.add(routine)
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="write",
        endpoint="POST /v1/routines",
        resource_type="routine",
        resource_ids=[routine.id],
        details={"kind": routine.kind, "name": routine.name},
    )
    return routine


async def get_routine(session: AsyncSession, routine_id) -> Routine:
    routine = await session.get(Routine, routine_id)
    if routine is None:
        raise KeyError(f"Routine {routine_id} not found")
    return routine


async def list_routines(
    session: AsyncSession,
    *,
    kind: str | None = None,
    enabled: bool | None = None,
) -> list[Routine]:
    stmt = select(Routine).order_by(Routine.created_at.asc())
    if kind:
        stmt = stmt.where(Routine.kind == kind)
    if enabled is not None:
        stmt = stmt.where(Routine.enabled.is_(enabled))
    rows = await session.execute(stmt)
    return list(rows.scalars().all())


async def update_routine(
    session: AsyncSession,
    routine_id,
    data: RoutineUpdate,
    *,
    actor: str = "user",
) -> Routine:
    routine = await get_routine(session, routine_id)
    changes = data.model_dump(exclude_unset=True)
    next_kind = changes.get("kind", routine.kind)
    _validate_routine(
        kind=next_kind,
        schedule=changes.get("schedule", routine.schedule),
        trigger=changes.get("trigger", routine.trigger or {}),
    )
    for field, value in changes.items():
        setattr(routine, field, value)
    if changes.get("kind") == "scheduled" or (
        routine.kind == "scheduled" and ("schedule" in changes or "timezone" in changes)
    ):
        routine.next_run_at = next_run_after(
            routine.schedule or "", utcnow(), timezone=routine.timezone
        )
    if routine.kind == "trigger":
        routine.schedule = None
        routine.next_run_at = None
    elif routine.kind == "scheduled":
        routine.trigger = {}
    routine.updated_at = utcnow()
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="write",
        endpoint="PATCH /v1/routines/{id}",
        resource_type="routine",
        resource_ids=[routine.id],
        details={"changed": sorted(changes)},
    )
    return routine


async def set_enabled(
    session: AsyncSession, routine_id, enabled: bool, *, actor: str = "user"
) -> Routine:
    routine = await get_routine(session, routine_id)
    routine.enabled = enabled
    if enabled and routine.kind == "scheduled" and routine.next_run_at is None:
        routine.next_run_at = next_run_after(
            routine.schedule or "", utcnow(), timezone=routine.timezone
        )
    routine.updated_at = utcnow()
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="write",
        endpoint="POST /v1/routines/{id}/enable" if enabled else "POST /v1/routines/{id}/disable",
        resource_type="routine",
        resource_ids=[routine.id],
        details={"enabled": enabled},
    )
    return routine


async def _scheduled_occurrence_key(routine: Routine, occurrence: datetime) -> str:
    return f"sched:{routine.id}:{occurrence.isoformat()}"


async def tick(
    session: AsyncSession, *, now: datetime | None = None
) -> RoutineTickOut:
    """Advance due scheduled routines.

    Each tick processes at most ``1 + backfill_max`` due occurrences per
    routine, in order; anything still due is picked up on a later tick, so
    missed work is recovered instead of silently dropped.
    """
    now = now or utcnow()
    outcome = RoutineTickOut(now=now)
    rows = (
        await session.execute(
            select(Routine).where(
                Routine.kind == "scheduled",
                Routine.enabled.is_(True),
                Routine.next_run_at.is_not(None),
                Routine.next_run_at <= now,
            )
        )
    ).scalars().all()

    for routine in rows:
        processed = 0
        cap = 1 + (routine.backfill_max or 0)
        while (
            routine.next_run_at is not None
            and routine.next_run_at <= now
            and processed < cap
        ):
            occurrence = routine.next_run_at
            processed += 1
            dedupe_key = await _scheduled_occurrence_key(routine, occurrence)
            run: RoutineRun | None
            if routine.quiet_hours_skip and quiet_hours_active(occurrence):
                run = await _insert_run(
                    session,
                    _new_run(
                        routine,
                        dedupe_key=dedupe_key,
                        status="skipped",
                        scheduled_for=occurrence,
                        trigger_snapshot={"reason": "quiet_hours"},
                    ),
                )
                if run is not None:
                    run.finished_at = utcnow()
                    outcome.skipped += 1
            else:
                run = await _insert_run(
                    session,
                    _new_run(
                        routine,
                        dedupe_key=dedupe_key,
                        status="queued",
                        scheduled_for=occurrence,
                    ),
                )
                if run is not None:
                    try:
                        await _route_action_for_run(
                            session, run, routine, actor=f"automation:{routine.name}"
                        )
                        outcome.created += 1
                    except Exception as exc:  # noqa: BLE001 - run-level failure isolation
                        run.status = "failed"
                        run.error = f"{type(exc).__name__}: {exc}"
                        run.finished_at = utcnow()
                        outcome.failed += 1
                        outcome.errors.append(
                            f"routine={routine.id} occurrence={occurrence.isoformat()}: "
                            f"{type(exc).__name__}: {exc}"
                        )
                else:
                    run = None
            if run is not None:
                outcome.runs.append(RoutineRunOut.model_validate(run))
                routine.last_run_at = occurrence
                routine.last_run_status = run.status
            routine.next_run_at = next_run_after(
                routine.schedule or "",
                occurrence,
                timezone=routine.timezone,
            )
            if routine.next_run_at is None:
                break
    outcome.failure_alerts = len(await detect_repeated_failures(session, now=now))
    await session.flush()
    return outcome


def _value_matches(expected: str | list[str], actual: str | None) -> bool:
    if actual is None:
        return False
    if isinstance(expected, list):
        return actual in expected
    return actual == expected


def _resolve_path(data: dict, path: str):
    current: object = data
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        else:
            return None
    return current


def _apply_op(op: str, actual, expected) -> bool:
    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "lt":
            return actual is not None and actual < expected
        if op == "lte":
            return actual is not None and actual <= expected
        if op == "gt":
            return actual is not None and actual > expected
        if op == "gte":
            return actual is not None and actual >= expected
        if op == "contains":
            return actual is not None and expected in actual
        if op == "in":
            return expected is not None and actual in expected
        if op == "exists":
            return (actual is not None) == bool(expected)
    except TypeError:
        return False
    raise ValueError(f"Unsupported trigger condition op {op!r}")


def _match_trigger(
    spec: dict,
    *,
    event: Event | None = None,
    live_event: LiveEvent | None = None,
    channel: LiveChannel | None = None,
) -> bool:
    if event is None and live_event is None:
        return False
    if isinstance(event, Event):
        event_type = event.event_type
        source: str | None = event.source
        data = event.content or {}
        channel_kind = None
    else:
        assert live_event is not None
        event_type = live_event.event_type
        source = live_event.collector or (
            channel.name if channel is not None else None
        )
        data = live_event.payload or {}
        channel_kind = channel.kind if channel else None
    if "event_type" in spec and not _value_matches(spec["event_type"], event_type):
        return False
    if "source" in spec and not _value_matches(spec["source"], source):
        return False
    if "channel_kind" in spec and not _value_matches(spec["channel_kind"], channel_kind):
        return False
    for condition in spec.get("conditions") or []:
        actual = _resolve_path(data, condition["path"])
        if not _apply_op(condition.get("op", "eq"), actual, condition.get("value")):
            return False
    return True


async def _last_run(session: AsyncSession, routine_id) -> RoutineRun | None:
    rows = (
        await session.execute(
            select(RoutineRun)
            .where(RoutineRun.routine_id == routine_id)
            .order_by(RoutineRun.created_at.desc())
            .limit(1)
        )
    ).scalars().all()
    return rows[0] if rows else None


async def consider_event(
    session: AsyncSession,
    *,
    event: Event | None = None,
    live_event: LiveEvent | None = None,
    channel: LiveChannel | None = None,
) -> list[RoutineRun]:
    """Evaluate enabled trigger routines against one event/live event."""
    if event is None and live_event is None:
        return []
    if event is not None and live_event is not None:
        raise ValueError("Pass exactly one of event or live_event")
    rows = (
        await session.execute(
            select(Routine).where(
                Routine.kind == "trigger",
                Routine.enabled.is_(True),
            )
        )
    ).scalars().all()
    matched: list[RoutineRun] = []
    for routine in rows:
        if not _match_trigger(routine.trigger, event=event, live_event=live_event, channel=channel):
            continue
        if routine.cooldown_seconds > 0:
            previous = await _last_run(session, routine.id)
            if previous is not None and (utcnow() - previous.created_at).total_seconds() < routine.cooldown_seconds:
                continue
        snapshot_source: str | None
        if isinstance(event, Event):
            digest = event.sha256
            snapshot_source = event.source
            snapshot_payload = event.content or {}
            source_id = event.id
            source_type = event.event_type
        else:
            assert live_event is not None
            digest = live_event.sha256
            snapshot_source = live_event.collector or (
                channel.name if channel is not None else None
            )
            snapshot_payload = live_event.payload or {}
            source_id = live_event.id
            source_type = live_event.event_type
        dedupe_key = f"trig:{routine.id}:{source_id}:{digest}"
        run = await _insert_run(
            session,
            _new_run(
                routine,
                dedupe_key=dedupe_key,
                status="queued",
                triggered_at=utcnow(),
                trigger_event_id=event.id if event is not None else None,
                trigger_live_event_id=live_event.id if live_event is not None else None,
                trigger_snapshot={
                    "event_type": source_type,
                    "source": snapshot_source,
                    "channel_kind": channel.kind if channel is not None else None,
                    "payload": snapshot_payload,
                },
            ),
        )
        if run is None:
            continue
        try:
            await _route_action_for_run(
                session, run, routine, actor=f"automation:{routine.name}"
            )
        except Exception as exc:  # noqa: BLE001 - run-level failure isolation
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            run.finished_at = utcnow()
        routine.last_run_at = utcnow()
        routine.last_run_status = run.status
        matched.append(run)
    await session.flush()
    return matched


async def manual_run(
    session: AsyncSession,
    routine_id,
    *,
    actor: str = "user",
    reason: str | None = None,
) -> RoutineRun:
    routine = await get_routine(session, routine_id)
    run = _new_run(
        routine,
        dedupe_key=f"manual:{routine.id}:{uuid4()}",
        status="queued",
        triggered_at=utcnow(),
        trigger_snapshot={"reason": reason or "manual_run", "actor": actor},
    )
    session.add(run)
    await session.flush()
    try:
        await _route_action_for_run(
            session, run, routine, actor=f"manual:{actor}"
        )
    except Exception as exc:  # noqa: BLE001 - run-level failure isolation
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
        run.finished_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="write",
        endpoint="POST /v1/routines/{id}/run",
        resource_type="routine_run",
        resource_ids=[run.id],
        details={"routine_id": str(routine.id), "reason": reason},
    )
    await session.flush()
    return run


async def list_runs(
    session: AsyncSession,
    *,
    routine_id=None,
    status_filter: str | None = None,
    limit: int = 50,
) -> list[RoutineRun]:
    stmt = select(RoutineRun).order_by(RoutineRun.created_at.desc()).limit(limit)
    if routine_id is not None:
        stmt = stmt.where(RoutineRun.routine_id == routine_id)
    if status_filter:
        stmt = stmt.where(RoutineRun.status == status_filter)
    rows = await session.execute(stmt)
    return list(rows.scalars().all())


async def get_run(session: AsyncSession, run_id) -> RoutineRun:
    run = await session.get(RoutineRun, run_id)
    if run is None:
        raise KeyError(f"Routine run {run_id} not found")
    return run


async def approve_run(
    session: AsyncSession,
    run_id,
    *,
    actor: str,
    data: RoutineRunDecisionRequest | None = None,
) -> RoutineRun:
    run = await get_run(session, run_id)
    if run.status != "awaiting_approval" or run.action_id is None:
        raise ValueError("Only runs awaiting approval can be approved")
    await runtime_service.decide_action(
        session,
        run.action_id,
        actor=actor,
        decision="approve",
        reason=data.reason if data else None,
    )
    run.status = "approved"
    run.updated_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="approve",
        endpoint="POST /v1/routines/runs/{id}/approve",
        resource_type="routine_run",
        resource_ids=[run.id],
    )
    await session.flush()
    return run


async def deny_run(
    session: AsyncSession,
    run_id,
    *,
    actor: str,
    data: RoutineRunDecisionRequest | None = None,
) -> RoutineRun:
    run = await get_run(session, run_id)
    if run.status != "awaiting_approval" or run.action_id is None:
        raise ValueError("Only runs awaiting approval can be denied")
    await runtime_service.decide_action(
        session,
        run.action_id,
        actor=actor,
        decision="deny",
        reason=data.reason if data else None,
    )
    run.status = "denied"
    run.error = data.reason if data and data.reason else "denied by user"
    run.finished_at = utcnow()
    run.updated_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="deny",
        endpoint="POST /v1/routines/runs/{id}/deny",
        resource_type="routine_run",
        resource_ids=[run.id],
    )
    await session.flush()
    return run


async def execute_run(
    session: AsyncSession,
    run_id,
    *,
    actor: str,
    data: RoutineRunDecisionRequest | None = None,
) -> RoutineRun:
    run = await get_run(session, run_id)
    if run.status != "approved" or run.action_id is None:
        raise ValueError("Only approved runs can be executed")
    action = await runtime_service.execute_action(
        session,
        run.action_id,
        actor=actor,
        result=data.result if data and data.result else {},
    )
    run.status = "executed"
    run.result = action.result
    run.finished_at = utcnow()
    run.updated_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="execute",
        endpoint="POST /v1/routines/runs/{id}/execute",
        resource_type="routine_run",
        resource_ids=[run.id],
    )
    await session.flush()
    return run


async def cancel_run(session: AsyncSession, run_id, *, actor: str) -> RoutineRun:
    run = await get_run(session, run_id)
    if run.status not in ("queued", "awaiting_approval", "approved"):
        raise ValueError(f"Runs in status {run.status!r} cannot be cancelled")
    run.status = "cancelled"
    run.error = "cancelled by user"
    run.finished_at = utcnow()
    run.updated_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="cancel",
        endpoint="POST /v1/routines/runs/{id}/cancel",
        resource_type="routine_run",
        resource_ids=[run.id],
    )
    await session.flush()
    return run


async def retry_run(
    session: AsyncSession, run_id, *, actor: str = "user"
) -> RoutineRun:
    run = await get_run(session, run_id)
    if run.status not in ("failed", "denied", "cancelled"):
        raise ValueError(f"Runs in status {run.status!r} cannot be retried")
    routine = await get_routine(session, run.routine_id)
    run.status = "queued"
    run.error = None
    run.result = None
    run.attempts += 1
    run.undo_status = "none"
    run.undo_payload = None
    run.rolled_back_at = None
    run.finished_at = None
    run.started_at = utcnow()
    run.updated_at = utcnow()
    try:
        await _route_action_for_run(session, run, routine, actor=f"retry:{actor}")
    except Exception as exc:  # noqa: BLE001 - run-level failure isolation
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
        run.finished_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="write",
        endpoint="POST /v1/routines/runs/{id}/retry",
        resource_type="routine_run",
        resource_ids=[run.id],
    )
    await session.flush()
    return run


async def rollback_run(
    session: AsyncSession, run_id, *, actor: str = "user"
) -> RoutineRun:
    run = await get_run(session, run_id)
    routine = await get_routine(session, run.routine_id)
    if not routine.undoable:
        raise ValueError("Routine is not marked undoable")
    if run.status != "executed":
        raise ValueError("Only executed runs can be rolled back")
    if run.undo_status == "done":
        raise ValueError("Run was already rolled back")
    if run.action_id is not None:
        await runtime_service.rollback_action(
            session,
            run.action_id,
            actor=actor,
            reason="routine rollback",
        )
    run.status = "rolled_back"
    run.undo_status = "done"
    run.undo_payload = {
        **routine.action_payload,
        "_undo": True,
        "run_id": str(run.id),
    }
    run.rolled_back_at = utcnow()
    run.finished_at = utcnow()
    run.updated_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="rollback",
        endpoint="POST /v1/routines/runs/{id}/rollback",
        resource_type="routine_run",
        resource_ids=[run.id],
    )
    await session.flush()
    return run


# --------------------------------------------------------------------------- #
# Template library (plan 19.4)
# --------------------------------------------------------------------------- #


async def instantiate_template(
    session: AsyncSession,
    slug: str,
    *,
    actor: str = "user",
    name: str | None = None,
    overrides: dict | None = None,
) -> Routine:
    """Create a real routine from a curated template, with optional overrides."""
    template = get_template(slug)
    fields: dict = {
        "name": name or template.name,
        "kind": template.kind,
        "schedule": template.schedule,
        "timezone": template.timezone,
        "quiet_hours_skip": template.quiet_hours_skip,
        "backfill_max": template.backfill_max,
        "cooldown_seconds": template.cooldown_seconds,
        "trigger": template.trigger,
        "action_type": template.action_type,
        "action_title": template.action_title,
        "action_payload": template.action_payload,
        "requires_approval": template.requires_approval,
        "undoable": template.undoable,
    }
    fields.update({key: value for key, value in (overrides or {}).items() if value is not None})
    routine = await create_routine(session, RoutineCreate(**fields), actor=actor)
    routine.metadata_ = {
        **(routine.metadata_ or {}),
        "template_slug": template.slug,
        "template_tags": list(template.tags),
        "personalization_hints": template.personalization_hints,
    }
    await session.flush()
    return routine


# --------------------------------------------------------------------------- #
# Observability (plan 19.5)
# --------------------------------------------------------------------------- #


async def detect_repeated_failures(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    threshold: int | None = None,
) -> list[Alert]:
    """Alert when a routine's last N runs are all failures (deduplicated).

    A pending alert with the same routine fingerprint suppresses re-alerting
    until the owner dismisses it; the run history remains the source of truth.
    """
    now = now or utcnow()
    threshold = threshold or settings.automation_failure_threshold
    if threshold <= 0:
        return []
    created: list[Alert] = []
    routines = await list_routines(session, enabled=True)
    for routine in routines:
        rows = (
            await session.execute(
                select(RoutineRun)
                .where(RoutineRun.routine_id == routine.id)
                .order_by(RoutineRun.created_at.desc())
                .limit(threshold)
            )
        ).scalars().all()
        if len(rows) < threshold or any(row.status != "failed" for row in rows):
            continue
        fingerprint = sha256_hex(f"automation_failure:{routine.id}")[:64]
        existing = (
            await session.execute(
                select(Alert).where(
                    Alert.fingerprint == fingerprint,
                    Alert.status == "pending",
                )
            )
        ).scalars().first()
        if existing is not None:
            continue
        latest = rows[0]
        alert = Alert(
            kind="automation_failure",
            title=f"Routine '{routine.name}' failing repeatedly",
            body=(
                f"{threshold} consecutive runs of routine '{routine.name}' failed. "
                f"Latest error: {latest.error or 'unknown'}"
            ),
            priority=0.6,
            tier="notify",
            status="pending",
            source="routines",
            trigger_ids=[str(row.id) for row in rows],
            rationale="Repeated automation failure detected from run history.",
            fingerprint=fingerprint,
            created_at=now,
            details={
                "routine_id": str(routine.id),
                "routine_name": routine.name,
                "failed_run_ids": [str(row.id) for row in rows],
                "latest_error": latest.error,
            },
        )
        session.add(alert)
        created.append(alert)
    await session.flush()
    return created


async def overview(session: AsyncSession, *, now: datetime | None = None) -> RoutineOverviewOut:
    """Compact observability summary for dashboards and status surfaces."""
    now = now or utcnow()
    since = now.replace(microsecond=0) - timedelta(hours=24)
    routines = await list_routines(session)
    enabled = [r for r in routines if r.enabled]
    runs = (
        await session.execute(
            select(RoutineRun).order_by(RoutineRun.created_at.desc()).limit(500)
        )
    ).scalars().all()
    runs_24h = [r for r in runs if _aware(r.created_at) >= since]
    failed_24h = [r for r in runs_24h if r.status == "failed"]
    awaiting = [r for r in runs if r.status == "awaiting_approval"]
    pending_alerts = (
        await session.execute(
            select(Alert).where(
                Alert.kind == "automation_failure",
                Alert.status == "pending",
            )
        )
    ).scalars().all()
    latest_error = next((r.error for r in runs if r.error), None)
    return RoutineOverviewOut(
        routines_total=len(routines),
        routines_enabled=len(enabled),
        routines_scheduled=sum(1 for r in routines if r.kind == "scheduled"),
        routines_trigger=sum(1 for r in routines if r.kind == "trigger"),
        runs_total=len(runs),
        runs_last_24h=len(runs_24h),
        runs_failed_last_24h=len(failed_24h),
        awaiting_approval=len(awaiting),
        pending_failure_alerts=len(pending_alerts),
        latest_error=latest_error,
        generated_at=now,
    )
