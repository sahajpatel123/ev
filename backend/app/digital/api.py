"""Owner-facing Digital Operations API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor
from app.db import get_session
from app.digital.connection_pack import connection_pack
from app.digital.fabric import OpContext, answer_can_you, capability_matrix, execute
from app.digital.graph import live_descriptors, matrix_from, semantic_digital_families
from app.digital.orchestrate import handle_outcome
from app.digital.types import AutonomyLevel
from app.digital.waiting import GLOBAL_WAITING, owner_brief

router = APIRouter(prefix="/v1/digital", tags=["digital-operations"])


@router.get("/capabilities")
async def capabilities(session: AsyncSession = Depends(get_session), _actor: str = Depends(require_actor)) -> dict[str, Any]:
    wa_status = {"authenticated": False}
    try:
        from app.digital.adapters.whatsapp import ComputerWhatsAppBacking

        raw = await ComputerWhatsAppBacking().status()
        wa_status = {
            "authenticated": bool(raw.get("authenticated")),
            "diagnosis": raw.get("diagnosis"),
            "focus_theft": int(raw.get("focus_theft") or 0),
        }
    except Exception:
        wa_status = {"authenticated": False, "diagnosis": "status_probe_failed"}
    descs = await live_descriptors(session, whatsapp_status=wa_status)
    return {
        "matrix": matrix_from(descs),
        "static_matrix": capability_matrix(),
        "families": semantic_digital_families(descs),
        "descriptors": [d.as_public() for d in descs],
        "authority": "digital.capability_graph",
    }


@router.get("/can-you")
async def can_you(q: str, _actor: str = Depends(require_actor)) -> dict[str, Any]:
    return answer_can_you(q)


@router.get("/waiting")
async def waiting(_actor: str = Depends(require_actor)) -> dict[str, Any]:
    return owner_brief(GLOBAL_WAITING)


@router.get("/connection-pack")
async def pack(_actor: str = Depends(require_actor)) -> dict[str, Any]:
    return connection_pack()


@router.post("/act")
async def act(
    body: dict[str, Any],
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict[str, Any]:
    ctx = OpContext(
        actor=actor,
        session=session,
        autonomy=AutonomyLevel(str(body.get("autonomy") or "SEND_WITH_CONFIRMATION")),
        confirmed=bool(body.get("confirmed")),
        goal_id=body.get("goal_id"),
        idempotency_key=body.get("idempotency_key"),
    )
    result = await execute(str(body.get("service") or ""), str(body.get("operation") or ""), body.get("args") or {}, ctx=ctx)
    return result.as_model()


@router.post("/outcome")
async def outcome(
    body: dict[str, Any],
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict[str, Any]:
    ctx = OpContext(
        actor=actor,
        session=session,
        autonomy=AutonomyLevel(str(body.get("autonomy") or "SEND_WITH_CONFIRMATION")),
        confirmed=bool(body.get("confirmed")),
        goal_id=body.get("goal_id"),
    )
    return await handle_outcome(str(body.get("text") or ""), ctx=ctx)
