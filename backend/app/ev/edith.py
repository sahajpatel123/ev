"""E.D.I.T.H.-inspired intelligence, ethically adapted:
focus designation, device fleet, recognition log, ops center, digital twin.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import alert_radar, companionship, health_radar
from app.ev.user_state import build_user_state
from app.memory.entities import get_or_create_entity
from app.models import (
    CommandLedger,
    Device,
    FleetTask,
    FocusDesignation,
    GearSnapshot,
    Memory,
    MemoryEvent,
    RecognitionLog,
)
from app.schemas import (
    AlertOut,
    CommandOut,
    FleetDeviceOut,
    FleetStatusOut,
    FleetTaskCreate,
    FocusDesignationCreate,
    FocusDesignationOut,
    HealthSummaryOut,
    HudFocusOut,
    OpsCenterOut,
    RecognitionCreate,
    TwinOut,
)
from app.utils.text import utcnow

# --------------------------------------------------------------------------- #
# Focus designation (E.D.I.T.H. targeting, pointed at attention instead of harm)
# --------------------------------------------------------------------------- #


# Universal task types every registered device can handle without a declared capability.
UNIVERSAL_TASK_TYPES = {"ping", "sync", "report_status", "ack"}

# Allowed fleet task lifecycle transitions.
FLEET_TASK_FLOW: dict[str, set[str]] = {
    "requested": {"accepted", "cancelled"},
    "accepted": {"running", "cancelled"},
    "running": {"completed", "failed"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


async def record_command(
    session: AsyncSession,
    *,
    command_type: str,
    actor: str,
    target_type: str | None = None,
    target_id: str | None = None,
    request: dict | None = None,
    status: str = "issued",
    result: dict | None = None,
    error: str | None = None,
) -> CommandLedger:
    """Append a command to the E.D.I.T.H. command ledger."""
    row = CommandLedger(
        command_type=command_type,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        actor=actor,
        status=status,
        request=request or {},
        result=result,
        error=error,
        completed_at=utcnow() if status in ("completed", "failed", "rejected") else None,
    )
    session.add(row)
    await session.flush()
    return row


async def list_commands(
    session: AsyncSession,
    *,
    actor: str,
    device_id: str | None = None,
    limit: int = 50,
) -> list[CommandLedger]:
    """Recent commands. Device actors only see commands they issued."""
    stmt = select(CommandLedger).order_by(CommandLedger.created_at.desc()).limit(min(limit, 200))
    if not actor.startswith("master"):
        stmt = stmt.where(CommandLedger.actor == actor)
    if device_id and actor.startswith("master"):
        stmt = stmt.where(CommandLedger.target_id == str(device_id))
    rows = await session.execute(stmt)
    return list(rows.scalars().all())


async def get_command(session: AsyncSession, command_id, *, actor: str):
    command = await session.get(CommandLedger, command_id)
    if command is None:
        raise KeyError(f"Command {command_id} not found")
    if not actor.startswith("master") and command.actor != actor:
        raise PermissionError("Command is not visible to this actor")
    return command


def _fleet_task_scoped(task: FleetTask, *, actor: str, device_id) -> bool:
    if actor.startswith("master"):
        return True
    return task.device_id == device_id


def _validate_task_type(device: Device, task_type: str) -> None:
    if task_type in UNIVERSAL_TASK_TYPES:
        return
    capabilities = [c.lower() for c in (device.capabilities or [])]
    if task_type.lower() not in capabilities:
        raise ValueError(
            f"Device '{device.name}' does not declare capability '{task_type}' "
            f"(capabilities: {device.capabilities or []})"
        )


async def designate_focus(
    session: AsyncSession,
    data: FocusDesignationCreate,
    *,
    actor: str,
) -> FocusDesignation:
    active = await active_focus(session)
    if active is not None:
        active.active = False
        active.ended_at = utcnow()
        await record_command(
            session,
            command_type="focus.end",
            actor=actor,
            target_type="focus",
            target_id=str(active.id),
            request={"label": active.label, "reason": "superseded by new designation"},
            result={"active": False},
            status="completed",
        )
    focus = FocusDesignation(
        label=data.label,
        kind=data.kind,
        target_id=data.target_id,
        reason=data.reason,
        active=True,
    )
    session.add(focus)
    await session.flush()
    await record_command(
        session,
        command_type="focus.designate",
        actor=actor,
        target_type=data.kind,
        target_id=str(focus.id),
        request={
            "label": data.label,
            "kind": data.kind,
            "target_id": data.target_id,
            "reason": data.reason,
        },
        result={"label": focus.label},
        status="completed",
    )
    return focus


async def active_focus(session: AsyncSession) -> FocusDesignation | None:
    result = await session.execute(
        select(FocusDesignation)
        .where(FocusDesignation.active.is_(True))
        .order_by(FocusDesignation.started_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def end_focus(session: AsyncSession, focus_id: UUID, *, actor: str) -> FocusDesignation:
    focus = await session.get(FocusDesignation, focus_id)
    if focus is None:
        raise KeyError(f"Focus {focus_id} not found")
    focus.active = False
    focus.ended_at = utcnow()
    await record_command(
        session,
        command_type="focus.end",
        actor=actor,
        target_type="focus",
        target_id=str(focus.id),
        request={"label": focus.label},
        result={"active": False},
        status="completed",
    )
    return focus


# --------------------------------------------------------------------------- #
# Device fleet (E.D.I.T.H.'s drone network → user's device network)
# --------------------------------------------------------------------------- #


async def fleet_status(session: AsyncSession) -> FleetStatusOut:
    devices = list((await session.execute(select(Device))).scalars().all())
    now = utcnow()
    rows: list[FleetDeviceOut] = []
    online = 0
    for device in devices:
        if device.revoked_at is not None:
            continue
        gear = (
            await session.execute(
                select(GearSnapshot)
                .where(GearSnapshot.device_id == str(device.id))
                .order_by(GearSnapshot.reported_at.desc())
                .limit(1)
            )
        ).scalars().first()
        last_seen = device.last_seen_at
        presence: Literal["online", "away", "unknown"]
        if last_seen is None:
            presence = "unknown"
        elif now - _aware(last_seen) <= timedelta(minutes=10):
            presence = "online"
            online += 1
        elif now - _aware(last_seen) <= timedelta(days=1):
            presence = "away"
        else:
            presence = "unknown"
        rows.append(
            FleetDeviceOut(
                device_id=device.id,
                name=device.name,
                capabilities=device.capabilities,
                last_seen_at=last_seen,
                latest_gear={
                    "reported_at": gear.reported_at.isoformat(),
                    "battery_percent": gear.battery_percent,
                    "storage_free_bytes": gear.storage_free_bytes,
                    "cpu_percent": gear.cpu_percent,
                }
                if gear
                else None,
                presence=presence,
            )
        )
    task_rows = (
        await session.execute(
            select(FleetTask).where(FleetTask.status.in_(["requested", "accepted", "running"]))
        )
    ).scalars().all()
    active_tasks = len(task_rows)
    return FleetStatusOut(devices=rows, online_count=online, active_tasks=active_tasks)


def _aware(value):
    return value if value.tzinfo is not None else value.replace(tzinfo=utcnow().tzinfo)


async def create_fleet_task(
    session: AsyncSession,
    data: FleetTaskCreate,
    *,
    actor: str,
    device_id: UUID | None = None,
) -> FleetTask:
    device = await session.get(Device, data.device_id)
    if device is None:
        raise KeyError(f"Device {data.device_id} not found")
    if device_id is not None and data.device_id != device_id:
        raise PermissionError("A device can only dispatch tasks to itself")
    try:
        _validate_task_type(device, data.task_type)
    except ValueError as exc:
        await record_command(
            session,
            command_type="fleet.task.create",
            actor=actor,
            target_type="device",
            target_id=str(device.id),
            request={"task_type": data.task_type, "payload": data.payload},
            status="rejected",
            error=str(exc),
        )
        raise
    task = FleetTask(
        device_id=data.device_id,
        task_type=data.task_type,
        status="requested",
        payload=data.payload,
        requested_by=actor,
    )
    session.add(task)
    await session.flush()
    await record_command(
        session,
        command_type="fleet.task.create",
        actor=actor,
        target_type="device",
        target_id=str(task.device_id),
        request={"task_type": task.task_type, "payload": task.payload},
        result={"task_id": str(task.id), "status": task.status},
        status="completed",
    )
    return task


async def list_fleet_tasks(session: AsyncSession, *, limit: int = 50) -> list[FleetTask]:
    result = await session.execute(
        select(FleetTask).order_by(FleetTask.created_at.desc()).limit(min(limit, 200))
    )
    return list(result.scalars().all())


async def list_pending_fleet_tasks(
    session: AsyncSession,
    *,
    actor: str,
    device_id: UUID | None = None,
    limit: int = 50,
) -> list[FleetTask]:
    """Device-facing queue: pending tasks for this device (all when master)."""
    stmt = (
        select(FleetTask)
        .where(FleetTask.status.in_(["requested", "accepted", "running"]))
        .order_by(FleetTask.created_at.desc())
        .limit(min(limit, 200))
    )
    if not actor.startswith("master"):
        if device_id is None:
            return []
        stmt = stmt.where(FleetTask.device_id == device_id)
    rows = await session.execute(stmt)
    return list(rows.scalars().all())


async def get_fleet_task(
    session: AsyncSession,
    task_id: UUID,
    *,
    actor: str,
    device_id: UUID | None = None,
) -> FleetTask:
    task = await session.get(FleetTask, task_id)
    if task is None:
        raise KeyError(f"Fleet task {task_id} not found")
    if not _fleet_task_scoped(task, actor=actor, device_id=device_id):
        raise PermissionError("Fleet task is not visible to this actor")
    return task


async def _transition_fleet_task(
    session: AsyncSession,
    task_id: UUID,
    *,
    actor: str,
    device_id: UUID | None,
    to_status: str,
    result: dict | None = None,
    error: str | None = None,
) -> FleetTask:
    task = await get_fleet_task(session, task_id, actor=actor, device_id=device_id)
    if to_status not in FLEET_TASK_FLOW.get(task.status, set()):
        raise ValueError(f"Invalid fleet task transition: {task.status} -> {to_status}")
    task.status = to_status
    if to_status == "accepted":
        task.accepted_by = actor
    if to_status in ("completed", "failed", "cancelled"):
        task.completed_at = utcnow()
    if result is not None:
        task.result = result
    if error is not None:
        task.result = {"error": error}
    await session.flush()
    await record_command(
        session,
        command_type=f"fleet.task.{to_status}",
        actor=actor,
        target_type="fleet_task",
        target_id=str(task.id),
        request={"task_type": task.task_type, "device_id": str(task.device_id)},
        result={"task_id": str(task.id), "status": task.status, **(result or {})},
        error=error,
        status="completed" if error is None else "failed",
    )
    return task


async def accept_fleet_task(
    session: AsyncSession,
    task_id: UUID,
    *,
    actor: str,
    device_id: UUID | None,
) -> FleetTask:
    return await _transition_fleet_task(
        session, task_id, actor=actor, device_id=device_id, to_status="accepted"
    )


async def start_fleet_task(
    session: AsyncSession,
    task_id: UUID,
    *,
    actor: str,
    device_id: UUID | None,
) -> FleetTask:
    return await _transition_fleet_task(
        session, task_id, actor=actor, device_id=device_id, to_status="running"
    )


async def complete_fleet_task(
    session: AsyncSession,
    task_id: UUID,
    *,
    actor: str,
    device_id: UUID | None,
    result: dict | None = None,
) -> FleetTask:
    return await _transition_fleet_task(
        session,
        task_id,
        actor=actor,
        device_id=device_id,
        to_status="completed",
        result=result or {},
    )


async def fail_fleet_task(
    session: AsyncSession,
    task_id: UUID,
    *,
    actor: str,
    device_id: UUID | None,
    error: str,
) -> FleetTask:
    return await _transition_fleet_task(
        session,
        task_id,
        actor=actor,
        device_id=device_id,
        to_status="failed",
        error=error,
    )


async def cancel_fleet_task(
    session: AsyncSession,
    task_id: UUID,
    *,
    actor: str,
    device_id: UUID | None,
) -> FleetTask:
    return await _transition_fleet_task(
        session, task_id, actor=actor, device_id=device_id, to_status="cancelled"
    )


# --------------------------------------------------------------------------- #
# Recognition log (user-tagged, over user-owned media — never stranger scanning)
# --------------------------------------------------------------------------- #


async def annotate(
    session: AsyncSession,
    data: RecognitionCreate,
    *,
    actor: str,
) -> RecognitionLog:
    entity = await get_or_create_entity(session, data.label, data.entity_type)
    row = RecognitionLog(
        event_id=data.event_id,
        live_event_id=data.live_event_id,
        attachment_id=data.attachment_id,
        entity_id=entity.id,
        label=data.label,
        confidence=data.confidence,
        source=data.source,
    )
    session.add(row)
    await session.flush()
    await record_command(
        session,
        command_type="recognition.annotate",
        actor=actor,
        target_type="entity",
        target_id=str(entity.id),
        request={
            "label": data.label,
            "entity_type": data.entity_type,
            "confidence": data.confidence,
            "source": data.source,
        },
        result={"recognition_id": str(row.id), "entity_id": str(entity.id)},
        status="completed",
    )
    return row


async def list_recognition(session: AsyncSession, *, limit: int = 50) -> list[RecognitionLog]:
    result = await session.execute(
        select(RecognitionLog).order_by(RecognitionLog.created_at.desc()).limit(min(limit, 200))
    )
    return list(result.scalars().all())


# --------------------------------------------------------------------------- #
# Ops center + digital twin + HUD focus overlay
# --------------------------------------------------------------------------- #


async def ops_center(session: AsyncSession) -> OpsCenterOut:
    state = await build_user_state(session)
    focus = await active_focus(session)
    health = await health_radar.morning_brief(session)
    alerts = await alert_radar.list_alerts(session, status="pending", limit=5)
    fleet = await fleet_status(session)
    pattern_rows = (
        await session.execute(
            select(Memory)
            .where(
                Memory.memory_type == "pattern",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
            .order_by(Memory.confidence.desc())
            .limit(5)
        )
    ).scalars().all()
    next_actions: list[str] = []
    if focus:
        next_actions.append(f"Stay locked on: {focus.label}")
    if state.current_task:
        next_actions.append(f"Continue: {state.current_task}")
    if alerts:
        next_actions.append(f"Handle top alert: {alerts[0].title}")
    if state.open_decisions:
        next_actions.append(f"Settle decision: {state.open_decisions[0]['text']}")
    if not next_actions:
        next_actions.append("No active operations — designate a focus to lock EV on target.")
    commands = await list_commands(session, actor="master", limit=5)
    return OpsCenterOut(
        generated_at=utcnow(),
        state=state,
        focus=FocusDesignationOut.model_validate(focus) if focus else None,
        health=HealthSummaryOut.model_validate(health),
        alerts=[AlertOut.model_validate(alert) for alert in alerts],
        fleet=fleet,
        open_decisions=state.open_decisions,
        patterns=[
            {
                "id": str(m.id),
                "text": m.text,
                "kind": (m.payload or {}).get("kind"),
                "confidence": m.confidence,
            }
            for m in pattern_rows
        ],
        next_actions=next_actions,
        recent_commands=[CommandOut.model_validate(c) for c in commands],
    )


async def twin(session: AsyncSession) -> TwinOut:
    rows = list(
        (
            await session.execute(
                select(Memory).where(
                    Memory.is_current.is_(True),
                    Memory.redacted.is_(False),
                    Memory.memory_type.in_(["fact", "preference", "goal", "pattern"]),
                )
            )
        ).scalars().all()
    )
    by_type: dict[str, list[dict]] = {"fact": [], "preference": [], "goal": [], "pattern": []}
    confidences: list[float] = []
    provenance_rows = (
        await session.execute(
            select(MemoryEvent.memory_id, MemoryEvent.event_id).where(
                MemoryEvent.memory_id.in_([memory.id for memory in rows])
            )
        )
    ).all() if rows else []
    provenance: dict[UUID, list[UUID]] = {}
    for memory_id, event_id in provenance_rows:
        provenance.setdefault(memory_id, []).append(event_id)
    for memory in rows:
        by_type.setdefault(memory.memory_type, []).append(
            {
                "id": str(memory.id),
                "text": memory.text,
                "confidence": memory.confidence,
                "source_type": memory.source_type,
                "event_time": memory.event_time.isoformat(),
                "updated_at": memory.updated_time.isoformat(),
                "version": memory.version,
                "source_event_ids": [str(e) for e in provenance.get(memory.id, [])],
            }
        )
        confidences.append(memory.confidence)
    relationship = await companionship.relationship_stats(session)
    health = await health_radar.morning_brief(session)
    return TwinOut(
        generated_at=utcnow(),
        facts=by_type["fact"],
        preferences=by_type["preference"],
        goals=by_type["goal"],
        patterns=by_type["pattern"],
        relationship=relationship,
        health=health,
        confidence=round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
    )


async def hud_focus(session: AsyncSession) -> HudFocusOut:
    focus = await active_focus(session)
    state = await build_user_state(session)
    context_parts = []
    if state.active_goal:
        context_parts.append(f"goal: {state.active_goal}")
    if state.current_task:
        context_parts.append(f"task: {state.current_task}")
    context = " | ".join(context_parts) if context_parts else "no active context"
    next_action = None
    if focus:
        next_action = f"Advance {focus.kind}: {focus.label}"
    elif state.open_decisions:
        next_action = f"Settle decision: {state.open_decisions[0]['text']}"
    return HudFocusOut(
        schema_version="ev.hud.focus.v1",
        generated_at=utcnow(),
        focus=FocusDesignationOut.model_validate(focus) if focus else None,
        locked=focus is not None,
        context=context,
        next_action=next_action,
        meta={
            "open_decisions": len(state.open_decisions),
            "recent_topics": state.recent_topics[:5],
        },
    )
