"""Sync watchdog: the follower supervises itself.

The 20s life tick ingests; this slower loop (default 5 min, scheduler-owned)
answers "is every source actually healthy?" and acts without the owner:

1. Detect — per-source store freshness, app state, grant health, and the
   consecutive-failure counters the daemon records in the cursor file.
2. Heal — hidden background relaunch for stalled WhatsApp/Mail (cooldown
   guarded). Cursor recovery and OAuth refresh already happen inline in
   the tick paths; the watchdog verifies their effects instead.
3. Escalate — fingerprinted ``sync_health`` alerts (one per source+condition,
   auto-resolved on heal) for states only the owner can fix: a dead Google
   grant, an app that will not sync after repeated restarts, a store whose
   reads keep failing. Delivery, quiet hours, and caps stay with the
   existing notification pipeline.

Nothing here pages the owner for a quiet inbox: an old-but-healthy store
means no new messages arrived, which is an honest answer, not an incident.
"""

from __future__ import annotations

import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, Integration, IntegrationCredential
from app.utils.text import sha256_hex, utcnow

logger = logging.getLogger("ev.sync_watchdog")

ALERT_KIND = "sync_health"
AUTO_HEALED_REASON = "auto_healed"

# Consecutive failed polls before a store is declared broken (schema drift,
# permissions) rather than briefly busy. At a 20s tick this is ~3 minutes.
READ_FAILURE_THRESHOLD = 10
# A closed app with an older store is stalled, not quiet.
STALL_SECONDS = {"whatsapp": 6 * 3600, "mail": 12 * 3600}
# Hidden relaunch at most hourly per source; escalate after repeated nudges.
NUDGE_COOLDOWN_SECONDS = 3600
NUDGES_BEFORE_ALERT = 2

_SOURCE_LABELS = {
    "whatsapp": "WhatsApp",
    "mail": "Mail",
    "imessage": "Messages",
    "calls": "Calls",
    "photos": "Photos",
}


def _fingerprint(key: str) -> str:
    return sha256_hex(f"sync_health:{key}")[:64]


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def _raise_alert(
    session: AsyncSession,
    *,
    key: str,
    title: str,
    body: str,
    priority: float,
    details: dict[str, Any],
) -> bool:
    """Insert the alert unless one is already pending. True when created."""
    fingerprint = _fingerprint(key)
    existing = (
        await session.execute(
            select(Alert).where(
                Alert.fingerprint == fingerprint,
                Alert.status == "pending",
            )
        )
    ).scalars().first()
    if existing is not None:
        return False
    session.add(
        Alert(
            kind=ALERT_KIND,
            title=title,
            body=body,
            priority=priority,
            tier="notify",
            status="pending",
            source="sync_watchdog",
            trigger_ids=[],
            rationale="Sync watchdog detected a state only the owner can fix.",
            fingerprint=fingerprint,
            created_at=utcnow(),
            details=details,
        )
    )
    return True


async def _resolve_alert(session: AsyncSession, *, key: str) -> bool:
    """Dismiss a pending alert the watchdog raised once its cause heals."""
    rows = (
        await session.execute(
            select(Alert).where(
                Alert.fingerprint == _fingerprint(key),
                Alert.status == "pending",
            )
        )
    ).scalars().all()
    if not rows:
        return False
    for row in rows:
        row.status = "dismissed"
        row.dismissed_at = utcnow()
        row.dismissed_reason = AUTO_HEALED_REASON
    return True


async def _check_google_grants(session: AsyncSession, result: dict[str, Any]) -> None:
    """Dead Google grants need the owner; expiring ones heal themselves."""
    integrations = (
        await session.execute(
            select(Integration).where(
                Integration.adapter.in_(["mail", "calendar"]),
                Integration.status == "active",
            )
        )
    ).scalars().all()
    for integration in integrations:
        if (integration.config or {}).get("provider") != "google":
            continue
        label = "Gmail" if integration.adapter == "mail" else "Google Calendar"
        key = f"{'gmail' if integration.adapter == 'mail' else 'gcal'}:reauth"
        cred = (
            await session.execute(
                select(IntegrationCredential)
                .where(
                    IntegrationCredential.integration_id == integration.id,
                    IntegrationCredential.kind == "oauth",
                    IntegrationCredential.revoked_at.is_(None),
                )
                .order_by(IntegrationCredential.created_at.desc())
            )
        ).scalars().first()
        dead = (
            cred is None
            or bool((cred.metadata_ or {}).get("reauth_required"))
            or (
                not cred.encrypted_refresh
                and (_as_aware(cred.expires_at) or datetime.max.replace(tzinfo=UTC))
                <= utcnow()
            )
        )
        if dead:
            raised = await _raise_alert(
                session,
                key=key,
                title=f"{label} needs reconnecting",
                body=(
                    f"Google rejected the saved {label} grant, so background sync "
                    f"for {label.lower()} is paused. Reconnect it in the workbench "
                    "Integrations page and sync resumes on its own."
                ),
                priority=0.7,
                details={
                    "adapter": integration.adapter,
                    "integration_id": str(integration.id),
                },
            )
            if raised:
                result["alerts_raised"].append(f"sync:{key}")
        elif await _resolve_alert(session, key=key):
            result["alerts_resolved"].append(f"sync:{key}")


