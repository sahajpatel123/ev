"""G2 Phase 11 — Approval continuity.

ONE approval authority: ``approved_actions``. This module only projects a
privacy-safe pending view and expires stale TTL tickets using the EXISTING
confirm semantics. Resolution stays on the canonical runtime endpoints
(POST /v1/runtime/actions/{id}/approve|deny) with their independent-factor
policy — mobile adds a window, never a bypass.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.confirm import confirmation_expired, expire_action, pol_meta, tool_arguments
from app.models import ApprovedAction
from app.utils.text import utcnow

# Statuses that mean "still waiting on the owner".
PENDING_STATES = ("pending", "approved")

MAX_LIST = 50


def public_approval(action: ApprovedAction) -> dict:
    meta = pol_meta(action.payload)
    return {
        "id": str(action.id),
        "action_type": action.action_type,
        "title": action.title,
        "status": action.status,
        "risk_class": meta.get("risk_class"),
        "target": meta.get("target"),
        "expires_at": meta.get("expires_at"),
        "created_at": action.created_at.isoformat() if action.created_at else None,
        "requested_by": action.requested_by,
        # Bounded preview of the parked arguments (never full payloads).
        "arguments_preview": {
            k: str(v)[:120] for k, v in list(tool_arguments(action.payload).items())[:6]
        },
    }


async def pending_approvals(session: AsyncSession, *, limit: int = 20) -> list[dict]:
    """Pending owner decisions, oldest-risk-first display order is the caller's
    concern; here newest-first bounded. TTL-expired tickets are expired first so
    phones never see already-dead approvals as actionable."""
    now = utcnow()
    rows = (
        (
            await session.execute(
                select(ApprovedAction)
                .where(ApprovedAction.status == "pending")
                .order_by(ApprovedAction.created_at.desc())
                .limit(MAX_LIST)
            )
        )
        .scalars()
        .all()
    )
    out = []
    expired_any = False
    for row in rows:
        if confirmation_expired(row.payload, now=now):
            expire_action(row, reason="ttl_expired", now=now)
            expired_any = True
            continue
        out.append(public_approval(row))
        if len(out) >= max(1, min(limit, MAX_LIST)):
            break
    if expired_any:
        # Make expiry visible in this transaction (API layer commits).
        await session.flush()
    return out
