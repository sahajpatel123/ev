"""Evie OS G2 — Evie Everywhere API.

One typed backend contract through which ANY trusted endpoint (Mac, primary
phone, secondary phone, future agents) reads and writes the SAME canonical
state. Every route reuses the canonical G1/notify/runtime services — no
parallel mobile logic, no per-device truth.

Auth: existing owner/device actor model (require_actor_context). Trust rules:
- life state, sync, mission control, memory recall: any trusted device token
  or the master key (sandbox devices resolve to isolated scopes upstream).
- approval RESOLUTION: stays on /v1/runtime/actions/{id}/approve|deny with its
  independent-factor policy. This surface only projects the pending view.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext, require_actor_context
from app.db import get_session

router = APIRouter(prefix="/v1/everywhere", tags=["evie-everywhere"])


class CapabilityResolveRequest(BaseModel):
    capability: str = Field(min_length=1, max_length=128)
    constraints: dict = Field(default_factory=dict)


class CommandEnvelope(BaseModel):
    """ONE typed cross-device command contract (G2 Stage 3).

    Identity is DERIVED from authentication — client-supplied owner/device
    fields are accepted as metadata only and never grant authority.
    """

    command_id: str = Field(min_length=8, max_length=128)
    operation: str = Field(min_length=3, max_length=64)
    arguments: dict = Field(default_factory=dict)
    expected_entity_version: int | None = Field(default=None, ge=0)
    correlation_id: str | None = Field(default=None, max_length=128)
    # Metadata at most (never authority):
    claimed_owner_id: str | None = Field(default=None, max_length=64)
    claimed_device_id: str | None = Field(default=None, max_length=64)


_ALLOWED_OPERATIONS = {
    "project.create",
    "project.update",
    "goal.create",
    "goal.update",
    "commitment.create",
    "commitment.cancel",
}


def _outcome(
    *,
    command_id: str,
    operation: str,
    result: dict,
    entity_type: str,
) -> dict:
    """Canonical command outcome (Stage 4): SUCCESS / REPLAYED / CONFLICT /
    NOT_FOUND / VALIDATION are all structured truth, never generic false."""
    entity_key = f"{entity_type}_id"
    entity = result.get(entity_type) or {}
    out: dict = {
        "command_id": command_id,
        "ok": bool(result.get("ok")),
        "operation": operation,
        "entity_type": entity_type,
        "entity_id": (entity.get("id") if isinstance(entity, dict) else None)
        or result.get("entity_id")
        or (result.get("arguments") or {}).get(entity_key),
        "new_version": entity.get("version") if isinstance(entity, dict) else None,
        "duplicate": bool(result.get("duplicate")),
        "unchanged": bool(result.get("unchanged")),
        "canonical_data": entity if isinstance(entity, dict) else None,
        "error_code": None if result.get("ok") else (result.get("error") or "failed"),
        "conflict": result.get("conflict"),
    }
    return out


@router.post("/commands")
async def commands(
    body: CommandEnvelope,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    """Single authoritative mutation entry point for trusted endpoints.

    Scope/identity come from auth (ctx.data_scope); device provenance is
    recorded server-side; every operation is durably idempotent by
    command_id; version conflicts return structured recovery data.
    """
    op = body.operation.strip().lower()
    if op not in _ALLOWED_OPERATIONS:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "UNKNOWN_OPERATION",
                "allowed": sorted(_ALLOWED_OPERATIONS),
            },
        )
    args = body.arguments or {}
    scope = ctx.data_scope
    device_id = str(ctx.device_id) if ctx.device_id else None

    def _need(*keys: str) -> dict:
        missing = [k for k in keys if not (args.get(k) or "").strip()]
        if missing:
            raise HTTPException(
                status_code=422,
                detail={"error_code": "VALIDATION", "missing": missing},
            )
        return args

    from app.everywhere.sync import current_cursor

    if op == "project.create":
        a = _need("title")
        result = await life.create_project(
            session, actor=scope, title=a["title"],
            priority=a.get("priority") or "NORMAL",
            description=a.get("description") or "",
            device_id=device_id, command_id=body.command_id,
        )
        out = _outcome(command_id=body.command_id, operation=op, result=result, entity_type="project")
    elif op == "project.update":
        a = _need("project_id")
        result = await life.update_project(
            session, actor=scope, project_id=a["project_id"],
            status=a.get("status"), priority=a.get("priority"),
            title=a.get("title"), description=a.get("description"),
            expected_version=body.expected_entity_version,
            device_id=device_id, command_id=body.command_id,
        )
        out = _outcome(command_id=body.command_id, operation=op, result=result, entity_type="project")
        if result.get("error") == "not_found":
            out["error_code"] = "NOT_FOUND"
    elif op == "goal.create":
        a = _need("title")
        result = await life.create_goal(
            session, actor=scope, title=a["title"],
            project_ref=a.get("project_ref") or a.get("project_id"),
            priority=a.get("priority") or "NORMAL",
            success_criteria=a.get("success_criteria") or "",
            device_id=device_id, command_id=body.command_id,
        )
        out = _outcome(command_id=body.command_id, operation=op, result=result, entity_type="goal")
    elif op == "goal.update":
        a = _need("goal_id")
        result = await life.update_goal(
            session, actor=scope, goal_id=a["goal_id"],
            state=a.get("state"), priority=a.get("priority"),
            progress_note=a.get("progress_note"), next_action=a.get("next_action"),
            title=a.get("title"),
            expected_version=body.expected_entity_version,
            device_id=device_id, command_id=body.command_id,
        )
        out = _outcome(command_id=body.command_id, operation=op, result=result, entity_type="goal")
        if result.get("error") == "not_found":
            out["error_code"] = "NOT_FOUND"
    elif op == "commitment.create":
        a = _need("description")
        due_raw = a.get("due_at")
        due = None
        if due_raw:
            from datetime import datetime as _dt

            try:
                due = _dt.fromisoformat(str(due_raw).replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail={"error_code": "VALIDATION", "missing": [], "bad_fields": ["due_at"]},
                ) from None
        result = await life.create_commitment(
            session, actor=scope, description=a["description"], due_at=due,
            project_ref=a.get("project_ref"), goal_id=a.get("goal_id"),
            device_id=device_id, command_id=body.command_id,
        )
        out = _outcome(command_id=body.command_id, operation=op, result=result, entity_type="commitment")
    elif op == "commitment.cancel":
        a = _need("commitment_id")
        result = await life.update_commitment(
            session, actor=scope, commitment_id=a["commitment_id"],
            status="CANCELLED", device_id=device_id, command_id=body.command_id,
        )
        out = _outcome(command_id=body.command_id, operation=op, result=result, entity_type="commitment")
        if result.get("error") == "not_found":
            out["error_code"] = "NOT_FOUND"
    else:  # pragma: no cover
        raise HTTPException(status_code=422, detail={"error_code": "UNKNOWN_OPERATION"})

    await session.commit()
    try:
        cur = await current_cursor(session)
    except Exception:
        cur = None
    out["server_cursor"] = cur
    out["correlation_id"] = body.correlation_id
    out["device_id"] = device_id
    return out


from app.life import service as life  # noqa: E402  (canonical mutation authority)


@router.get("/bootstrap")
async def bootstrap(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    """Snapshot + delta starting point for a (re)connecting device."""
    from app.everywhere.sync import bootstrap as bootstrap_snapshot

    payload = await bootstrap_snapshot(session, ctx)
    await session.commit()
    return {"ok": True, **payload}


@router.get("/changes")
async def changes(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    """Incremental semantic delta after the device's last cursor."""
    from app.everywhere.sync import changes as sync_changes

    result = await sync_changes(session, ctx, cursor=cursor, limit=limit)
    if result.get("ok"):
        await session.commit()
    return result


