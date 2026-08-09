"""Consent lifecycle for training/personalization tracks.

Every training track requires explicit, revocable consent. Revoking consent
disables the track; deleting biometric data is handled by the voice service.
"""

from __future__ import annotations

from typing import get_args

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConsentRecord
from app.schemas import TrainingTrack
from app.utils.text import utcnow

TRACKS: set[str] = set(get_args(TrainingTrack))


class ConsentRequiredError(Exception):
    """Raised when a training operation is attempted without active consent."""


async def active_consent(session: AsyncSession, track: str) -> ConsentRecord | None:
    result = await session.execute(
        select(ConsentRecord)
        .where(ConsentRecord.track == track, ConsentRecord.revoked_at.is_(None))
        .order_by(ConsentRecord.granted_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def require_consent(session: AsyncSession, track: str) -> ConsentRecord:
    row = await active_consent(session, track)
    if row is None:
        raise ConsentRequiredError(track)
    return row


async def grant_consent(
    session: AsyncSession,
    *,
    track: str,
    purpose: str,
    scope: dict,
    source: str,
    consent_version: str = "1.0",
) -> ConsentRecord:
    if track not in TRACKS:
        raise ValueError(f"Unknown training track: {track}")
    existing = await active_consent(session, track)
    if existing is not None:
        return existing
    row = ConsentRecord(
        track=track,
        purpose=purpose,
        scope=scope,
        source=source,
        consent_version=consent_version,
    )
    session.add(row)
    await session.flush()
    return row


async def revoke_consent(
    session: AsyncSession,
    *,
    track: str,
    reason: str,
) -> ConsentRecord | None:
    row = await active_consent(session, track)
    if row is None:
        return None
    row.revoked_at = utcnow()
    row.revoked_reason = reason
    return row


async def list_consents(session: AsyncSession) -> list[ConsentRecord]:
    result = await session.execute(select(ConsentRecord).order_by(ConsentRecord.granted_at.desc()))
    return list(result.scalars().all())
