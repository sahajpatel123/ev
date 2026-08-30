"""Companion guardrails: evidence-based isolation detection and relationship stats.

Deliberately built against dependency loops: EV notices isolation and recommends
human connection, and is transparent about being an AI.
"""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.user_state import build_user_state
from app.models import DecisionOutcome, Device, Entity, Event, Memory, Prediction, ResponseLog
from app.schemas import IsolationScanOut, RelationshipOut
from app.utils.text import utcnow

GENERIC_HUMAN_NUDGE = (
    "I'm not a substitute for people. A real conversation with someone you know "
    "would help more than I can."
)

LONELINESS_TOKENS = re.compile(
    r"\b(lonely|alone|no one|nobody|isolated|isolation|i have no friends|"
    r"nobody cares|why bother|i feel invisible|miss having someone)\b",
    re.IGNORECASE,
)
PEOPLE_RE = re.compile(
    r"(?:my|our)\s+(friend|colleague|boss|manager|mom|dad|mother|father|brother|sister|"
    r"wife|husband|partner|girlfriend|boyfriend|roommate|neighbor)\s+([A-Z][a-z]+)",
)
SOCIAL_VERBS = re.compile(
    r"\b(met|called|talked to|texted|hung out|had lunch with|coffee with|visited|phoned)\b",
    re.IGNORECASE,
)
VENTING = re.compile(r"\b(ugh|sigh|i can't|i give up|not again|tired of everything)\b", re.IGNORECASE)


async def scan_isolation(session: AsyncSession, *, window_days: int = 14) -> IsolationScanOut:
    since = utcnow() - timedelta(days=window_days)
    rows = (
        await session.execute(
            select(Event).where(
                Event.tombstoned_at.is_(None),
                Event.occurred_at >= since,
                Event.event_type.in_(["message.user", "note", "voice", "share"]),
            )
        )
    ).scalars().all()

    lonely_events: list[Event] = []
    social_events: list[Event] = []
    venting_events: list[Event] = []
    late_night_events: list[Event] = []
    for event in rows:
        text = (event.content or {}).get("text") or ""
        if LONELINESS_TOKENS.search(text):
            lonely_events.append(event)
        if PEOPLE_RE.search(text) or SOCIAL_VERBS.search(text):
            social_events.append(event)
        if VENTING.search(text):
            venting_events.append(event)
        hour = event.occurred_at.hour
        if hour >= 23 or hour < 5:
            late_night_events.append(event)

    signals: list[dict] = []
    if lonely_events:
        signals.append(
            {
                "kind": "loneliness_language",
                "count": len(lonely_events),
                "event_ids": [str(e.id) for e in lonely_events[:5]],
            }
        )
    if len(social_events) == 0 and len(rows) >= 3:
        signals.append(
            {
                "kind": "no_social_mentions",
                "count": len(rows),
                "event_ids": [str(e.id) for e in rows[:5]],
            }
        )
    if venting_events:
        signals.append(
            {
                "kind": "venting",
                "count": len(venting_events),
                "event_ids": [str(e.id) for e in venting_events[:5]],
            }
        )
    if len(late_night_events) >= 3:
        signals.append(
            {
                "kind": "late_night_alone",
                "count": len(late_night_events),
                "event_ids": [str(e.id) for e in late_night_events[:5]],
            }
        )

    detected = len(lonely_events) >= 2 and len(social_events) < 2
    confidence = round(min(0.85, 0.45 + 0.1 * len(lonely_events)), 3) if detected else 0.0
    evidence_ids = list(
        {
            str(e.id)
            for e in lonely_events[:5] + venting_events[:3] + late_night_events[:3]
        }
    )
    recommendation = None
    if detected:
        recommendation = (
            "There have been several isolated stretches with little social contact in your "
            "timeline. A short call or meetup with someone from your network would break the "
            "loop — I can help you prepare for it. (I'm an AI; I don't replace that.)"
        )
    result = IsolationScanOut(
        detected=detected,
        signals=signals,
        recommendation=recommendation,
        evidence_ids=evidence_ids,
        confidence=confidence,
    )
    try:
        from app.ev.assistant import get_profile

        profile = await get_profile(session)
        profile.isolation_scan_ran_at = utcnow()
        profile.isolation_detected = detected
        profile.updated_at = utcnow()
        await session.flush()
    except Exception:  # noqa: BLE001 - scan result still returns
        pass
    return result


