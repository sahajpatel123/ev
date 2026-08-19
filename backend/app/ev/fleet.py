"""Shared live thread, TTS routing, device bootstrap, panic, and transcript."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.assistant import bind_live_thread, get_profile
from app.ev.callouts import emit_callout
from app.models import Device, Event, FeatureGate
from app.notify.routing import best_reachable_device, device_reachability
from app.utils.text import utcnow

DEFAULT_PREFS = {
    "nickname": "EVIE",
    "quiet_hours": {"start": None, "end": None},
    "hud_layout": {},
    "feature_gates": [],
    "tts_voice": "default",
    "live_conversation_id": None,
}

WE_ARE_ONLINE = "We're online."
PREFS_FAILED = "I couldn't load prefs; using defaults."


async def resolve_registry_device(
    session: AsyncSession,
    raw: str | None,
) -> Device | None:
    """Accept a Device UUID or the registered Device.name (hostname tag)."""

    value = (raw or "").strip()
    if not value:
        return None
    try:
        uid = UUID(value)
    except ValueError:
        uid = None
    if uid is not None:
        device = await session.get(Device, uid)
        if device is not None and device.revoked_at is None:
            return device
    row = (
        await session.execute(
            select(Device).where(Device.name == value, Device.revoked_at.is_(None))
        )
    ).scalars().first()
    return row


async def tts_playback_device(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    prefer_device_id: UUID | None = None,
) -> Device | None:
    """Best reachable device that can play TTS (attention or voice).

    Uses the existing heartbeat registry. One online device → that device.
    """

    now = now or utcnow()
    if prefer_device_id is not None:
        preferred = await session.get(Device, prefer_device_id)
        if preferred is not None and preferred.revoked_at is None:
            caps = {str(c).lower() for c in (preferred.capabilities or [])}
            if "attention" in caps or "voice" in caps or not caps:
                return preferred
    attention = await best_reachable_device(session, "attention", now=now)
    voice = await best_reachable_device(session, "voice", now=now)
    if attention is None:
        return voice
    if voice is None:
        return attention
    rank = {"online": 0, "away": 1, "unknown": 2}
    a_rank = rank[device_reachability(attention, now)]
    v_rank = rank[device_reachability(voice, now)]
    if a_rank < v_rank:
        return attention
    if v_rank < a_rank:
        return voice
    return attention


async def owner_prefs(session: AsyncSession) -> dict:
    profile = await get_profile(session)
    gates = list((await session.execute(select(FeatureGate))).scalars().all())
    return {
        "nickname": profile.nickname,
        "quiet_hours": {
            "start": profile.quiet_hours_start,
            "end": profile.quiet_hours_end,
        },
        "hud_layout": dict(profile.hud_layout or {}),
        "feature_gates": [
            {
                "key": row.key,
                "status": row.status,
                "reason": row.reason,
            }
            for row in gates
        ],
        "tts_voice": profile.tts_voice or "default",
        "live_conversation_id": (
            str(profile.live_conversation_id) if profile.live_conversation_id else None
        ),
        "volume_percent": int(profile.volume_percent or 70),
    }


async def bootstrap_device(
    session: AsyncSession,
    device_id: UUID,
    *,
    actor: str = "master",
) -> dict:
    """Import owner prefs onto a newly paired device. Speak We're online once."""

    from app.ev.training_wheels import ensure_seed_gates

    device = await session.get(Device, device_id)
    if device is None:
        raise KeyError(f"Device {device_id} not found")
    if device.revoked_at is not None:
        raise PermissionError("device is revoked")

    prefs_loaded = True
    try:
        await ensure_seed_gates(session)
        await bind_live_thread(session)
        prefs = await owner_prefs(session)
    except Exception:
        prefs_loaded = False
        prefs = dict(DEFAULT_PREFS)

    now = utcnow()
    spoken = False
    spoken_text: str | None = None
    if not prefs_loaded:
        spoken = True
        spoken_text = PREFS_FAILED
    elif device.bootstrapped_spoken_at is None:
        spoken = True
        spoken_text = WE_ARE_ONLINE
        device.bootstrapped_spoken_at = now
        await emit_callout(
            session,
            WE_ARE_ONLINE,
            source="bootstrap",
            source_item=str(device.id),
            hud={"schema_version": "ev.hud.card.v1", "title": "Online", "body": WE_ARE_ONLINE},
        )
    if device.bootstrapped_at is None:
        device.bootstrapped_at = now
    await session.flush()
    return {
        "device_id": str(device.id),
        "prefs": prefs,
        "spoken": spoken,
        "spoken_text": spoken_text,
        "tts_device_id": str(device.id) if spoken else None,
        "bootstrapped_spoken_at": (
            device.bootstrapped_spoken_at.isoformat()
            if device.bootstrapped_spoken_at
            else None
        ),
        "prefs_loaded": prefs_loaded,
        "actor": actor,
    }


