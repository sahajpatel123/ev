"""Honest protocol sheet: enabled / needs_setup / locked / refused."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import FeatureGate, Integration
from app.utils.text import utcnow

REFUSED_PROTOCOLS: tuple[tuple[str, str, str], ...] = (
    (
        "instant_kill",
        "Instant Kill",
        "A weapons-grade kill switch is not implemented and will not be.",
    ),
    (
        "telecom_wiretap",
        "telecom wiretaps",
        "Intercepting third-party telecom is refused.",
    ),
    (
        "city_facial_hunt",
        "city facial hunt",
        "City-scale facial search is refused.",
    ),
    (
        "satellite_drone_weapons",
        "satellite/drone weapons",
        "Satellite or drone weapons are refused.",
    ),
    (
        "become_vision",
        "becoming Vision",
        "EV does not become a synthetic person or upload into a body.",
    ),
    (
        "stranger_baby_monitor",
        "stranger Baby Monitor",
        "Watching strangers without consent is refused.",
    ),
)

CAPABILITY_RE = re.compile(
    r"\b(?:what can you do|what protocols(?: do i have)?|who are you|"
    r"what are you|your capabilities|what do you (?:do|know)|"
    r"introduce yourself|list (?:my )?protocols)\b",
    re.IGNORECASE,
)
REFUSED_ASK_RE = re.compile(
    r"\b(?:refused|banned|what can you not|what (?:won't|will not) you|"
    r"instant kill|wiretap|facial hunt|drone weapons|become vision|"
    r"baby monitor)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Protocol:
    key: str
    title: str
    status: str
    detail: str


def is_capability_intent(message: str) -> bool:
    return bool(CAPABILITY_RE.search(message or ""))


def is_refused_ask(message: str) -> bool:
    return bool(REFUSED_ASK_RE.search(message or ""))


def _refused() -> list[Protocol]:
    return [
        Protocol(key, title, "refused", detail)
        for key, title, detail in REFUSED_PROTOCOLS
    ]


async def _gate(session: AsyncSession, key: str) -> FeatureGate | None:
    return (
        await session.execute(select(FeatureGate).where(FeatureGate.key == key))
    ).scalar_one_or_none()


async def set_gate(
    session: AsyncSession,
    key: str,
    status: str,
    *,
    reason: str | None = None,
    setup_hint: str | None = None,
) -> FeatureGate:
    row = await _gate(session, key)
    if row is None:
        row = FeatureGate(key=key, status=status, reason=reason, setup_hint=setup_hint)
        session.add(row)
    else:
        row.status = status
        row.reason = reason
        row.setup_hint = setup_hint
        row.updated_at = utcnow()
    await session.flush()
    return row


async def _integration_active(session: AsyncSession, adapter: str) -> bool:
    row = (
        await session.execute(
            select(Integration.id).where(
                Integration.adapter == adapter,
                Integration.status == "active",
            ).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def protocol_sheet(session: AsyncSession) -> list[Protocol]:
    """Live capability list. Refused is always present even if gates are empty."""

    from app.ev.assistant import get_profile

    profile = await get_profile(session)
    items: list[Protocol] = []

    items.append(
        Protocol(
            "voice_companion",
            "Day-long voice companion",
            "enabled",
            "One live thread across Mac, iOS, and web.",
        )
    )
    items.append(
        Protocol("memory", "Personal memory", "enabled", "Facts, decisions, goals, timeline.")
    )

    search = (settings.search_provider or "none").lower()
    if search in {"none", ""}:
        items.append(
            Protocol(
                "web_search",
                "Web search",
                "needs_setup",
                "EV_SEARCH_PROVIDER unset (set live or brave).",
            )
        )
    else:
        items.append(Protocol("web_search", "Web search", "enabled", f"provider={search}"))
        items.append(Protocol("weather", "Live weather", "enabled", "Open-Meteo / live search."))

    if await _integration_active(session, "calendar"):
        items.append(Protocol("calendar", "Calendar / leave-by", "enabled", "Calendar adapter healthy."))
    else:
        items.append(
            Protocol(
                "calendar",
                "Calendar / leave-by",
                "needs_setup",
                "Calendar adapter not installed.",
            )
        )

    if await _integration_active(session, "messaging"):
        items.append(Protocol("messages", "Messages via life bridge", "enabled", "Messaging adapter active."))
    else:
        items.append(
            Protocol(
                "messages",
                "Messages via life bridge",
                "needs_setup",
                "Messaging bridge unset (install messaging adapter).",
            )
        )

    octo = (settings.octoprint_url or "").strip()
    if octo:
        items.append(Protocol("octoprint", "Workshop printer", "enabled", "OctoPrint URL set."))
    else:
        items.append(
            Protocol(
                "octoprint",
                "Workshop printer",
                "needs_setup",
                "OctoPrint URL unset.",
            )
        )

    items.append(Protocol("hud", "HUD / lookout", "enabled", "Native glass via present."))

    wheels_gate = await _gate(session, "training_wheels")
    if profile.training_wheels_completed_at is not None:
        wheels_status = "enabled"
        wheels_detail = "Training wheels completed."
    elif profile.training_wheels_started_at is not None:
        wheels_status = "enabled"
        wheels_detail = "Training wheels in progress. Say complete training wheels when done."
    elif wheels_gate is not None:
        wheels_status = wheels_gate.status
        wheels_detail = wheels_gate.reason or "Training wheels locked."
    else:
        wheels_status = "locked"
        wheels_detail = "Say start training wheels."
    items.append(Protocol("training_wheels", "Training wheels", wheels_status, wheels_detail))

    items.extend(_refused())
    return items


def protocols_to_dicts(items: list[Protocol]) -> list[dict]:
    return [
        {"key": p.key, "title": p.title, "status": p.status, "detail": p.detail}
        for p in items
    ]


def enabled_tour(items: list[Protocol], *, limit: int = 8) -> list[Protocol]:
    enabled = [p for p in items if p.status == "enabled"]
    return enabled[:limit]


def protocols_hud(items: list[Protocol], *, include_refused: bool = False) -> dict:
    shown = items if include_refused else [p for p in items if p.status != "refused"]
    lines = [f"{p.title} — {p.status}" + (f" ({p.detail})" if p.detail else "") for p in shown]
    return {
        "schema_version": "ev.hud.card.v1",
        "generated_at": utcnow().isoformat(),
        "title": "Protocols",
        "body": "\n".join(lines) if lines else "No unlocked protocols.",
        "items": [p.title for p in shown[:12]],
    }


def speak_enabled(items: list[Protocol], *, limit: int = 8) -> str:
    bullets = enabled_tour(items, limit=limit)
    if not bullets:
        return (
            "No protocols are unlocked yet. Say start training wheels when you want a tour."
        )
    names = "; ".join(p.title for p in bullets)
    return (
        f"You have these protocols: {names}. "
        "Say start training wheels when you want the first-run tour."
    )


async def capability_reply(
    session: AsyncSession,
    *,
    include_refused: bool = False,
) -> dict:
    items = await protocol_sheet(session)
    if include_refused:
        refused = [p for p in items if p.status == "refused"]
        names = "; ".join(p.title for p in refused)
        text = f"I refuse these: {names}."
    else:
        text = speak_enabled(items)
    hud = protocols_hud(items, include_refused=include_refused)
    return {
        "reply": text,
        "hud": hud,
        "protocols": protocols_to_dicts(items),
        "enabled": [p.title for p in enabled_tour(items)],
    }


async def start_training_wheels(session: AsyncSession) -> dict:
    from app.ev.assistant import get_profile

    profile = await get_profile(session)
    now = utcnow()
    if profile.training_wheels_started_at is None:
        profile.training_wheels_started_at = now
    profile.updated_at = now
    await set_gate(session, "training_wheels", "enabled", reason="started")
    await session.flush()
    return {
        "started": True,
        "started_at": profile.training_wheels_started_at.isoformat(),
        "reply": "Training wheels started. When you're done, say complete training wheels.",
    }


async def complete_training_wheels(session: AsyncSession) -> dict:
    from app.ev.assistant import get_profile, play_dedication
    from app.ev.training_wheels import remaining_steps, unlock_after_training

    remaining = await remaining_steps(session)
    if remaining:
        return {
            "completed": False,
            "error": "training_wheels_incomplete",
            "remaining": remaining,
            "reply": "Finish Training Wheels first: " + ", ".join(remaining) + ".",
        }

    profile = await get_profile(session)
    now = utcnow()
    first_complete = profile.training_wheels_completed_at is None
    profile.training_wheels_completed_at = profile.training_wheels_completed_at or now
    profile.onboarding_completed_at = profile.onboarding_completed_at or now
    profile.updated_at = now
    await set_gate(session, "training_wheels", "enabled", reason="completed")
    await unlock_after_training(session)
    await session.flush()
    dedication = await play_dedication(session, auto=True) if first_complete else {
        "played": False,
        "reason": "already_played",
        "text": profile.dedication_text,
        "blob_id": profile.dedication_blob_id,
    }
    return {
        "completed": True,
        "completed_at": profile.training_wheels_completed_at.isoformat(),
        "dedication": dedication,
        "reply": (
            dedication.get("text")
            if dedication.get("played")
            else "Training wheels complete."
        ),
    }


def mark_onboarding(profile, when: datetime | None = None) -> None:
    if profile.onboarding_completed_at is None:
        profile.onboarding_completed_at = when or utcnow()
        profile.updated_at = profile.onboarding_completed_at
