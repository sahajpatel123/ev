"""HUD-ready outputs for watch/widget/AR surfaces.

Every HUD surface output validates against a strict schema in `HUD_SCHEMAS`,
so downstream renderers (Watch complications, widgets, future AR) can rely on
the exact same field contract.
"""

from __future__ import annotations

from typing import Protocol, cast

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import alert_radar
from app.ev.health_radar import morning_brief
from app.ev.user_state import build_user_state
from app.models import WatchlistItem
from app.schemas import (
    HudAlertOut,
    HudAlertTier,
    HudCardOut,
    HudFocusOut,
    HudOpsCardOut,
    HudQuickCardOut,
    RouteBriefingOut,
    TacticalBriefOut,
)
from app.utils.text import utcnow


class _HudPayload(Protocol):
    schema_version: str


HUD_SCHEMAS: dict[str, type[BaseModel]] = {
    "ev.hud.card.v1": HudCardOut,
    "ev.hud.briefing.v1": TacticalBriefOut,
    "ev.hud.focus.v1": HudFocusOut,
    "ev.hud.route.v1": RouteBriefingOut,
    "ev.hud.alert.v1": HudAlertOut,
    "ev.hud.quickcard.v1": HudQuickCardOut,
    "ev.hud.ops.v1": HudOpsCardOut,
}


def validate_hud(payload: dict | _HudPayload) -> tuple[str, BaseModel]:
    """Validate any HUD payload against its declared schema version."""
    schema_version = (
        payload["schema_version"] if isinstance(payload, dict) else payload.schema_version
    )
    if schema_version not in HUD_SCHEMAS:
        raise ValueError(f"Unknown HUD schema version: {schema_version}")
    return schema_version, HUD_SCHEMAS[schema_version].model_validate(payload)


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


async def alerts_card(session: AsyncSession) -> list[HudAlertOut]:
    """Render pending alerts as strict HUD alert cards, highest priority first."""
    pending = await alert_radar.list_alerts(session, status="pending", limit=5)
    return [
        HudAlertOut(
            schema_version="ev.hud.alert.v1",
            generated_at=utcnow(),
            alert_id=alert.id,
            title=alert.title,
            body=alert.body,
            priority=alert.priority,
            tier=cast(HudAlertTier, alert.tier),
            kind=alert.kind,
            rationale=alert.rationale,
            meta={
                "source": alert.source,
                "fingerprint": alert.fingerprint,
                "trigger_ids": alert.trigger_ids,
            },
        )
        for alert in pending
    ]
