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