async def list_transcript(
    session: AsyncSession,
    *,
    since: datetime | None = None,
    limit: int = 50,
) -> dict:
    """Events on the owner's one live conversation_id."""

    thread = await bind_live_thread(session)
    stmt = (
        select(Event)
        .where(
            Event.conversation_id == thread.id,
            Event.tombstoned_at.is_(None),
            Event.event_type.in_(
                ("message.user", "message.assistant", "assistant.greeting")
            ),
        )
        .order_by(Event.occurred_at.asc(), Event.id.asc())
        .limit(min(max(limit, 1), 200))
    )
    if since is not None:
        stmt = stmt.where(Event.occurred_at > since)
    rows = list((await session.execute(stmt)).scalars().all())
    return {
        "conversation_id": str(thread.id),
        "events": [
            {
                "id": str(row.id),
                "event_type": row.event_type,
                "occurred_at": row.occurred_at.isoformat(),
                "text": ((row.content or {}).get("text") or "")[:2000],
                "source": row.source,
            }
            for row in rows
        ],
    }


async def panic_device(
    session: AsyncSession,
    device_id: UUID,
    *,
    actor: str = "master",
) -> dict:
    """Revoke one device. Remaining trusted devices hear that it went offline."""

    device = await session.get(Device, device_id)
    if device is None:
        raise KeyError(f"Device {device_id} not found")
    now = utcnow()
    already = device.revoked_at is not None
    if not already:
        device.revoked_at = now
        device.revoked_reason = "panic"
        device.token_hash = None
    name = device.name or "A device"
    text = f"{name} went offline."
    await emit_callout(
        session,
        text,
        source="panic",
        source_item=str(device.id),
        emergency=True,
        hud={"schema_version": "ev.hud.card.v1", "title": "Offline", "body": text},
    )
    await session.flush()
    return {
        "ok": True,
        "revoked": True,
        "already": already,
        "device_id": str(device.id),
        "revoked_at": device.revoked_at.isoformat() if device.revoked_at else None,
        "spoken": text,
        "lookout": "offline",
        "actor": actor,
    }


async def lock_all(
    session: AsyncSession,
    *,
    actor: str = "master",
    trusted: bool = True,
) -> dict:
    """Master-key / still-trusted device: revoke every remaining device token."""

    if not trusted:
        return {
            "ok": False,
            "error": "untrusted",
            "spoken": "A seized device is not trusted to lock the fleet. Use another device or the master key.",
        }
    now = utcnow()
    rows = list(
        (await session.execute(select(Device).where(Device.revoked_at.is_(None)))).scalars().all()
    )
    revoked_ids: list[str] = []
    for device in rows:
        device.revoked_at = now
        device.revoked_reason = "lock-all"
        device.token_hash = None
        revoked_ids.append(str(device.id))
    text = "Everything is locked."
    await emit_callout(
        session,
        text,
        source="lock-all",
        emergency=True,
        hud={"schema_version": "ev.hud.card.v1", "title": "Locked", "body": text},
    )
    await session.flush()
    return {
        "ok": True,
        "revoked": revoked_ids,
        "count": len(revoked_ids),
        "spoken": text,
        "lookout": "offline",
        "actor": actor,
    }
