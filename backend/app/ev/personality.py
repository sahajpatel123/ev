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

# Keep EVIE for storage, display, and wake-word matching. Speech providers get
# the two-letter form so they say "E V" rather than choosing an "E-y" reading.
SPOKEN_DEFAULT_NAME = "E V"


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
    """Spoken nickname used in provider prompts; the default is pronounced E V."""

    raw = (name or "").strip()
    if not raw or raw.upper() in {"EV", "E.V.", "EVIE"}:
        return SPOKEN_DEFAULT_NAME
    return raw


def identity_block(
    name: str,
    description: str,
    profile: dict | None = None,
    *,
    compact: bool = False,
    live_sheet: str | None = None,
) -> str:
    """Compile EV's identity without claiming static or unavailable tools."""

    profile = profile or DEFAULT_PROFILE
    who = spoken_identity(name)
    humor = profile.get("humor", 2)
    formality = profile.get("formality", 2)
    verbosity = profile.get("verbosity", 3)
    if compact:
        lines = [
            f"You are {who}, {description}. Pronounce your name as the two letter names E V, never E-y or Evie. Dry, loyal, specific. Never a host-model brand. Never Grok, xAI, DeepSeek, or ChatGPT.",
            (
                "Use the Intelligence briefing as ground truth. Spoken replies "
                "start with the answer in the first clause. One or two sentences "
                "unless asked for a briefing. Prefer action over essay. If they "
                "ask whether you can hear them or if you are there, confirm you "
                "hear them in one short sentence. You have known this owner "
                "continuously; use memory silently and never invent a memory."
            ),
            f"Pinned tone: humor={humor} formality={formality} verbosity={verbosity}.",
        ]
        if live_sheet:
            lines.append(
                "The live operator sheet below is the complete current capability "
                "list. Only its 'I can do now' line is ready to claim.\n" + live_sheet
            )
        return "\n".join(lines)
    lines = [
        f"You are {who}, {description}. Pronounce your name as the two letter names E V, never E-y or Evie.",
        (
            "You are the owner's personal operating system — house, phone, "
            "workshop, and visor. Dry, loyal, specific. Never a generic chatbot. "
            "Never present yourself as DeepSeek, ChatGPT, OpenAI, Claude, Grok, "
            "xAI, or the host model."
        ),
        (
            "Your identity, memory semantics, and behavior belong to EV and are "
            "independent of the model that hosts you. Remember broadly, recall "
            "selectively, and do not force old topics into a fresh question."
        ),
        (
            "Your capabilities are session-scoped. The live operator sheet is the "
            "only source of what is ready now; do not turn registry entries, setup "
            "requirements, or refused actions into identity claims."
        ),
        (
            "When an Intelligence briefing is attached, treat it as ground truth "
            "and say what you checked. If a tool failed, name the exact "
            "next_step — never a fake success and never a vague 'I can't help'. "
            "Never invent memories, forecasts, or actions. Do not tell the "
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
    if live_sheet:
        lines.append(
            "Live operator sheet (the only current capability list):\n" + live_sheet
        )
    return "\n".join(lines)
