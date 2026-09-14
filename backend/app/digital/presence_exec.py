"""Presence graph node executors for Digital Operations — additive, no new engine."""

from __future__ import annotations

from typing import Any
from uuid import UUID

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

_SEND_KINDS = frozenset({"EMAIL_SEND", "WHATSAPP_SEND"})

# Payload keys a node may use to point at the approval that authorizes a send.
_APPROVAL_KEYS = ("action_id", "approval_id", "approval_action_id")


async def _send_is_approved(
    session: AsyncSession, row: PresenceContract, payload: dict[str, Any]
) -> bool:
    """Owner approval on record for this send — never a model-authored flag.

    Two durable sources count: an ``ApprovedAction`` ticket the node points at
    that the owner actually approved (still inside its confirmation TTL), or a
    ``WAIT_APPROVAL`` node already satisfied in this graph. Everything else
    (including a payload ``confirmed`` flag) is not authorization.
    """

    ref = ""
    for key in _APPROVAL_KEYS:
        value = str(payload.get(key) or "").strip()
        if value:
            ref = value
            break
    if ref:
        ticket = None
        try:
            from app.ev.confirm import confirmation_expired
            from app.models import ApprovedAction

            ticket = await session.get(ApprovedAction, UUID(ref))
        except (ValueError, TypeError, AttributeError):
            ticket = None
        if (
            ticket is not None
            and str(ticket.status) == "approved"
            and not confirmation_expired(ticket.payload)
        ):
            return True
    graph = row.graph if isinstance(row.graph, dict) else {}
    return any(
        isinstance(n, dict)
        and str(n.get("kind") or "") == "WAIT_APPROVAL"
        and str(n.get("status") or "") == "SUCCEEDED"
        for n in (graph.get("nodes") or [])
    )


async def _park_unsent(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], digital_status: str
) -> dict[str, Any]:
    """Park a composed-but-unsent node on the owner. Never reports success."""

    from app.presence import service as presence
    from app.presence.contract import GoalState, can_transition

    node_id = str(node.get("node_id") or "")
    approval_state = GoalState.WAITING_FOR_APPROVAL.value
    if str(row.state) != approval_state and can_transition(row.state, approval_state):
        await presence.set_wait(
            session,
            row,
            wait_state=approval_state,
            condition={"expect": "owner_approval"},
            reason=f"digital:{node_id}:{digital_status}:nothing_left_the_machine"[:500],
        )
    notice = "delivered"
    try:
        verdict = presence.attention_verdict(
            interruption=str(row.interruption_policy or "NORMAL"),
            priority=str(row.priority or "NORMAL"),
            approval_required=True,
        )
        await presence.deliver(
            session,
            row,
            title=f"Needs you: {row.objective[:80]}",
            body=(
                f"“{row.objective[:280]}” is waiting on your approval — "
                "nothing has left this machine yet."
            ),
            verdict=verdict,
            payload={"needs_you": True, "digital_status": digital_status, "node_id": node_id},
        )
    except Exception as exc:
        notice = f"notice_error:{type(exc).__name__}"
    return {
        "status": "WAITING",
        "digital_status": digital_status,
        "sent": False,
        "prepared": True,
        "stop": True,
        "notice": notice,
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

    # Authorization is a resolved owner approval on record, never a payload
    # flag: the graph payload is model-authored, so trusting it let a real
    # send run under SAFE_DELEGATED with the fabric's confirmation gate
    # bypassed. Without approval the send stays on the prepare-only path.
    approved = kind in _SEND_KINDS and await _send_is_approved(session, row, payload)
    args = dict(payload.get("args") or {})
    if approved:
        operation = "send"
    ctx = OpContext(
        session=session,
        goal_id=str(row.id),
        autonomy=AutonomyLevel.SAFE_DELEGATED if approved else AutonomyLevel.PREPARE_ONLY,
        confirmed=approved,
        actor="presence",
    )
    result = await execute(service, operation, args, ctx=ctx)
    if result.status.value in {"PREPARED", "WAITING_FOR_APPROVAL"}:
        # Composed locally or parked: nothing left the machine. Never a node
        # success — the goal waits on the owner instead of advancing to a
        # verified completion.
        return await _park_unsent(session, row, node, result.status.value)
    if result.status.value == "COMPLETED_VERIFIED":
        return {"status": "SUCCEEDED", "digital_status": result.status.value, "verification": result.verification}
    if result.status.value == "SERVICE_AUTH_REQUIRED":
        return {"status": "WAITING", "reason": "SERVICE_AUTH_REQUIRED", "stop": True}
    return {"status": "FAILED", "reason": result.diagnosis or result.error or result.status.value}
