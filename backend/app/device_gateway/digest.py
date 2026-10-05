"""Phone digest scheduler: quietly pushes a composed digest into the phone
inbox when a device's routine says it is due.

The digest is assembled from server-side facts only (HUD card, pending
reminders, calendar snapshot, health freshness) — no model call, so it
works fully offline and never fabricates a summary.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.device_gateway.phone_routines import due_times, normalize
from app.models import Device
from app.utils.text import utcnow

DIGEST_MAX_BODY = 1800


def _parse_event_start(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _in_label(start: datetime | None, *, now: datetime) -> str:
    if start is None:
        return "time unknown"
    delta = (start - now).total_seconds()
    if delta <= 0:
        return "now"
    if delta < 3600:
        return f"in {max(1, int(delta // 60))}m"
    if delta < 86400:
        hours = int(delta // 3600)
        minutes = int((delta % 3600) // 60)
        return f"in {hours}h {minutes}m" if minutes else f"in {hours}h"
    return f"in {int(delta // 86400)}d"


def _daypart(now: datetime, routines: dict[str, Any]) -> str:
    try:
        tz = ZoneInfo(routines.get("timezone") or "UTC")
        hour = now.astimezone(tz).hour
    except Exception:
        hour = now.hour
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


async def today_briefing(
    session: AsyncSession,
    device: Device,
    *,
    calendar: dict[str, Any],
    reminders: list[dict[str, Any]],
    routines: dict[str, Any],
    inbox_unread: int,
    memory_count: int,
    memory_enabled: bool,
) -> dict[str, Any]:
    """Anticipatory Today block: next event, top reminder, digest preview.

    Deterministic composition of facts the /today endpoint already holds —
    no model call, nothing invented. Sandbox devices get counts and daypart
    only: no titles, bodies, or previews leak across the trust boundary.
    """

    from app.everywhere.offline_queue import list_pending

    now = utcnow()
    pending = await list_pending(session, device_id=device.id, limit=100)
    queue_pending = sum(1 for item in pending if str(item.get("state") or "") == "pending")
    briefing: dict[str, Any] = {
        "daypart": _daypart(now, routines),
        "next_event": None,
        "top_reminder": None,
        "digest_preview": [],
        "counts": {
            "reminders_pending": len(reminders),
            "inbox_unread": int(inbox_unread),
            "queue_pending": queue_pending,
            "memories": int(memory_count),
        },
        "generated_at": now.isoformat(),
    }
    if not memory_enabled:
        return briefing
    events = calendar.get("events") if isinstance(calendar.get("events"), list) else []
    upcoming: list[tuple[datetime | None, str, str]] = []
    for item in events[:20]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        start_raw = str(item.get("start") or "")
        upcoming.append((_parse_event_start(start_raw), title, start_raw))
    if upcoming:
        upcoming.sort(key=lambda row: (row[0] is None, row[0] or datetime.max.replace(tzinfo=UTC)))
        start, title, start_raw = upcoming[0]
        briefing["next_event"] = {"title": title, "start": start_raw, "in_label": _in_label(start, now=now)}
    if reminders:
        briefing["top_reminder"] = str(reminders[0].get("text") or "untitled")[:280]
    try:
        briefing["digest_preview"] = (await _digest_lines(session, device))[:4]
    except Exception:
        briefing["digest_preview"] = []
    return briefing


async def _digest_lines(session: AsyncSession, device: Device) -> list[str]:
    from app.ev.alert_radar import list_alerts
    from app.ev.hud import status_card
    from app.ev.workbench import last_hud_payload

    lines: list[str] = []
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    last = await last_hud_payload(session)
    if last and last.get("schema_version") == "ev.hud.card.v1":
        title = str(last.get("title") or "").strip()
        body = str(last.get("body") or "").strip()
        if title and body:
            lines.append(f"{title}: {body}")
    else:
        try:
            card = await status_card(session)
            title = str(getattr(card, "title", "") or "").strip()
            body = str(getattr(card, "body", "") or "").strip()
            if title and body:
                lines.append(f"{title}: {body}")
        except Exception:  # noqa: BLE001 - digest is best-effort
            pass
    reminders = await list_alerts(session, status="pending", kind="reminder", limit=6)
    if reminders:
        lines.append("Reminders: " + "; ".join(str(r.body or r.title or "untitled") for r in reminders))
    calendar = profile.get("calendar") or {}
    events = [(str(e.get("title") or ""), str(e.get("start") or "")) for e in (calendar.get("events") or []) if e.get("title")]
    if events:
        lines.append("Calendar: " + "; ".join(title for title, _start in events[:3]))
    health = profile.get("healthkit") or {}
    freshness = str(health.get("freshness") or "unavailable")
    lines.append(f"Health: {freshness}.")
    return lines


async def deliver_digest(
    session: AsyncSession,
    *,
    device: Device,
    now: datetime,
) -> dict | None:
    """Compose and deliver one digest for a device; marks last_digest_at."""
    from app.everywhere.inbox import push_inbox

    cfg = normalize((getattr(device, "endpoint_profile", None) or {}).get("routines"))
    if not cfg["enabled"] or not cfg["digest_times"]:
        return None
    local_now = now.astimezone(ZoneInfo(cfg["timezone"]))
    if not due_times(local_now, cfg):
        return None
    lines = await _digest_lines(session, device)
    hour = local_now.hour
    if 5 <= hour < 12:
        digest_title = "Morning digest"
        greeting = "Good morning."
    elif 12 <= hour < 17:
        digest_title = "Afternoon digest"
        greeting = "Good afternoon."
    elif 17 <= hour < 22:
        digest_title = "Evening digest"
        greeting = "Good evening."
    else:
        digest_title = "Night digest"
        greeting = "Quick check before bed."
    body_lines = [greeting, *lines]
    body = "\n".join(body_lines)[:DIGEST_MAX_BODY] or "Everything is quiet."
    item = await push_inbox(
        session,
        device_id=device.id,
        kind="digest",
        title=digest_title,
        body=body,
        payload={"digest_time": local_now.strftime("%H:%M"), "timezone": cfg["timezone"]},
    )
    item_id = str(item.get("id") or "")
    from app.device_gateway.push import attempt_push

    push = await attempt_push(
        session,
        device=device,
        title=str(item.get("title") or "Evie digest"),
        body=str(item.get("body") or "")[:300],
        kind="digest",
        item_id=item_id,
    )
    if isinstance(item.get("payload"), dict):
        item["payload"]["push"] = push
    try:
        from uuid import UUID as _UUID

        from app.models import DeviceInboxItem as _InboxRow

        row = await session.get(_InboxRow, _UUID(item_id))
        if row is not None:
            row.payload = dict(row.payload or {})
            row.payload["push"] = push
    except Exception:  # noqa: BLE001 - payload enrichment is best-effort
        pass
    cfg["last_digest_at"] = now.isoformat()
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    profile["routines"] = cfg
    device.endpoint_profile = profile
    return item


async def phone_digest_tick(session: AsyncSession, *, now: datetime | None = None) -> list[dict]:
    """Scan phones and deliver any digest that is due right now."""
    now = now or utcnow()
    delivered: list[dict] = []
    devices = (
        (
            await session.execute(
                select(Device).where(
                    Device.device_type == "phone",
                    Device.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for device in devices:
        from app.device_gateway.sandbox import is_sandbox_device

        if is_sandbox_device(device):
            continue  # personal memory is off; digest would be hollow
        item = await deliver_digest(session, device=device, now=now)
        if item is not None:
            delivered.append({"device_id": str(device.id), "item_id": str(item.get("id") or "")})
    if delivered:
        await session.commit()
    return delivered


async def phone_digest_watch_loop() -> None:
    """Background loop: check every 30 s. No-op unless a phone enables a
    routine, so the Mac server is unaffected."""
    while True:
        try:
            from app.db import SessionLocal

            async with SessionLocal() as session:
                await phone_digest_tick(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - watcher must survive
            pass
        await asyncio.sleep(max(10, int(getattr(settings, "phone_digest_poll_seconds", 30))))
