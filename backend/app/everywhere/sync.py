"""G2 Phase 4/5 — Event-cursor synchronization over canonical history.

SNAPSHOT + DELTA. Devices never replay the owner's lifetime event log and
never receive unrestricted dumps. A device presents its last cursor
``(occurred_at, id)`` into the immutable ``events`` table and receives the
bounded, relevance-filtered delta after it.

Laws:
- Stable deterministic ordering: (occurred_at ASC, id ASC). Events are
  immutable (tombstone-only), so the ordering is a safe resume point.
- Relevance filtering: only user-meaningful semantic events cross to devices.
  Model tokens, PCM packets, debug/heartbeat noise never sync.
- Privacy filtering: sensitive events require an owner-trust device.
- At-least-once with dedupe-by-id on the client; cursors advance server-side
  per device row (Device.sync_cursor_at / Device.sync_cursor_id).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.everywhere.owner import CANONICAL_OWNER, owner_scope
from app.life import service as life
from app.models import Device, Event
from app.utils.text import utcnow

SOURCE = "everywhere"

# Device-visible semantic vocabulary (Phase 4: DO NOT SYNC EVERYTHING).
VISIBLE_TYPE_PREFIXES: tuple[str, ...] = (
    "project.",
    "goal.",
    "goal_step.",
    "commitment.",
    "decision.",
    "approval.",
    "notification.",
    "mission_control.",
    "device.",
    "conversation.",
)

# Sources allowed across the device boundary today.
VISIBLE_SOURCES: tuple[str, ...] = ("life", SOURCE)

SENSITIVE = "sensitive"

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200

# A cursor older than this is refused: the client re-bootstraps from a fresh
# snapshot instead of attempting unsafe partial recovery.
CURSOR_MAX_AGE_DAYS = 30


def _visible_filters(*, owner_trusted: bool) -> list[Any]:
    filters: list[Any] = [Event.source.in_(VISIBLE_SOURCES)]
    if not owner_trusted:
        filters.append(Event.privacy_level != SENSITIVE)
    return filters


def _type_prefix_or(filters: list[Any]) -> list[Any]:
    from sqlalchemy import or_

    return [*filters, or_(*(Event.event_type.like(f"{p}%") for p in VISIBLE_TYPE_PREFIXES))]


async def current_cursor(session: AsyncSession, *, owner_trusted: bool = True) -> dict | None:
    """Latest visible event — the cursor a fresh client should start from."""
    stmt = (
        select(Event)
        .where(*_type_prefix_or(_visible_filters(owner_trusted=owner_trusted)))
        .order_by(Event.occurred_at.desc(), Event.id.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return {"at": row.occurred_at.isoformat(), "id": str(row.id)}


def parse_cursor(raw: str | None) -> tuple[datetime, UUID] | None | str:
    """Parse an opaque cursor. Returns (at, id), None (no cursor), or 'invalid'."""
    if not raw:
        return None
    try:
        at_raw, id_raw = raw.split("|", 1)
        at = datetime.fromisoformat(at_raw.replace("Z", "+00:00"))
        if at.tzinfo is None:
            from datetime import UTC

            at = at.replace(tzinfo=UTC)
        return at, UUID(id_raw)
    except (ValueError, TypeError):
        return "invalid"


def format_cursor(event: Event) -> str:
    return f"{event.occurred_at.isoformat()}|{event.id}"


async def changes(
    session: AsyncSession,
    ctx: ActorContext,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_LIMIT,
) -> dict:
    """Bounded delta after a cursor, ascending, relevance+privacy filtered."""
    limit = max(1, min(int(limit or DEFAULT_PAGE_LIMIT), MAX_PAGE_LIMIT))
    parsed = parse_cursor(cursor)
    if parsed == "invalid":
        return {"ok": False, "error": "CURSOR_INVALID", "reset_required": True}
    owner_trusted = bool(ctx.is_master or (ctx.device is not None and ctx.device.trust_level == "owner"))
    filters = _type_prefix_or(_visible_filters(owner_trusted=owner_trusted))
    if parsed is not None:
        at, eid = parsed
        if utcnow() - at > timedelta(days=CURSOR_MAX_AGE_DAYS):
            return {"ok": False, "error": "CURSOR_TOO_OLD", "reset_required": True}
        filters.append(tuple_(Event.occurred_at, Event.id) > tuple_(at, eid))
    stmt: Select = (
        select(Event)
        .where(*filters)
        .order_by(Event.occurred_at.asc(), Event.id.asc())
        .limit(limit + 1)
    )
    rows = (await session.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    events = [_public_event(e) for e in rows]
    next_cursor = format_cursor(rows[-1]) if rows else cursor
    await _advance_device_cursor(session, ctx, next_cursor)
    return {
        "ok": True,
        "count": len(events),
        "events": events,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def _public_event(e: Event) -> dict:
    return {
        "id": str(e.id),
        "type": e.event_type,
        "source": e.source,
        "at": e.occurred_at.isoformat(),
        "device_id": e.device_id,
        "privacy_level": e.privacy_level,
        "content": e.content or {},
    }


async def _advance_device_cursor(session: AsyncSession, ctx: ActorContext, cursor: str | None) -> None:
    if ctx.device is None or not cursor:
        return
    parsed = parse_cursor(cursor)
    if parsed is None or parsed == "invalid":
        return
    at, eid = parsed
    device: Device = ctx.device
    device.sync_cursor_at = at
    device.sync_cursor_id = eid


# ---------------------------------------------------------------------------
# Phase 5 — bootstrap snapshot (one round trip, bounded)
# ---------------------------------------------------------------------------


async def bootstrap(session: AsyncSession, ctx: ActorContext) -> dict:
    scope = owner_scope(ctx.actor, device=ctx.device)
    situation = await life.situation_snapshot(session, actor=scope)
    projects = await life.list_projects(session, actor=scope, active_only=True)
    goals_active = await life.list_goals(session, actor=scope, state="ACTIVE")
    goals_blocked = await life.list_goals(session, actor=scope, state="BLOCKED")
    commitments = await life.list_commitments(session, actor=scope, open_only=True)

    from app.everywhere.approvals import pending_approvals
    from app.everywhere.capabilities import capability_universe
    from app.everywhere.devices import list_devices

    approvals = await pending_approvals(session, limit=20)
    notifications = await recent_notifications(session, limit=20)
    devices = await list_devices(session)
    universe = await capability_universe(session)
    cursor = await current_cursor(session, owner_trusted=True)

    return {
        "owner": CANONICAL_OWNER,
        "generated_at": utcnow().isoformat(),
        "cursor": cursor,
        "projects": projects[:50],
        "goals": (goals_active + goals_blocked)[:100],
        "open_commitments": commitments[:50],
        "situation_summary": {
            "top_focus": situation.get("top_focus"),
            "active_goal_count": len(situation.get("active_goals") or []),
            "blocked_goal_count": len(situation.get("blocked_goals") or []),
            "open_commitment_count": len(situation.get("open_commitments") or []),
            "overdue_commitment_count": len(situation.get("overdue_commitments") or []),
        },
        "pending_approvals": approvals,
        "notifications": notifications,
        "devices": devices,
        "capabilities": {
            "revision": universe["revision"],
            "count": len(universe["capabilities"]),
            "available": sum(1 for c in universe["capabilities"] if c["state"] == "AVAILABLE"),
        },
    }


async def recent_notifications(session: AsyncSession, *, limit: int = 20) -> list[dict]:
    from sqlalchemy import select

    from app.models import Notification

    rows = (
        (
            await session.execute(
                select(Notification)
                .where(Notification.status == "delivered")
                .order_by(Notification.queued_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    out = []
    for r in rows:
        details = r.details or {}
        out.append(
            {
                "id": str(r.id),
                "kind": r.kind,
                "title": r.title,
                "body": r.body,
                "tier": r.tier,
                "priority": r.priority,
                "source": r.source,
                "queued_at": r.queued_at.isoformat() if r.queued_at else None,
                "action_id": str(r.action_id) if r.action_id else None,
                "acknowledged": bool(details.get("acknowledged_at")),
            }
        )
    return out


async def emit_everywhere_event(
    session: AsyncSession,
    *,
    event_type: str,
    actor_label: str,
    content: dict,
    device_id: str | None = None,
    privacy_level: str = "normal",
) -> None:
    """Canonical durable event for G2-visible state changes (same tx law).

    Mirrors app.life.service._emit but for approval/notification/device-surface
    transitions that happen outside the life services.
    """
    import hashlib
    import json

    payload = {"t": event_type, **content}
    session.add(
        Event(
            source=SOURCE,
            event_type=event_type,
            content=content,
            device_id=device_id,
            privacy_level=privacy_level,
            sha256=hashlib.sha256(
                json.dumps(payload, sort_keys=True, default=str).encode()
            ).hexdigest(),
            occurred_at=utcnow(),
        )
    )