async def relationship_stats(session: AsyncSession) -> RelationshipOut:
    since = utcnow() - timedelta(days=30)
    interaction_rows = (
        await session.execute(
            select(Event).where(
                Event.tombstoned_at.is_(None),
                Event.occurred_at >= since,
                Event.event_type.in_(["message.user", "note", "voice", "share"]),
            )
        )
    ).scalars().all()
    logs = list(
        (
            await session.execute(
                select(ResponseLog).order_by(ResponseLog.created_at.desc()).limit(200)
            )
        ).scalars().all()
    )
    prediction_rows = list((await session.execute(select(Prediction))).scalars().all())
    reviewed_predictions = [p for p in prediction_rows if p.reviewed_at is not None]
    prediction_accuracy = (
        round(
            sum(1 for p in reviewed_predictions if p.outcome == "correct")
            / len(reviewed_predictions),
            3,
        )
        if reviewed_predictions
        else None
    )
    decision_memory_count = len(
        (
            await session.execute(
                select(Memory).where(
                    Memory.memory_type == "decision",
                    Memory.is_current.is_(True),
                    Memory.redacted.is_(False),
                )
            )
        ).scalars().all()
    )
    outcome_count = len(
        (await session.execute(select(DecisionOutcome))).scalars().all()
    )
    decision_review_rate = (
        round(outcome_count / decision_memory_count, 3)
        if decision_memory_count
        else None
    )
    state = await build_user_state(session)
    device_count = int(
        (
            await session.execute(
                select(func.count(Device.id)).where(Device.revoked_at.is_(None))
            )
        ).scalar_one()
        or 0
    )

    useful = [log for log in logs if log.was_useful is not None]
    followed = [log for log in logs if log.followed_recommendation is not None]
    challenges = [log for log in logs if (log.strategy or {}).get("challenge") and log.intervention_appropriate is not None]
    corrections = sum(1 for log in logs if log.was_correction)

    def rate(items: list[ResponseLog], attr: str) -> float | None:
        if not items:
            return None
        return round(sum(1 for item in items if getattr(item, attr)) / len(items), 3)

    return RelationshipOut(
        total_interactions=len(interaction_rows),
        topics=state.recent_topics,
        corrections=corrections,
        useful_ratings=len(useful),
        followed_rate=rate(followed, "followed_recommendation"),
        challenge_acceptance_rate=rate(challenges, "intervention_appropriate"),
        prediction_reviews=len(reviewed_predictions),
        prediction_accuracy=prediction_accuracy,
        decision_review_rate=decision_review_rate,
        devices=device_count,
        updated_at=utcnow(),
    )


async def first_person_name(session: AsyncSession) -> str | None:
    row = (
        await session.execute(
            select(Entity.name)
            .where(Entity.entity_type == "person")
            .order_by(Entity.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    name = (row or "").strip()
    return name or None


def isolation_nudge_text(person_name: str | None) -> str:
    if person_name:
        return (
            f"I'm not a substitute for people. When you can, reach out to {person_name}."
        )
    return GENERIC_HUMAN_NUDGE


async def maybe_isolation_nudge(
    session: AsyncSession,
    *,
    scan: IsolationScanOut | None = None,
    social_turns: int | None = None,
) -> str | None:
    """At most one nudge after a real scan trip (or N social turns that run a scan).

    If a scan has never run, return None — a fake nudge is worse than silence.
    """

    from app.config import settings
    from app.ev.assistant import get_profile

    profile = await get_profile(session)
    if profile.social_nudge_sent_at is not None:
        return None

    detected = False
    ran = profile.isolation_scan_ran_at is not None
    if scan is not None:
        ran = True
        detected = bool(scan.detected)
    elif ran:
        detected = bool(profile.isolation_detected)

    threshold = int(settings.social_nudge_after_turns)
    turns = profile.social_turn_count if social_turns is None else social_turns
    if not detected and turns >= threshold:
        scan = await scan_isolation(session)
        ran = True
        detected = bool(scan.detected)

    if not ran or not detected:
        return None

    person = await first_person_name(session)
    text = isolation_nudge_text(person)
    profile.social_nudge_sent_at = utcnow()
    profile.updated_at = utcnow()
    await session.flush()
    return text


async def note_social_turn(session: AsyncSession) -> int:
    from app.ev.assistant import get_profile

    profile = await get_profile(session)
    profile.social_turn_count = int(profile.social_turn_count or 0) + 1
    profile.updated_at = utcnow()
    await session.flush()
    return profile.social_turn_count
