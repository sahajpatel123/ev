"""Attention budget: quiet hours, daily cap, dedup, priority routing.

EVIE is deliberately less intrusive than Karen. A non-emergency notification
must be *provably impossible* during quiet hours and past the daily cap, so
every suppression carries a reason that survives in the delivery ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.ev_sense import quiet_hours_active
from app.models import Notification
from app.utils.text import utcnow


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str | None = None
    note: str | None = None


def is_emergency(*, priority: float, tier: str, emergency: bool) -> bool:
    """True when the human's declared emergencies may pierce quiet hours."""
    return bool(
        emergency
        or priority >= settings.notify_emergency_priority_threshold
        or tier in ("urgent", "notify_card")
    )


async def decide(
    session: AsyncSession,
    *,
    fingerprint: str,
    exclude_id,
    priority: float,
    tier: str,
    emergency: bool,
    attention_kind: str = "incoming",
    allow_during_quiet_hours: bool,
    bypass_policy: bool,
    now: datetime | None = None,
) -> PolicyDecision:
    """Return the attention-budget verdict for one notification."""
    now = now or utcnow()

    if not bypass_policy:
        # Already-acknowledged events must never re-notify.
        if attention_kind == "acknowledged":
            return PolicyDecision(False, "already_acknowledged")

        # Dedup: identical content that already went out (or is in flight)
        # inside the window is suppressed, never double-delivered.
        since = now - timedelta(seconds=settings.notify_dedup_window_seconds)
        dup = (
            await session.execute(
                select(Notification.id).where(
                    Notification.fingerprint == fingerprint,
                    Notification.status.in_(["attempted", "delivered"]),
                    Notification.queued_at >= since,
                    Notification.id != exclude_id,
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            return PolicyDecision(False, "duplicate")

        # Permanent failure after repeated backend failures.
        failed_count = int(
            (
                await session.execute(
                    select(func.count(Notification.id)).where(
                        Notification.fingerprint == fingerprint,
                        Notification.status == "failed",
                    )
                )
            ).scalar_one()
        )
        if failed_count >= settings.notify_max_attempts:
            return PolicyDecision(False, "max_attempts")

        # Quiet hours: only human-declared emergencies may break them.
        if (
            quiet_hours_active(now)
            and not emergency
            and not allow_during_quiet_hours
        ):
            return PolicyDecision(False, "quiet_hours")

        # Hard per-day delivery cap.
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        delivered_today = int(
            (
                await session.execute(
                    select(func.count(Notification.id)).where(
                        Notification.status == "delivered",
                        Notification.delivered_at >= start_of_day,
                    )
                )
            ).scalar_one()
        )
        if delivered_today >= settings.daily_alert_budget and not emergency:
            return PolicyDecision(False, "daily_cap")

    return PolicyDecision(True, note="allowed")
