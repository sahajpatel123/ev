"""G2 Phase 8/28 — Mission Control everywhere + concise device section.

Same canonical truth from every endpoint: this wraps the G1 situation service
(the only Mission Control authority) and adds a SHORT device/capability issue
section only when something is actually wrong. Healthy devices stay quiet.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.everywhere.devices import health_summary
from app.everywhere.owner import owner_scope
from app.life import service as life
from app.life.situation import summarize


async def status(session: AsyncSession, ctx: ActorContext) -> dict:
    scope = owner_scope(ctx.actor, device=ctx.device)
    snap = await life.situation_snapshot(session, actor=scope)
    health = await health_summary(session)
    issues = list(health.get("issues") or [])
    # Phase 28: only meaningful problems surface. Never clutter with healthy
    # device detail; never fabricate state we cannot see.
    device_notes = [
        f"{issue['display_name'] or 'A device'} is {issue['state'].lower()}."
        for issue in issues
        if issue.get("state") in ("OFFLINE", "DEGRADED")
    ]
    snap["capability_issues"] = device_notes[:3]
    snap["system_health"] = {
        "core": "READY",
        "devices_online": health.get("devices_online"),
        "devices_total": health.get("devices_total"),
        "pending_approvals": health.get("pending_approvals"),
        "notification_backlog": health.get("notification_backlog"),
    }
    return {"snapshot": snap, "summary": summarize(snap)}
