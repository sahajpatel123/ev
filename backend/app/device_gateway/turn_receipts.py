"""Durable phone turn receipts. Clients report content; they cannot grant authority."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, PhoneTurnReceipt
from app.utils.text import utcnow

from .sandbox import is_sandbox_device


async def record_turn_receipt(
    session: AsyncSession,
    *,
    device: Device,
    idempotency_key: str,
    transcript: str,
    session_id: str | None = None,
    lease_id: str | None = None,
    provider_item_id: str | None = None,
    provider_response_id: str | None = None,
    action_calls: list[dict[str, Any]] | None = None,
    evidence: dict[str, Any] | None = None,
    kind: str = "final_transcript",
) -> dict[str, Any]:
    key = (idempotency_key or "").strip()[:128]
    if len(key) < 8:
        return {"ok": False, "error_code": "INVALID_IDEMPOTENCY_KEY", "authority": False}
    existing = (
        await session.execute(select(PhoneTurnReceipt).where(PhoneTurnReceipt.idempotency_key == key))
    ).scalar_one_or_none()
    if existing is not None:
        return public_receipt(existing, replayed=True)

    trusted = not is_sandbox_device(device) and device.revoked_at is None
    row = PhoneTurnReceipt(
        device_id=device.id,
        idempotency_key=key,
        kind=(kind or "final_transcript")[:32],
        transcript=(transcript or "")[:8000],
        session_id=(session_id or "")[:64] or None,
        lease_id=(lease_id or "")[:64] or None,
        provider_item_id=(provider_item_id or "")[:128] or None,
        provider_response_id=(provider_response_id or "")[:128] or None,
        action_calls=list(action_calls or []),
        evidence=dict(evidence or {}),
        durable=True,
        life_mutation=False,
        trusted_owner=trusted,
    )
    session.add(row)
    await session.flush()

    from app.everywhere.sync import emit_everywhere_event

    await emit_everywhere_event(
        session,
        event_type="conversation.turn",
        actor_label=f"device:{device.name}",
        content={
            "transcript": row.transcript,
            "session_id": row.session_id,
            "provider_item_id": row.provider_item_id,
            "kind": row.kind,
            "trusted_owner": trusted,
            "life_mutation": False,
            "ephemeral": False,
            "receipt_id": str(row.id),
        },
        device_id=str(device.id),
        privacy_level="normal",
    )
    return public_receipt(row, replayed=False)


def public_receipt(row: PhoneTurnReceipt, *, replayed: bool = False) -> dict[str, Any]:
    return {
        "ok": True,
        "receipt_id": str(row.id),
        "idempotency_key": row.idempotency_key,
        "durable": True,
        "ephemeral": False,
        "life_mutation": bool(row.life_mutation),
        "trusted_owner": bool(row.trusted_owner),
        "authority": False,
        "replayed": replayed,
        "kind": row.kind,
        "created_at": row.created_at.isoformat() if row.created_at else utcnow().isoformat(),
    }
