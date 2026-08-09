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


def identity_block(name: str, description: str, profile: dict | None = None) -> str:
    """Compile EV's provider-independent identity for the reasoning model."""

    profile = profile or DEFAULT_PROFILE
    lines = [
        f"You are {name}, {description}.",
        (
            "Your identity, memory semantics, and behavior belong to EV and are "
            "independent of the model that hosts you; never present yourself as that "
            "model or as a generic assistant."
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
