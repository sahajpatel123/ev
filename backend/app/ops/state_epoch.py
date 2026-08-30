"""G2 P0 — explicit STATE_EPOCH lineage identity (replaces event-derived epoch).

AUTHORITY LAW: the state/sync epoch is a server-owned persistent lineage
identifier in the ``state_epoch`` table. It is NOT derived from which Event
happens to be earliest, and it is stable through normal semantic activity,
restarts, deploys, migrations, and event pruning.

It changes ONLY for lineage-replacing operations: destructive restore,
wipe/reseed, disaster reconstruction, explicit canonical-history replacement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StateEpoch


async def get_current_epoch_id(session: AsyncSession) -> str | None:
    """Current lineage epoch id, or None when never initialized."""
    row = (
        await session.execute(
            select(StateEpoch).order_by(StateEpoch.created_at.desc()).limit(1)
        )
    ).scalars().first()
    return str(row.epoch_id) if row is not None else None


async def ensure_current_epoch(session: AsyncSession) -> str:
    """Return the current epoch, initializing the first lineage if absent."""
    current = await get_current_epoch_id(session)
    if current is not None:
        return current
    row = StateEpoch(
        epoch_id=uuid4(),
        reason="initial_lineage",
        environment=None,
    )
    session.add(row)
    await session.flush()
    return str(row.epoch_id)


async def rotate_epoch(session: AsyncSession, *, reason: str) -> str:
    """Lineage-replacing transition. MUST run inside the same transaction as
    the replacing operation so 'new database state + old epoch' is impossible."""
    previous = await get_current_epoch_id(session)
    row = StateEpoch(
        epoch_id=uuid4(),
        reason=str(reason)[:256],
        previous_epoch_id=UUID(previous) if previous else None,
    )
    session.add(row)
    await session.flush()
    return str(row.epoch_id)


def envelope_metadata(epoch_id: str | None, environment: str | None) -> dict[str, Any]:
    return {
        "state_epoch": epoch_id,
        "environment": environment,
        "recorded_at": datetime.now().astimezone().isoformat(),
    }
