"""Alert radar: watchlist over the user's own events/memories with priority scoring."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from dateutil import parser as date_parser
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, Event, LiveChannel, LiveEvent, Memory, Prediction, WatchlistItem
from app.schemas import WatchlistCreate
from app.utils.text import fingerprint, normalize_text, utcnow


def _priority(
    *,
    watch: WatchlistItem,
    kind_urgency: float,
    deadline_proximity: float,
    pattern_relevance: float,
) -> tuple[float, str]:
    priority = (
        0.4 * kind_urgency
        + 0.3 * watch.priority
        + 0.2 * deadline_proximity
        + 0.1 * pattern_relevance
    )
    priority = round(max(0.0, min(1.0, priority)), 3)
    if priority >= 0.7:
        tier = "urgent"
    elif priority >= 0.4:
        tier = "useful"
    else:
        tier = "background"
    return priority, tier


def _deadline_proximity(watch: WatchlistItem) -> float:
    if watch.kind != "deadline":
        return 0.0
    raw = (watch.metadata_ or {}).get("date") or watch.value
    try:
        when = date_parser.parse(raw)
    except (ValueError, TypeError, OverflowError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=utcnow().tzinfo)
    remaining = (when - utcnow()).total_seconds() / 86400.0
    if remaining < 0:
        return 0.4  # overdue, still worth one mention
    if remaining <= 1:
        return 1.0
    if remaining <= 3:
        return 0.7
    if remaining <= 7:
        return 0.5
    return 0.2


def _live_match_text(event: LiveEvent, channel: LiveChannel | None) -> str:
    """Derived, coarse text slice of a live event for watchlist matching.

    Only user-facing derived fields are matched: raw audio transcripts, raw
    screen dumps, and exact coordinates are intentionally not promoted, so the
    watch radar sees the same minimal representation the rest of EV sees.
    """
    payload = event.payload or {}
    kind = channel.kind if channel is not None else ""
    parts: list[str] = []
    if kind == "screen":
        for key in ("app", "document", "code_file", "meeting", "summary"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
    elif kind == "audio":
        for key in ("scene",):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
    elif kind == "location":
        for key in ("place", "coarse_place", "city", "neighborhood", "presence"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
    elif kind == "app":
        for key in ("app", "summary"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
    elif kind == "vision":
        for key in ("summary", "label"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
    elif kind == "health":
        for key, value in payload.items():
            if isinstance(key, str):
                parts.append(key)
            if isinstance(value, str):
                parts.append(value)
    elif event.event_type == "calendar.event.updated":
        for key in ("summary", "location"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
        for attendee in payload.get("attendees") or []:
            if not isinstance(attendee, dict):
                continue
            name = attendee.get("name") or attendee.get("email")
            if isinstance(name, str):
                parts.append(name)
    else:
        for key in ("text", "summary"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
    return " ".join(part for part in parts if part)


async def upsert_watch_item(session: AsyncSession, data: WatchlistCreate) -> WatchlistItem:
    key = normalize_text(data.value)
    result = await session.execute(
        select(WatchlistItem).where(
            WatchlistItem.kind == data.kind,
            WatchlistItem.active.is_(True),
        )
    )
    for item in result.scalars().all():
        if normalize_text(item.value) == key:
            item.priority = data.priority
            item.metadata_ = data.metadata
            item.sources = data.sources
            return item
    item = WatchlistItem(
        kind=data.kind,
        value=data.value,
        priority=data.priority,
        sources=data.sources,
        metadata_=data.metadata,
        active=data.active,
    )
    session.add(item)
    await session.flush()
    return item


async def list_watch_items(session: AsyncSession, *, active_only: bool = True) -> list[WatchlistItem]:
    stmt = select(WatchlistItem).order_by(WatchlistItem.priority.desc(), WatchlistItem.created_at.asc())
    if active_only:
        stmt = stmt.where(WatchlistItem.active.is_(True))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def deactivate_watch_item(session: AsyncSession, item_id: UUID) -> WatchlistItem:
    item = await session.get(WatchlistItem, item_id)
    if item is None:
        raise KeyError(f"Watchlist item {item_id} not found")
    item.active = False
    return item


async def scan(session: AsyncSession, *, window_days: int = 7, limit: int = 1000) -> dict:
    """Scan events, memories, and live signals for watchlist matches; dedupe alerts."""
    watch_items = await list_watch_items(session)
    if not watch_items:
        return {
            "scanned_events": 0,
            "scanned_memories": 0,
            "scanned_live_events": 0,
            "alerts_created": [],
            "existing_alerts": 0,
        }

    since = utcnow() - timedelta(days=window_days)
    event_stmt = (
        select(Event)
        .where(Event.tombstoned_at.is_(None), Event.occurred_at >= since)
        .order_by(Event.occurred_at.desc())
        .limit(limit)
    )
    events = list((await session.execute(event_stmt)).scalars().all())
    memory_stmt = (
        select(Memory)
        .where(Memory.is_current.is_(True), Memory.redacted.is_(False))
        .order_by(Memory.event_time.desc())
        .limit(limit)
    )
    memories = list((await session.execute(memory_stmt)).scalars().all())
    live_rows = (
        await session.execute(
            select(LiveEvent, LiveChannel)
            .join(LiveChannel, LiveChannel.id == LiveEvent.channel_id)
            .where(LiveChannel.active.is_(True), LiveEvent.occurred_at >= since)
            .order_by(LiveEvent.occurred_at.desc())
            .limit(limit)
        )
    ).all()

    pattern_topics = {
        normalize_text((m.payload or {}).get("topic") or "")
        for m in memories
        if m.memory_type == "pattern"
    }

    existing_stmt = select(Alert.fingerprint).where(Alert.status.in_(["pending", "delivered"]))
    existing = set((await session.execute(existing_stmt)).scalars().all())

    created: list[Alert] = []
    seen_new: set[str] = set()
    for watch in watch_items:
        proximity = _deadline_proximity(watch)
        pattern_relevance = 0.8 if normalize_text(watch.value) in pattern_topics else 0.3
        sources: list[tuple[str, list[Any]]] = [
            ("event", list(events)),
            ("memory", list(memories)),
            ("live", list(live_rows)),
        ]
        for source_type, rows in sources:
            for row in rows:
                if source_type == "live":
                    live_event: LiveEvent = row[0]
                    channel: LiveChannel | None = row[1]
                    text = _live_match_text(live_event, channel)
                    trigger_id = str(live_event.id)
                    occurred_at = live_event.occurred_at
                else:
                    raw_text = row.content.get("text") if isinstance(row, Event) else row.text
                    text = str(raw_text or "")
                    trigger_id = str(row.id)
                    occurred_at = getattr(row, "occurred_at", None) or getattr(row, "event_time", None) or utcnow()
                if not text or normalize_text(watch.value) not in normalize_text(text):
                    continue
                fp = fingerprint({"kind": watch.kind, "value": watch.value, "trigger": trigger_id})
                if fp in existing or fp in seen_new:
                    continue
                seen_new.add(fp)
                urgency = 1.0 if watch.kind == "deadline" and proximity >= 0.7 else 0.3
                priority, tier = _priority(
                    watch=watch,
                    kind_urgency=urgency,
                    deadline_proximity=proximity,
                    pattern_relevance=pattern_relevance,
                )
                snippet = text if len(text) <= 240 else text[:237] + "..."
                alert = Alert(
                    kind="watch_match",
                    title=f"Watch match: {watch.value}",
                    body=snippet,
                    priority=priority,
                    tier=tier,
                    source=f"{source_type}:{watch.kind}",
                    trigger_ids=[trigger_id],
                    rationale=(
                        f"Watch item '{watch.value}' ({watch.kind}) matched a recent {source_type} "
                        f"on {occurred_at.isoformat()}."
                    ),
                    fingerprint=fp,
                    details={
                        "watchlist_item_id": str(watch.id),
                        "watchlist_kind": watch.kind,
                        "source_type": source_type,
                        "trigger_kind": (
                            channel.kind
                            if source_type == "live" and channel is not None
                            else None
                        ),
                    },
                )
                session.add(alert)
                watch.last_matched_at = utcnow()
                created.append(alert)

    await session.flush()
    return {
        "scanned_events": len(events),
        "scanned_memories": len(memories),
        "scanned_live_events": len(live_rows),
        "alerts_created": created,
        "existing_alerts": len(existing),
    }


async def precision_report(
    session: AsyncSession,
    *,
    window_days: int = 7,
) -> dict:
    """Alert precision over a labeled window.

    A labeled alert is one whose ``details["label"]`` is ``correct`` /
    ``incorrect`` (set by an eval harness or a future review flow) OR whose
    dismissal reason is a label: ``correct`` / ``useful`` (a true positive) or
    ``incorrect`` / ``false_positive`` (a false positive). The dismissal path
    already exists in the API, so the owner can label real alerts without any
    new endpoint. No labels means precision is honestly reported as
    unmeasurable rather than fabricated from delivery counts.
    """
    since = utcnow() - timedelta(days=window_days)
    rows = (
        await session.execute(
            select(Alert).where(
                Alert.created_at >= since,
                Alert.details.is_not(None),
            )
        )
    ).scalars().all()
    positive_labels = {"correct", "useful"}
    negative_labels = {"incorrect", "false_positive"}

    def _label(row: Alert) -> str | None:
        detail = str((row.details or {}).get("label", "")).lower()
        if detail in positive_labels | negative_labels:
            return detail
        reason = str(row.dismissed_reason or "").lower()
        if reason in positive_labels | negative_labels:
            return reason
        return None

    labeled = [(row, _label(row)) for row in rows if _label(row) is not None]
    correct = sum(1 for _, label in labeled if label in positive_labels)
    incorrect = len(labeled) - correct
    return {
        "generated_at": utcnow().isoformat(),
        "window_days": window_days,
        "labeled_alerts": len(labeled),
        "correct": correct,
        "incorrect": incorrect,
        "label_sources": {
            "details_label": sum(
                1
                for row, _ in labeled
                if str((row.details or {}).get("label", "")).lower() in positive_labels | negative_labels
            ),
            "dismissed_reason": sum(
                1
                for row, _ in labeled
                if str(row.dismissed_reason or "").lower() in positive_labels | negative_labels
            ),
        },
        "precision": round(correct / len(labeled), 3) if labeled else None,
        "note": (
            "precision measured on labeled alert outcomes"
            if labeled
            else (
                "no labeled alert outcomes in window; label alerts by dismissing "
                "with reason 'correct'/'useful' (true positive) or "
                "'incorrect'/'false_positive' (false positive), or by setting "
                "details.label in an eval harness"
            )
        ),
    }


async def list_alerts(
    session: AsyncSession,
    *,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 50,
) -> list[Alert]:
    stmt = select(Alert).order_by(Alert.priority.desc(), Alert.created_at.desc()).limit(min(limit, 200))
    if status:
        stmt = stmt.where(Alert.status == status)
    if kind:
        stmt = stmt.where(Alert.kind == kind)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def dismiss_alert(session: AsyncSession, alert_id: UUID, reason: str = "dismissed") -> Alert:
    alert = await session.get(Alert, alert_id)
    if alert is None:
        raise KeyError(f"Alert {alert_id} not found")
    alert.status = "dismissed"
    alert.dismissed_at = utcnow()
    alert.dismissed_reason = reason
    return alert


async def build_digest(session: AsyncSession, *, limit: int = 50) -> dict:
    """Batch pending non-urgent alerts into one quiet-hours-friendly digest.

    Urgent alerts are never swallowed by the digest; only ``useful`` and
    ``background`` tiers are batched. Delivered alerts are marked with their
    digest id so delivery is observable and auditable.
    """
    pending = [
        alert
        for alert in await list_alerts(session, status="pending", limit=min(limit, 200))
        if alert.tier in ("useful", "background")
    ]
    digest_id = uuid4().hex
    delivered: list[dict] = []
    for alert in pending:
        alert.status = "delivered"
        alert.delivered_at = utcnow()
        details = dict(alert.details or {})
        details["delivery"] = "digest"
        details["digest_id"] = digest_id
        alert.details = details
        delivered.append(
            {
                "id": str(alert.id),
                "title": alert.title,
                "body": alert.body,
                "priority": alert.priority,
                "tier": alert.tier,
            }
        )
    return {
        "schema_version": "ev.runtime.digest.v1",
        "digest_id": digest_id,
        "generated_at": utcnow().isoformat(),
        "delivered": len(delivered),
        "alerts": delivered,
    }


async def promote_predictions(session: AsyncSession, predictions: list[Prediction]) -> list[Alert]:
    """Promote deliverable EV Sense predictions into pending alert-radar alerts.

    This is the explicit prediction -> alert signal flow: stored predictions that
    passed the attention policy become observable, dismissible alerts with the same
    evidence (trigger ids) and rationale, deduplicated against live alerts.
    """
    if not predictions:
        return []
    existing_stmt = select(Alert.fingerprint).where(
        Alert.status.in_(["pending", "delivered"]),
        Alert.source.like("ev_sense:%"),
    )
    existing = set((await session.execute(existing_stmt)).scalars().all())

    created: list[Alert] = []
    seen: set[str] = set()
    for prediction in predictions:
        tier = (prediction.details or {}).get("tier", "do_nothing")
        if tier not in ("notify", "notify_card"):
            continue
        fp = fingerprint({"kind": "prediction", "source": "ev_sense", "text": prediction.text})
        if fp in existing or fp in seen:
            continue
        seen.add(fp)
        priority = round(max(0.0, min(1.0, prediction.intervention_score or 0.0)), 3)
        alert_tier = (
            "urgent"
            if tier == "notify_card" or priority >= 0.7
            else ("useful" if priority >= 0.4 else "background")
        )
        alert = Alert(
            kind="prediction",
            title=f"EV Sense: {prediction.text[:120]}",
            body=prediction.text,
            priority=priority,
            tier=alert_tier,
            source=f"ev_sense:{prediction.kind}",
            trigger_ids=[str(prediction.id), *prediction.basis_ids],
            rationale=prediction.rationale or prediction.text,
            fingerprint=fp,
            details={
                "prediction_id": str(prediction.id),
                "prediction_kind": prediction.kind,
                "intervention_score": priority,
            },
        )
        session.add(alert)
        created.append(alert)
    await session.flush()
    return created


async def count_pending(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count(Alert.id)).where(Alert.status == "pending")
    )
    return int(result.scalar_one() or 0)
