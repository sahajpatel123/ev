"""Live data recording: user-permissioned channels + immutable live events.

Every observation originates from an explicit user-controlled collector and
carries provenance (channel + collector), permissions (privacy level), and a
content hash.  Event privacy is fail-closed: an event can never be *less*
restrictive than its channel's granted permission.  Replays are idempotent
thanks to the unique (channel_id, sha256) invariant, so derived state can be
rebuilt from the recorded stream.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LiveChannel, LiveEvent
from app.schemas import (
    LiveChannelCreate,
    LiveChannelOut,
    LiveChannelStatus,
    LiveEventCreate,
    LiveStatusOut,
)
from app.security.pii import classify_pii, escalate_privacy
from app.utils.text import canonical_json, sha256_hex, utcnow

# More restrictive = higher order.
PRIVACY_ORDER = {
    "normal": 0,
    "sensitive": 1,
    "private": 2,
    "never_send_to_model": 3,
}


def effective_privacy(channel_privacy: str, event_privacy: str) -> str:
    """Fail closed: the stored event is at least as restrictive as its channel."""
    if PRIVACY_ORDER.get(event_privacy, 0) >= PRIVACY_ORDER.get(channel_privacy, 0):
        return event_privacy
    return channel_privacy


async def create_channel(session: AsyncSession, data: LiveChannelCreate) -> LiveChannel:
    result = await session.execute(
        select(LiveChannel).where(
            LiveChannel.name == data.name,
            LiveChannel.active.is_(True),
        )
    )
    existing = result.scalars().first()
    if existing is not None:
        return existing
    channel = LiveChannel(
        name=data.name,
        kind=data.kind,
        privacy_level=data.privacy_level,
        metadata_=data.metadata,
    )
    session.add(channel)
    await session.flush()
    return channel


async def get_or_create_channel(
    session: AsyncSession,
    *,
    name: str,
    kind: str,
    privacy_level: str = "normal",
) -> LiveChannel:
    result = await session.execute(
        select(LiveChannel).where(LiveChannel.name == name, LiveChannel.active.is_(True))
    )
    channel = result.scalars().first()
    if channel is not None:
        return channel
    channel = LiveChannel(
        name=name,
        kind=kind,
        privacy_level=privacy_level,
        metadata_={"collector": name},
    )
    session.add(channel)
    await session.flush()
    return channel


async def ingest_events(
    session: AsyncSession,
    channel: LiveChannel,
    events: list[LiveEventCreate],
) -> list[LiveEvent]:
    """Append-only ingestion with fail-closed privacy and replay idempotency."""
    stored: list[LiveEvent] = []
    latest: datetime | None = None
    collector = (channel.metadata_ or {}).get("collector") or channel.name
    candidates: list[tuple[LiveEventCreate, str, str, str, datetime]] = []
    for data in events:
        occurred_at = data.occurred_at or utcnow()
        effective = effective_privacy(channel.privacy_level, data.privacy_level)
        pii_categories = classify_pii(json.dumps(data.payload, default=str))
        effective = escalate_privacy(effective, pii_categories)
        canonical = canonical_json(
            {
                "channel_id": str(channel.id),
                "event_type": data.event_type,
                "payload": data.payload,
                "occurred_at": occurred_at.isoformat(),
                "collector": collector,
                "privacy_level": effective,
            }
        )
        candidates.append((data, effective, collector, sha256_hex(canonical), occurred_at))

    existing: set[str] = set()
    if candidates:
        digests = [c[3] for c in candidates]
        rows = await session.execute(
            select(LiveEvent.sha256).where(
                LiveEvent.channel_id == channel.id,
                LiveEvent.sha256.in_(digests),
            )
        )
        existing = {row[0] for row in rows.all()}

    for data, effective, collector, digest, occurred_at in candidates:
        if digest in existing:
            continue
        existing.add(digest)  # dedupe within the same batch
        row = LiveEvent(
            channel_id=channel.id,
            occurred_at=occurred_at,
            event_type=data.event_type,
            payload=data.payload,
            device_id=data.device_id,
            collector=collector,
            privacy_level=effective,
            sha256=digest,
        )
        session.add(row)
        stored.append(row)
        latest = latest or occurred_at
    await session.flush()
    if latest is not None:
        channel.last_event_at = latest
    return stored


async def list_channels(session: AsyncSession, *, active_only: bool = True) -> list[LiveChannel]:
    stmt = select(LiveChannel).order_by(LiveChannel.created_at.asc())
    if active_only:
        stmt = stmt.where(LiveChannel.active.is_(True))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_events(
    session: AsyncSession,
    channel_id: UUID,
    *,
    limit: int = 100,
    since: datetime | None = None,
) -> list[LiveEvent]:
    stmt = (
        select(LiveEvent)
        .where(LiveEvent.channel_id == channel_id)
        .order_by(LiveEvent.occurred_at.desc())
        .limit(min(limit, 500))
    )
    if since is not None:
        stmt = stmt.where(LiveEvent.occurred_at >= since)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def query_live_events(
    session: AsyncSession,
    *,
    access: str = "user",
    since: datetime | None = None,
    limit: int = 50,
) -> list[LiveEvent]:
    """Live events across active channels, filtered by the access slice.

    ``access="model"`` only returns the permitted slice: events and channels
    whose privacy level is never_send_to_model are excluded.
    """
    stmt = (
        select(LiveEvent)
        .join(LiveChannel, LiveChannel.id == LiveEvent.channel_id)
        .where(LiveChannel.active.is_(True))
        .order_by(LiveEvent.occurred_at.desc())
        .limit(min(limit, 500))
    )
    if since is not None:
        stmt = stmt.where(LiveEvent.occurred_at >= since)
    if access == "model":
        stmt = stmt.where(
            LiveEvent.privacy_level.notin_(("never_send_to_model", "sensitive")),
            LiveChannel.privacy_level.notin_(("never_send_to_model", "sensitive")),
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=utcnow().tzinfo)


def derive_audio_context(payload: dict) -> dict:
    """Minimal, derived audio-scene representation.

    Returns scene/in-call/music/noise flags plus confidence.  Raw audio refs and
    transcripts are intentionally never included, so model-facing context can
    know *what kind* of audio environment the user is in without exposing the
    content of the sound itself.
    """
    scene = str(payload.get("scene") or "").strip().lower()
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        confidence = None
    in_call = bool(payload.get("in_call")) or scene in {"meeting", "call", "conference"}
    music = bool(payload.get("music")) or scene in {"music", "music_playing"}
    noise = bool(payload.get("noise")) or scene in {"noise", "loud", "construction"}
    return {
        "scene": scene or "unknown",
        "in_call": in_call,
        "music": music,
        "noise": noise,
        "confidence": float(confidence) if confidence is not None else None,
    }


def derive_location_context(payload: dict) -> dict:
    """Minimal, coarse location/presence representation.

    Only named/coarse place labels and presence are surfaced.  Exact
    coordinates, addresses, and raw collector text are never included, so the
    model can know "at the airport" without receiving a precise fix.
    """
    place = (
        payload.get("coarse_place")
        or payload.get("place")
        or payload.get("city")
        or payload.get("neighborhood")
    )
    presence = str(payload.get("presence") or "unknown")[:32]
    return {
        "place": str(place)[:80] if place else None,
        "presence": presence,
        "coarse": bool(place),
    }


def derive_channel_signals(channel: LiveChannel, events: list[LiveEvent]) -> list[dict]:
    """Deterministic per-channel derived signal flags from a replay slice.

    Mirrors the EV Sense signal rules so rebuilt derived state is identical
    across replays.  Signals carry basis live-event ids (provenance) and only
    minimal derived representations -- never raw payload content.
    """
    signals: list[dict] = []
    if not events:
        return signals

    late_night: list[LiveEvent] = []
    health_anomalies: list[tuple[LiveEvent, str]] = []
    audio_in_call: list[LiveEvent] = []
    location_events: list[LiveEvent] = []

    for event in events:
        hour = _aware(event.occurred_at).hour
        if channel.kind == "screen" and (hour >= 23 or hour < 5):
            late_night.append(event)
        if channel.kind == "audio":
            derived = derive_audio_context(event.payload or {})
            if derived["in_call"]:
                audio_in_call.append(event)
        if channel.kind == "location":
            location_events.append(event)
        if channel.kind == "health":
            payload = event.payload or {}
            heart_rate = payload.get("heart_rate") or payload.get("bpm")
            readiness = payload.get("readiness")
            sleep_hours = payload.get("sleep_hours")
            if heart_rate is not None and (heart_rate < 40 or heart_rate > 110):
                health_anomalies.append((event, f"heart_rate {heart_rate}"))
            if readiness is not None and readiness < 40:
                health_anomalies.append((event, f"readiness {readiness}"))
            if sleep_hours is not None and sleep_hours < 4:
                health_anomalies.append((event, f"sleep_hours {sleep_hours}"))

    if len(late_night) >= 2:
        signals.append(
            {
                "kind": "screen_late_night",
                "count": len(late_night),
                "latest_at": max(late_night, key=lambda e: _aware(e.occurred_at)).occurred_at.isoformat(),
                "basis_ids": [str(e.id) for e in late_night[:5]],
            }
        )
    if health_anomalies:
        latest_event, detail = max(health_anomalies, key=lambda pair: _aware(pair[0].occurred_at))
        signals.append(
            {
                "kind": "live_health_signal",
                "count": len(health_anomalies),
                "detail": detail,
                "latest_at": _aware(latest_event.occurred_at).isoformat(),
                "basis_ids": [str(e.id) for e, _ in health_anomalies[:5]],
            }
        )
    if audio_in_call:
        signals.append(
            {
                "kind": "audio_in_call",
                "count": len(audio_in_call),
                "latest_at": max(audio_in_call, key=lambda e: _aware(e.occurred_at)).occurred_at.isoformat(),
                "basis_ids": [str(e.id) for e in audio_in_call[:5]],
            }
        )
    if location_events:
        latest = max(location_events, key=lambda e: _aware(e.occurred_at))
        derived = derive_location_context(latest.payload or {})
        signals.append(
            {
                "kind": "location_presence",
                "count": len(location_events),
                "presence": derived["presence"],
                "place": derived["place"],
                "latest_at": _aware(latest.occurred_at).isoformat(),
                "basis_ids": [str(e.id) for e in location_events[:5]],
            }
        )
    return signals


def live_context_line(
    channel: LiveChannel | None,
    event: LiveEvent,
    *,
    access: str = "user",
) -> str:
    """One derived, privacy-safe context line for a live event.

    Audio and location channels always render through their minimal derived
    representation.  For the model slice, screen and vision lines are also
    restricted to collector-provided summaries; raw payload text is never
    promoted to model context.
    """
    source = f"{channel.name}/{event.event_type}" if channel else event.event_type
    payload = event.payload or {}
    if channel is not None and channel.kind == "audio":
        derived = derive_audio_context(payload)
        line = f"[{source}] audio scene={derived['scene']} in_call={derived['in_call']}"
        if derived["confidence"] is not None:
            line += f" conf={derived['confidence']:.2f}"
        return line
    if channel is not None and channel.kind == "location":
        derived = derive_location_context(payload)
        line = f"[{source}] location presence={derived['presence']}"
        if derived["place"]:
            line += f" coarse={derived['place']}"
        return line
    if channel is not None and channel.kind == "screen":
        app = payload.get("app") or "unknown"
        details = [f"app={app}"]
        for key in ("document", "code_file", "meeting"):
            value = payload.get(key)
            if value:
                details.append(f"{key}={str(value)[:80]}")
        line = f"[{source}] screen {' '.join(details)}"
        summary = payload.get("summary")
        if summary:
            line += f" {str(summary)[:120]}"
        return line
    if access == "model" and channel is not None and channel.kind == "vision":
        summary = payload.get("summary") or payload.get("label") or "vision event"
        return f"[{source}] vision {str(summary)[:120]}"
    snippet = payload.get("text") or payload.get("summary") or event.event_type
    return f"[{source}] {str(snippet)[:160]}"


async def sense_signals(
    session: AsyncSession,
    *,
    since: datetime,
    late_night_min: int = 2,
) -> list[dict]:
    """Derived live-signal candidates for EV Sense (permissioned slice only).

    Returns deterministic signals with explicit live-event provenance
    (basis_ids) so every insight stays traceable to the recorded stream.
    """
    rows = (
        await session.execute(
            select(LiveEvent, LiveChannel)
            .join(LiveChannel, LiveChannel.id == LiveEvent.channel_id)
            .where(
                LiveChannel.active.is_(True),
                LiveEvent.occurred_at >= since,
                LiveEvent.privacy_level != "never_send_to_model",
                LiveChannel.privacy_level != "never_send_to_model",
            )
            .order_by(LiveEvent.occurred_at.desc())
            .limit(300)
        )
    ).all()

    signals: list[dict] = []
    late_night: list[LiveEvent] = []
    health_anomalies: list[tuple[LiveEvent, str]] = []
    audio_in_call: list[LiveEvent] = []
    location_events: list[LiveEvent] = []
    for event, channel in rows:
        hour = _aware(event.occurred_at).hour
        if channel.kind == "screen" and (hour >= 23 or hour < 5):
            late_night.append(event)
        if channel.kind == "audio":
            derived = derive_audio_context(event.payload or {})
            if derived["in_call"]:
                audio_in_call.append(event)
        if channel.kind == "location":
            location_events.append(event)
        if channel.kind == "health":
            payload = event.payload or {}
            heart_rate = payload.get("heart_rate") or payload.get("bpm")
            readiness = payload.get("readiness")
            sleep_hours = payload.get("sleep_hours")
            if heart_rate is not None and (heart_rate < 40 or heart_rate > 110):
                health_anomalies.append((event, f"heart_rate {heart_rate}"))
            if readiness is not None and readiness < 40:
                health_anomalies.append((event, f"readiness {readiness}"))
            if sleep_hours is not None and sleep_hours < 4:
                health_anomalies.append((event, f"sleep_hours {sleep_hours}"))

    if len(late_night) >= late_night_min:
        latest = max(late_night, key=lambda e: _aware(e.occurred_at))
        signals.append(
            {
                "kind": "screen_late_night",
                "text": (
                    f"Screen activity continued late into the night "
                    f"({len(late_night)} late-night live events)."
                ),
                "confidence": round(min(0.85, 0.55 + 0.05 * len(late_night)), 3),
                "importance": 0.7,
                "urgency": 0.5,
                "goal_relevance": 0.6,
                "benefit": 0.7,
                "why_now": (
                    f"Because {len(late_night)} permitted screen events occurred between "
                    f"23:00 and 05:00, latest at {_aware(latest.occurred_at).isoformat()}."
                ),
                "basis_ids": [str(event.id) for event in late_night[:5]],
            }
        )

    if health_anomalies:
        latest_event, detail = health_anomalies[0]  # rows are newest-first
        signals.append(
            {
                "kind": "live_health_signal",
                "text": f"Health live signal: {detail}.",
                "confidence": 0.75,
                "importance": 0.85,
                "urgency": 0.6,
                "goal_relevance": 0.6,
                "benefit": 0.75,
                "why_now": (
                    f"Because the health channel reported {detail} at "
                    f"{_aware(latest_event.occurred_at).isoformat()}."
                ),
                "basis_ids": [str(event.id) for event, _ in health_anomalies[:5]],
            }
        )

    if audio_in_call:
        latest = max(audio_in_call, key=lambda e: _aware(e.occurred_at))
        latest_derived = derive_audio_context(latest.payload or {})
        confidence = latest_derived["confidence"] or 0.7
        signals.append(
            {
                "kind": "audio_in_call",
                "text": (
                    f"Active call/meeting detected from {len(audio_in_call)} permissioned "
                    f"audio scene events."
                ),
                "confidence": round(min(0.95, max(0.5, confidence)), 3),
                "importance": 0.8,
                "urgency": 0.5,
                "goal_relevance": 0.6,
                "benefit": 0.8,
                "why_now": (
                    f"Because audio collectors reported an in-call/meeting scene at "
                    f"{_aware(latest.occurred_at).isoformat()}."
                ),
                "basis_ids": [str(event.id) for event in audio_in_call[:5]],
            }
        )

    if location_events:
        latest = max(location_events, key=lambda e: _aware(e.occurred_at))
        derived = derive_location_context(latest.payload or {})
        if derived["place"] or derived["presence"] not in {"unknown"}:
            place_text = f"Coarse location: {derived['place']}." if derived["place"] else "Coarse location updated."
            signals.append(
                {
                    "kind": "location_presence",
                    "text": f"{place_text} Presence: {derived['presence']}.",
                    "confidence": 0.8,
                    "importance": 0.6,
                    "urgency": 0.3,
                    "goal_relevance": 0.7,
                    "benefit": 0.7,
                    "why_now": (
                        f"Because the location channel reported presence "
                        f"'{derived['presence']}' at {_aware(latest.occurred_at).isoformat()}."
                    ),
                    "basis_ids": [str(event.id) for event in location_events[:5]],
                }
            )

    return signals


async def status(session: AsyncSession) -> LiveStatusOut:
    channels = await list_channels(session)
    since = utcnow() - timedelta(days=1)
    count_rows = (
        await session.execute(
        select(LiveEvent.channel_id, func.count(LiveEvent.id)).where(
            LiveEvent.ingested_at >= since
        )
        )
    ).all()
    counts: dict[UUID, int] = {row[0]: int(row[1]) for row in count_rows}
    consumed = int(
        (
            await session.execute(
                select(func.count(LiveEvent.id)).where(
                    LiveEvent.ingested_at >= since,
                    LiveEvent.consumed.is_(True),
                )
            )
        ).scalar_one()
        or 0
    )
    return LiveStatusOut(
        channels=[
            LiveChannelStatus(
                channel=LiveChannelOut.model_validate(channel),
                event_count=int(counts.get(channel.id, 0)),
                last_event_at=channel.last_event_at,
            )
            for channel in channels
        ],
        total_events_24h=int(sum(counts.values())),
        consumed_24h=consumed,
    )
