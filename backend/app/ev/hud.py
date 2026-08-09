"""HUD-ready status cards (ev.hud.card.v1) for watch/widget/AR surfaces."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import alert_radar
from app.ev.health_radar import morning_brief
from app.ev.user_state import build_user_state
from app.models import WatchlistItem
from app.schemas import HudCardOut
from app.utils.text import utcnow


async def status_card(session: AsyncSession) -> HudCardOut:
    state = await build_user_state(session)
    brief = await morning_brief(session)
    pending = await alert_radar.list_alerts(session, status="pending", limit=1)
    deadline_rows = (
        await session.execute(
            select(WatchlistItem).where(
                WatchlistItem.active.is_(True),
                WatchlistItem.kind == "deadline",
            )
        )
    ).scalars().all()

    parts: list[str] = []
    if state.active_goal:
        parts.append(f"Goal: {state.active_goal}")
    if state.active_project:
        parts.append(f"Project: {state.active_project}")
    if state.current_task:
        parts.append(f"Task: {state.current_task}")
    if brief.get("readiness") is not None:
        parts.append(f"Readiness {brief['readiness']} ({brief.get('band')})")
    if deadline_rows:
        parts.append(f"Next deadline: {deadline_rows[0].value}")
    body = " | ".join(parts) if parts else "No active signals. EV is watching."
    priority = pending[0].priority if pending else 0.0
    title = pending[0].title if pending else "EV status"
    return HudCardOut(
        schema_version="ev.hud.card.v1",
        generated_at=utcnow(),
        title=title,
        body=body,
        priority=priority,
        meta={
            "readiness": brief.get("readiness"),
            "band": brief.get("band"),
            "pending_alerts": len(pending),
            "active_project": state.active_project,
            "active_goal": state.active_goal,
            "open_decisions": len(state.open_decisions),
        },
    )

