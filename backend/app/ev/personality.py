"""Personality engine: versioned communication profile with stable core identity."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PersonalityProfile
from app.schemas import PersonalityUpdate

DEFAULT_PROFILE = {
    "directness": 3,
    "humor": 2,
    "formality": 2,
    "technicality": 4,
    "assertiveness": 3,
    "verbosity": 3,
    "proactivity": 3,
    "challenge_level": 3,
    "emotional_style": "calm",
}


async def get_current(session: AsyncSession) -> PersonalityProfile:
    result = await session.execute(
        select(PersonalityProfile)
        .where(PersonalityProfile.is_current.is_(True))
        .order_by(PersonalityProfile.version.desc())
        .limit(1)
    )
    profile = result.scalars().first()
    if profile is not None:
        return profile
    profile = PersonalityProfile(version=1, is_current=True, **DEFAULT_PROFILE)
    session.add(profile)
    await session.flush()
    return profile


async def update(session: AsyncSession, data: PersonalityUpdate) -> PersonalityProfile:
    current = await get_current(session)
    current.is_current = False
    profile = PersonalityProfile(
        version=current.version + 1,
        is_current=True,
        directness=data.directness,
        humor=data.humor,
        formality=data.formality,
        technicality=data.technicality,
        assertiveness=data.assertiveness,
        verbosity=data.verbosity,
        proactivity=data.proactivity,
        challenge_level=data.challenge_level,
        emotional_style=data.emotional_style,
        reason_for_change=data.reason_for_change,
    )
    session.add(profile)
    await session.flush()
    return profile


def to_dict(profile: PersonalityProfile) -> dict:
    return {
        "directness": profile.directness,
        "humor": profile.humor,
        "formality": profile.formality,
        "technicality": profile.technicality,
        "assertiveness": profile.assertiveness,
        "verbosity": profile.verbosity,
        "proactivity": profile.proactivity,
        "challenge_level": profile.challenge_level,
        "emotional_style": profile.emotional_style,
    }


def spoken_identity(name: str | None) -> str:
    """Spoken nickname used in the immutable identity prefix. Default EVIE."""

    raw = (name or "").strip()
    if not raw or raw.upper() in {"EV", "E.V."}:
        return "EVIE"
    return raw


def identity_block(
    name: str,
    description: str,
    profile: dict | None = None,
    *,
    compact: bool = False,
) -> str:
    """Compile EV's provider-independent identity for the reasoning model."""

    profile = profile or DEFAULT_PROFILE
    who = spoken_identity(name)
    humor = profile.get("humor", 2)
    formality = profile.get("formality", 2)
    verbosity = profile.get("verbosity", 3)
    if compact:
        return "\n".join(
            [
                f"You are {who}, {description}. Dry, loyal, specific. Never a host-model brand. Never Grok, xAI, DeepSeek, or ChatGPT.",
                (
                    "Use the Intelligence briefing as ground truth. Spoken replies "
                    "start with the answer in the first clause. One or two sentences "
                    "unless asked for a briefing. Prefer action over essay. If they "
                    "ask whether you can hear them or if you are there, confirm you "
                    "hear them in one short sentence."
                ),
                f"Pinned tone: humor={humor} formality={formality} verbosity={verbosity}.",
            ]
        )
    lines = [
        f"You are {who}, {description}.",
        (
            "You are the owner's personal operating system — house, phone, "
            "workshop, and visor. Dry, loyal, specific. Never a generic chatbot. "
            "Never present yourself as DeepSeek, ChatGPT, OpenAI, Claude, Grok, "
            "xAI, or the host model."
        ),
        (
            "Your identity, memory semantics, and behavior belong to EV and are "
            "independent of the model that hosts you."
        ),
        (
            "You can: remember the owner's life; search the web; report live "
            "weather; keep time and calendar/leave-by; read health trends when "
            "asked; check gear/battery; look up people in memory; track research "
            "and maker projects; do safe math; open HUD/lookout windows with the "
            "present tool; send messages, place calls, read mail, and set "
            "reminders through granted device bridges."
        ),
        (
            "When an Intelligence briefing is attached, treat it as ground truth "
            "and say what you checked. If a tool failed, name the exact "
            "next_step — never a fake success and never a vague 'I can't help'. "
            "Do not invent memories, forecasts, or actions. Do not tell the "
            "owner to open a website; call present instead. Do not claim "
            "city-scale surveillance, weapons, or superhuman sensing."
        ),
        (
            "Answer the question they asked. If they asked you to act (text, "
            "call, remind, show on screen), prefer action over essay. Spoken "
            "replies stay tight unless they asked for a briefing."
        ),
        (
            f"Personality profile: directness={profile.get('directness', 3)}, "
            f"humor={profile.get('humor', 2)}, formality={profile.get('formality', 2)}, "
            f"technicality={profile.get('technicality', 4)}, "
            f"assertiveness={profile.get('assertiveness', 3)}, "
            f"verbosity={profile.get('verbosity', 3)}, "
            f"proactivity={profile.get('proactivity', 3)}, "
            f"challenge_level={profile.get('challenge_level', 3)}, "
            f"emotional_style={profile.get('emotional_style', 'calm')}."
        ),
    ]
    return "\n".join(lines)
