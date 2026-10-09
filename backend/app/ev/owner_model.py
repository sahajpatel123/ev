"""Owner model store: versioned traits/values/thinking-style with provenance.

Mirrors ``app.ev.memory_ops`` correction/forget/restore semantics for the
three owner row kinds. Owner rows carry no embedding column; retrieval boosts
(Phase 3) operate on the memory side, never on fabricated owner vectors.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    OwnerModelEvent,
    OwnerStateSnapshot,
    OwnerThinkingStyle,
    OwnerTrait,
    OwnerValue,
)
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.utils.text import fingerprint, normalize_text

OwnerKind = Literal["trait", "value", "thinking_style"]

OwnerRowModel = OwnerTrait | OwnerValue | OwnerThinkingStyle

OWNER_ROW_MODELS: dict[str, type[OwnerTrait] | type[OwnerValue] | type[OwnerThinkingStyle]] = {
    "trait": OwnerTrait,
    "value": OwnerValue,
    "thinking_style": OwnerThinkingStyle,
}


def _model_for(kind: str):
    try:
        return OWNER_ROW_MODELS[kind]
    except KeyError:
        raise ValueError(f"Unknown owner row kind: {kind!r}") from None


def _row_fingerprint(kind: str, text: str) -> str:
    return fingerprint({"owner_kind": kind, "text": normalize_text(text)})


async def _copy_provenance(
    session: AsyncSession, kind: str, target_id: UUID, source_id: UUID
) -> None:
    rows = (
        await session.execute(
            select(OwnerModelEvent).where(
                OwnerModelEvent.row_kind == kind,
                OwnerModelEvent.row_id == source_id,
            )
        )
    ).scalars().all()
    for row in rows:
        session.add(
            OwnerModelEvent(row_kind=kind, row_id=target_id, event_id=row.event_id)
        )


async def write_owner_row(
    session: AsyncSession,
    kind: OwnerKind,
    text: str,
    *,
    source_event_id: UUID,
    importance: float = 0.5,
    confidence: float = 0.8,
    source_type: str = "inferred",
    privacy_level: str = "normal",
    event_time: datetime | None = None,
    payload: dict | None = None,
):
    """Create a v1 owner row, or return the current row on fingerprint dedupe."""
    model = _model_for(kind)
    digest = _row_fingerprint(kind, text)
    existing = (
        await session.execute(
            select(model)
            .where(model.fingerprint == digest, model.is_current.is_(True))
            .limit(1)
        )
    ).scalars().first()
    if existing is not None:
        return existing
    row = model(
        text=text,
        payload=dict(payload or {}),
        importance=importance,
        confidence=confidence,
        source_type=source_type,
        privacy_level=privacy_level,
        event_time=event_time or datetime.now(UTC),
        fingerprint=digest,
    )
    session.add(row)
    await session.flush()
    session.add(
        OwnerModelEvent(row_kind=kind, row_id=row.id, event_id=source_event_id)
    )
    return row


async def apply_owner_correction(
    session: AsyncSession,
    row,
    kind: OwnerKind,
    *,
    corrected_text: str,
    reason: str,
    event,
):
    """Create the corrected version from an already-recorded raw event."""
    model = _model_for(kind)
    new = model(
        text=corrected_text,
        payload={
            **row.payload,
            "corrected": True,
            "original_text": row.text,
            "original_row_id": str(row.id),
        },
        importance=row.importance,
        confidence=1.0,
        source_type="explicit",
        privacy_level=row.privacy_level,
        event_time=event.occurred_at,
        valid_from=event.occurred_at,
        version_group=row.version_group,
        version=row.version + 1,
        supersedes_id=row.id,
        reason_for_change=reason,
        fingerprint=_row_fingerprint(kind, corrected_text),
    )
    session.add(new)
    await session.flush()

    row.is_current = False
    row.superseded_by_id = new.id
    row.valid_until = event.occurred_at
    await _copy_provenance(session, kind, new.id, row.id)
    session.add(OwnerModelEvent(row_kind=kind, row_id=new.id, event_id=event.id))
    return new


async def apply_owner_forget(session: AsyncSession, row, *, reason: str, event):
    """Hide an owner row from active fetching using an already-recorded event."""
    row.is_current = False
    row.valid_until = event.occurred_at
    row.payload = {
        **row.payload,
        "forgotten": True,
        "forgotten_at": event.occurred_at.isoformat(),
        "forget_reason": reason,
    }
    return row


async def apply_owner_restore(session: AsyncSession, row, *, event):
    """Reverse a forget using an already-recorded raw event."""
    row.is_current = True
    row.valid_until = None
    row.payload = {
        **row.payload,
        "forgotten": False,
        "restored_at": event.occurred_at.isoformat(),
    }
    return row


async def _owner_event(
    session: AsyncSession,
    *,
    actor: str,
    event_type: str,
    text: str,
    row,
    kind: str,
    reason: str,
):
    return await EventService(session, actor=actor).create(
        EventCreate(
            source="owner",
            event_type=event_type,
            text=text,
            metadata={
                "owner_row_id": str(row.id),
                "owner_kind": kind,
                "reason": reason,
                "fingerprint": row.fingerprint,
            },
        )
    )


async def correct_owner_row(
    session: AsyncSession,
    kind: OwnerKind,
    row_id: UUID,
    *,
    corrected_text: str,
    reason: str = "owner correction",
    actor: str = "api",
):
    """Create a new current version with the correction; v1 stays intact."""
    model = _model_for(kind)
    row = await session.get(model, row_id)
    if row is None:
        raise KeyError(f"Owner {kind} row {row_id} not found")
    if not row.is_current:
        raise ValueError("Only the current owner row version can be corrected")
    event = await _owner_event(
        session,
        actor=actor,
        event_type="owner.correction",
        text=corrected_text,
        row=row,
        kind=kind,
        reason=reason,
    )
    return await apply_owner_correction(
        session, row, kind, corrected_text=corrected_text, reason=reason, event=event
    )


async def forget_owner_row(
    session: AsyncSession,
    kind: OwnerKind,
    row_id: UUID,
    *,
    reason: str = "owner requested",
    actor: str = "api",
):
    """Hide from active fetching; raw events and history are preserved."""
    model = _model_for(kind)
    row = await session.get(model, row_id)
    if row is None:
        raise KeyError(f"Owner {kind} row {row_id} not found")
    if not row.is_current:
        raise ValueError("Owner row is already inactive")
    event = await _owner_event(
        session,
        actor=actor,
        event_type="owner.forget",
        text=f"Forget: {row.text}",
        row=row,
        kind=kind,
        reason=reason,
    )
    return await apply_owner_forget(session, row, reason=reason, event=event)


async def restore_owner_row(
    session: AsyncSession,
    kind: OwnerKind,
    row_id: UUID,
    *,
    actor: str = "api",
):
    """Reverse a forget; history remains auditable."""
    model = _model_for(kind)
    row = await session.get(model, row_id)
    if row is None:
        raise KeyError(f"Owner {kind} row {row_id} not found")
    if row.is_current:
        return row
    event = await _owner_event(
        session,
        actor=actor,
        event_type="owner.restore",
        text=f"Restore: {row.text}",
        row=row,
        kind=kind,
        reason="owner requested restore",
    )
    return await apply_owner_restore(session, row, event=event)


async def current_owner_rows(session: AsyncSession, kind: OwnerKind) -> list:
    """Current, non-forgotten owner rows of one kind, importance-ordered."""
    model = _model_for(kind)
    return list(
        (
            await session.execute(
                select(model)
                .where(model.is_current.is_(True))
                .order_by(model.importance.desc(), model.created_time.asc())
            )
        ).scalars().all()
    )


async def row_source_events(
    session: AsyncSession, kind: OwnerKind, row_id: UUID
) -> list[UUID]:
    """Source event ids backing one owner row (provenance join)."""
    rows = (
        await session.execute(
            select(OwnerModelEvent.event_id).where(
                OwnerModelEvent.row_kind == kind,
                OwnerModelEvent.row_id == row_id,
            )
        )
    ).all()
    return [event_id for (event_id,) in rows]


async def record_owner_state(
    session: AsyncSession,
    *,
    state_kind: str,
    label: str,
    details: dict | None = None,
    confidence: float = 0.5,
    source_type: str = "inferred",
    privacy_level: str = "normal",
    ttl_s: int | None = None,
) -> OwnerStateSnapshot:
    """Write a TTL'd state snapshot. Device-local; never synced, never trained on."""
    now = datetime.now(UTC)
    snapshot = OwnerStateSnapshot(
        state_kind=state_kind,
        label=label,
        details=dict(details or {}),
        confidence=confidence,
        source_type=source_type,
        privacy_level=privacy_level,
        created_time=now,
        expires_at=now + timedelta(seconds=ttl_s if ttl_s is not None else settings.owner_state_ttl_s),
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def current_owner_state(session: AsyncSession) -> OwnerStateSnapshot | None:
    """Latest unexpired state snapshot, or None when absent/expired."""
    now = datetime.now(UTC)
    return (
        await session.execute(
            select(OwnerStateSnapshot)
            .where(OwnerStateSnapshot.expires_at > now)
            .order_by(OwnerStateSnapshot.created_time.desc())
            .limit(1)
        )
    ).scalars().first()