@router.get("/devices")
async def devices(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    from app.everywhere.devices import list_devices

    del ctx
    return {"ok": True, "devices": await list_devices(session)}


@router.get("/health-summary")
async def health_summary(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    """G2 diagnostics: devices, sync lag hints, pending actions/approvals."""
    from app.everywhere.devices import health_summary as summary

    del ctx
    return {"ok": True, **await summary(session)}


@router.get("/capabilities")
async def capabilities(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    """The ONE projected capability universe across all trusted devices."""
    from app.everywhere.capabilities import capability_universe

    del ctx
    universe = await capability_universe(session)
    return {"ok": True, **universe}


@router.post("/capabilities/resolve")
async def capabilities_resolve(
    body: CapabilityResolveRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    from app.everywhere.capabilities import CapabilityRouter

    del ctx
    return await CapabilityRouter.resolve(session, capability=body.capability, constraints=body.constraints)


@router.get("/mission-control/status")
async def mission_control_status(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    """Same Mission Control authority from every device; formatting may differ."""
    from app.everywhere.mission_control import status as mc_status

    return {"ok": True, **await mc_status(session, ctx)}


@router.get("/approvals/pending")
async def approvals_pending(
    limit: int = Query(default=20, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    """Privacy-safe pending approval projection (TTL-expired tickets expire).

    Resolution happens ONLY through the canonical runtime endpoints:
      POST /v1/runtime/actions/{id}/approve   (independent factor required)
      POST /v1/runtime/actions/{id}/deny      (owner trust)
    """
    from app.everywhere.approvals import pending_approvals

    # Approvals are OWNER decisions: a sandbox-scope endpoint never sees the
    # owner's pending queue (server-side trust enforcement, not UI hiding).
    if ctx.data_scope != "master":
        return {"ok": True, "count": 0, "approvals": []}
    del ctx
    rows = await pending_approvals(session, limit=limit)
    await session.commit()
    return {"ok": True, "count": len(rows), "approvals": rows}


@router.get("/notifications")
async def notifications(
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    from app.everywhere.sync import recent_notifications

    del ctx
    rows = await recent_notifications(session, limit=limit)
    return {"ok": True, "count": len(rows), "notifications": rows}


@router.post("/notifications/{notification_id}/ack")
async def notifications_ack(
    notification_id: UUID,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    """Acknowledge via the EXISTING notification authority; emits one canonical
    event so other devices see the ack on their next delta."""
    from app.everywhere.sync import emit_everywhere_event
    from app.notify.service import acknowledge_notification
    from app.utils.text import utcnow

    try:
        row = await acknowledge_notification(
            session, notification_id, device_id=ctx.device_id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Notification not found") from None
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    await emit_everywhere_event(
        session,
        event_type="notification.acknowledged",
        actor_label=ctx.actor,
        content={
            "notification_id": str(notification_id),
            "title": row.title[:256],
            "acknowledged_at": utcnow().isoformat(),
        },
        device_id=str(ctx.device_id) if ctx.device_id else None,
    )
    await session.commit()
    return {"ok": True, "notification_id": str(notification_id), "acknowledged": True}


@router.get("/memory/recall")
async def memory_recall(
    q: str = Query(min_length=1, max_length=512),
    k: int = Query(default=8, ge=1, le=25),
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    """Canonical Memory OS retrieval — the SAME Retriever the model pipeline
    uses (privacy boundary enforced inside: sensitive never crosses)."""
    from app.memory.retrieval import Retriever

    del ctx
    retriever = Retriever(session)
    hits = await retriever.search(q, k=k, access="model")
    return {
        "ok": True,
        "query": q,
        "memories": [
            {
                "id": str(h.memory.id),
                "memory_type": h.memory.memory_type,
                "text": h.memory.text,
                "importance": h.memory.importance,
                "event_time": h.memory.event_time.isoformat() if h.memory.event_time else None,
            }
            for h in hits
        ],
    }


@router.get("/conversation/resume_context")
async def conversation_resume_context(
    thread_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> dict:
    """Logical continuation state: thread + rollup + active Project/Goal refs."""
    from app.everywhere.continuity import resume_context

    return await resume_context(
        session,
        actor=ctx.actor,
        device_name=ctx.device.name if ctx.device else None,
        thread_id=thread_id,
        device=ctx.device,
    )
