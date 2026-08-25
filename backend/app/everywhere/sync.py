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


# ---------------------------------------------------------------------------
# STATE EPOCH (P0): explicit server-owned lineage identity.
#
# Backed by the ``state_epoch`` table (app.ops.state_epoch). The epoch is a
# random opaque id representing the canonical DATABASE/HISTORY LINEAGE. It
# is stable through normal semantic activity, restarts, deploys, migrations,
# and event pruning. It changes ONLY on lineage-replacing operations
# (destructive restore / wipe / explicit replacement), which rotate it in
# the same transaction as the replacing write.
#
# Every cursor embeds the epoch it was minted under; a cursor from any other
# lineage is rejected with STATE_EPOCH_MISMATCH -> fresh bootstrap.
# ---------------------------------------------------------------------------


async def state_epoch(session: AsyncSession) -> str | None:
    from app.ops.state_epoch import ensure_current_epoch

    return await ensure_current_epoch(session)


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
    epoch = await state_epoch(session)
    return {
        "epoch": epoch,
        "at": row.occurred_at.isoformat(),
        "id": str(row.id),
    }


def format_cursor(event: Event, epoch: str | None = None) -> str:
    if epoch:
        return f"{epoch}|{event.occurred_at.isoformat()}|{event.id}"
    return f"{event.occurred_at.isoformat()}|{event.id}"


def parse_cursor(raw: str | None) -> tuple[str | None, datetime, UUID] | None | str:
    """Parse an opaque cursor.

    Returns (epoch, at, id), (None, at, id) for legacy pre-epoch cursors,
    None when absent, or 'invalid' when malformed.
    """
    if not raw:
        return None
    parts = raw.split("|")
    try:
        if len(parts) == 3:
            epoch, at_raw, id_raw = parts
            at = _parse_iso(at_raw)
            return (epoch, at, UUID(id_raw))
        if len(parts) == 2:
            at_raw, id_raw = parts
            at = _parse_iso(at_raw)
            return (None, at, UUID(id_raw))
    except (ValueError, TypeError):
        pass
    return "invalid"


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        from datetime import UTC

        dt = dt.replace(tzinfo=UTC)
    return dt


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

    # STATE EPOCH LAW (P0 Phase 13/14): a cursor minted under a different
    # canonical history lineage can never be continued. Old-epoch and legacy
    # pre-epoch cursors get an explicit reset so clients rebuild from the
    # current lineage instead of silently mixing histories.
    current_epoch = await state_epoch(session)
    if parsed is not None:
        cursor_epoch = parsed[0]
        if cursor_epoch != current_epoch:
            return {
                "ok": False,
                "error": "STATE_EPOCH_MISMATCH",
                "reset_required": True,
                "expected_epoch": current_epoch,
            }

    owner_trusted = bool(ctx.is_master or (ctx.device is not None and ctx.device.trust_level == "owner"))
    filters = _type_prefix_or(_visible_filters(owner_trusted=owner_trusted))
    if parsed is not None:
        _, at, eid = parsed
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
    next_cursor = format_cursor(rows[-1], epoch=current_epoch) if rows else cursor
    await _advance_device_cursor(session, ctx, next_cursor)
    return {
        "ok": True,
        "count": len(events),
        "events": events,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "epoch": current_epoch,
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
    _, at, eid = parsed
    device: Device = ctx.device
    device.sync_cursor_at = at
    device.sync_cursor_id = eid


# ---------------------------------------------------------------------------
# Phase 5 — bootstrap snapshot (one round trip, bounded)
# ---------------------------------------------------------------------------


async def bootstrap(session: AsyncSession, ctx: ActorContext) -> dict:
    # STAGE 9 CONSISTENCY LAW: capture the cursor HIGH-WATER MARK FIRST, then
    # read entities. Any mutation committing after the mark is guaranteed to
    # appear in changes(cursor=mark), so a concurrent write can never vanish
    # between snapshot and delta. (A write landing mid-read may appear in
    # BOTH — clients apply deltas idempotently by event id.)
    start_cursor = await current_cursor(session, owner_trusted=True)
    scope = owner_scope(ctx.actor, device=ctx.device)
    situation = await life.situation_snapshot(session, actor=scope)
    projects = await life.list_projects(session, actor=scope, active_only=True)
    goals_active = await life.list_goals(session, actor=scope, state="ACTIVE")
    goals_blocked = await life.list_goals(session, actor=scope, state="BLOCKED")
    commitments = await life.list_commitments(session, actor=scope, open_only=True)

    owner_scope_caller = scope == CANONICAL_OWNER

    from app.everywhere.approvals import pending_approvals
    from app.everywhere.capabilities import capability_universe
    from app.everywhere.devices import list_devices

    # Owner-only surfaces: sandbox endpoints never see the owner's approval
    # queue, notifications, or the device roster (server-side enforcement).
    approvals = await pending_approvals(session, limit=20) if owner_scope_caller else []
    notifications = await recent_notifications(session, limit=20) if owner_scope_caller else []
    devices = await list_devices(session) if owner_scope_caller else []
    universe = await capability_universe(session)

    return {
        "owner": CANONICAL_OWNER if owner_scope_caller else scope,
        "generated_at": utcnow().isoformat(),
        # STAGE 14 / PART 21: explicit auth-state categories — a paired
        # sandbox endpoint must SEE that it is not yet trusted, never infer
        # it from an empty project list.
        "device_trust": {
            "authenticated": True,
            "trusted": owner_scope_caller,
            "scope_resolved": scope,
            "state": (
                "TRUSTED_OWNER_DEVICE" if owner_scope_caller else "PAIRED_SANDBOX"
            ),
            "canonical_message": (
                None
                if owner_scope_caller
                else "This phone is paired, but it hasn't been trusted for access to your Evie data yet."
            ),
            "required_action": None if owner_scope_caller else "trust_device_from_mac",
        },
        "cursor": start_cursor,
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
