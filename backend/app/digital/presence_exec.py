"""Presence graph node executors for Digital Operations — additive, no new engine."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PresenceContract

_KIND_OP = {
    "EMAIL_READ": ("gmail", "search"),
    "EMAIL_SEND": ("gmail", "draft"),
    "WHATSAPP_READ": ("whatsapp", "read_recent"),
    "WHATSAPP_SEND": ("whatsapp", "compose"),
    "CONTACT_RESOLVE": ("contacts", "resolve"),
    "CALENDAR_CREATE": ("calendar", "create"),
    "BROWSER_ACTION": ("browser", "observe"),
    "ARTIFACT_DOWNLOAD": ("files", "save"),
}


async def exec_digital_node(
    session: AsyncSession,
    row: PresenceContract,
    node: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    kind = str(node.get("kind") or "")
    if kind == "COMMUNICATION_WAIT":
        from app.presence import service as presence
        from app.presence.contract import GoalState

        cond = str(payload.get("cond_class") or payload.get("class") or "EMAIL_RECEIVED_MATCH")
        await presence.add_condition(
            session,
            row,
            cond_class=cond,
            payload=dict(payload.get("match") or payload),
        )
        await presence.set_wait(
            session,
            row,
            wait_state=GoalState.WAITING_FOR_CONDITION.value,
            reason=f"digital-wait:{cond}"[:500],
        )
        return {"status": "WAITING", "wait": GoalState.WAITING_FOR_CONDITION.value, "stop": True}

    mapped = _KIND_OP.get(kind)
    if mapped is None:
        return {"status": "FAILED", "reason": "unknown_digital_kind"}
    service, operation = mapped
    from app.digital.fabric import OpContext, execute
    from app.digital.types import AutonomyLevel

    confirmed = bool(payload.get("confirmed"))
    args = dict(payload.get("args") or {})
    if kind in {"EMAIL_SEND", "WHATSAPP_SEND"} and confirmed:
        operation = "send"
    ctx = OpContext(
        session=session,
        goal_id=str(row.id),
        autonomy=AutonomyLevel.SAFE_DELEGATED if confirmed else AutonomyLevel.PREPARE_ONLY,
        confirmed=confirmed,
        actor="presence",
    )
    result = await execute(service, operation, args, ctx=ctx)
    if result.status.value in {"WAITING_FOR_APPROVAL", "PREPARED"}:
        return {
            "status": "WAITING" if result.status.value == "WAITING_FOR_APPROVAL" else "SUCCEEDED",
            "digital_status": result.status.value,
            "sent": False,
            "prepared": True,
        }
    if result.status.value == "COMPLETED_VERIFIED":
        return {"status": "SUCCEEDED", "digital_status": result.status.value, "verification": result.verification}
    if result.status.value == "SERVICE_AUTH_REQUIRED":
        return {"status": "WAITING", "reason": "SERVICE_AUTH_REQUIRED", "stop": True}
    return {"status": "FAILED", "reason": result.diagnosis or result.error or result.status.value}