async def _check_read_failures(
    daemon: Any, session: AsyncSession, result: dict[str, Any]
) -> None:
    """Consecutive poll failures mean breakage, not an empty inbox."""
    health = getattr(daemon, "_sync_health", {}) or {}
    for stream in ("whatsapp", "mail", "imessage", "calls", "photos"):
        entry = health.get(stream) or {}
        failures = int(entry.get("failures") or 0)
        key = f"{stream}:read_failing"
        if failures >= READ_FAILURE_THRESHOLD:
            label = _SOURCE_LABELS.get(stream, stream)
            raised = await _raise_alert(
                session,
                key=key,
                title=f"{label} reads keep failing",
                body=(
                    f"{failures} consecutive {label} reads failed "
                    f"(last: {entry.get('last_error') or 'unknown'}). "
                    "The app may have updated its format. Latest messages "
                    f"for {label} come from the last good copy until this clears."
                ),
                priority=0.65,
                details={
                    "stream": stream,
                    "failures": failures,
                    "last_error": entry.get("last_error"),
                },
            )
            if raised:
                result["alerts_raised"].append(f"sync:{key}")
        elif await _resolve_alert(session, key=key):
            result["alerts_resolved"].append(f"sync:{key}")


async def _check_stalled_apps(
    daemon: Any, fresh: dict[str, Any], result: dict[str, Any], session: AsyncSession
) -> None:
    """Closed + stale apps get relaunched; stubborn ones escalate."""
    from app.services.life_stream_daemon import ensure_background_sync

    now = time.time()
    for stream, threshold in STALL_SECONDS.items():
        info = fresh.get(stream) or {}
        mtime = info.get("mtime_age_s")
        app_running = info.get("app_running")
        key = f"{stream}:stalled"
        label = _SOURCE_LABELS[stream]
        stale = (
            isinstance(mtime, (int, float))
            and mtime > threshold
            and app_running is False
        )
        entry = daemon._health(stream)
        if not stale:
            entry["nudges"] = 0
            if await _resolve_alert(session, key=key):
                result["alerts_resolved"].append(f"sync:{key}")
            continue
        last_nudge = float(entry.get("last_nudge_at") or 0)
        if now - last_nudge >= NUDGE_COOLDOWN_SECONDS:
            try:
                outcome = ensure_background_sync(force=True)
                launched = outcome.get("launched") or []
            except Exception as exc:  # noqa: BLE001 - a failed nudge escalates, never crashes
                logger.warning("sync watchdog nudge failed: %s", type(exc).__name__)
                launched = []
            entry["last_nudge_at"] = now
            entry["nudges"] = int(entry.get("nudges") or 0) + 1
            result["nudged"].append(stream)
            logger.info(
                "sync watchdog relaunched %s in background (launched=%s)",
                stream,
                ",".join(str(item) for item in launched) or "none",
            )
        if int(entry.get("nudges") or 0) >= NUDGES_BEFORE_ALERT:
            hours = int(mtime // 3600) if isinstance(mtime, (int, float)) else 0
            raised = await _raise_alert(
                session,
                key=key,
                title=f"{label} won't sync in the background",
                body=(
                    f"{label} on this Mac hasn't synced in about {hours} hours "
                    "even after background restarts. Open it once so it can "
                    "sync; latest messages resume from there on their own."
                ),
                priority=0.55,
                details={"stream": stream, "mtime_age_s": mtime, "nudges": entry.get("nudges")},
            )
            if raised:
                result["alerts_raised"].append(f"sync:{key}")


async def run_sync_watchdog(
    session: AsyncSession,
    *,
    daemon: Any | None = None,
) -> dict[str, Any]:
    """One supervision pass. Never raises; failures return as notes.

    Google-grant checks always run. Mac-store checks (freshness, nudges,
    read failures) run only when the Mac hub is on, so owners who never
    enabled local sync hear nothing about its stores.
    """
    from app.services.life_stream_daemon import (
        get_life_stream_daemon,
        life_freshness,
        life_stream_should_run,
    )

    result: dict[str, Any] = {
        "alerts_raised": [],
        "alerts_resolved": [],
        "nudged": [],
        "notes": [],
    }
    try:
        await _check_google_grants(session, result)
    except Exception as exc:  # noqa: BLE001 - watchdog boundary: record and continue
        logger.warning("sync watchdog grant check failed: %s", type(exc).__name__)
        result["notes"].append(f"grant_check:{type(exc).__name__}")
    try:
        if not life_stream_should_run():
            await session.flush()
            return result
        resolved_daemon = daemon if daemon is not None else get_life_stream_daemon()
        fresh = life_freshness()
        if not fresh.get("ok"):
            result["notes"].append("freshness_unavailable")
            await session.flush()
            return result
        await _check_read_failures(resolved_daemon, session, result)
        await _check_stalled_apps(resolved_daemon, fresh, result, session)
        with contextlib.suppress(Exception):
            resolved_daemon.save_cursor()
        await session.flush()
        return result
    except Exception as exc:  # noqa: BLE001 - the watchdog must never break the scheduler
        logger.warning("sync watchdog pass failed: %s", type(exc).__name__)
        result["notes"].append(f"pass:{type(exc).__name__}")
        with contextlib.suppress(Exception):
            await session.flush()
        return result
