"""Durable phone-action records. In-memory maps may accelerate, not authorize."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PhoneActionRecord
from app.utils.text import utcnow


def _row_from_memory(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_id": str(record.get("action_id") or "")[:80],
        "device_id": str(record.get("device_id") or ""),
        "operation": str(record.get("operation") or "")[:64],
        "state": str(record.get("state") or "created")[:32],
        "result": str(record.get("result") or record.get("spoken") or "")[:64],
        "executed": bool(record.get("executed")),
        "verified": bool(record.get("verified")),
        "payload": dict(record),
        "idempotency_key": str(record.get("idempotency_key") or record.get("action_id") or "")[:128],
    }


async def upsert_action(session: AsyncSession, record: dict[str, Any]) -> PhoneActionRecord | None:
    data = _row_from_memory(record)
    if not data["action_id"]:
        return None
    row = (
        await session.execute(select(PhoneActionRecord).where(PhoneActionRecord.action_id == data["action_id"]))
    ).scalar_one_or_none()
    if row is None:
        from uuid import UUID

        try:
            device_id = UUID(str(data["device_id"]))
        except (ValueError, TypeError):
            return None
        row = PhoneActionRecord(
            action_id=data["action_id"],
            device_id=device_id,
            operation=data["operation"],
            state=data["state"],
            result=data["result"] or None,
            executed=data["executed"],
            verified=data["verified"],
            payload=data["payload"],
            idempotency_key=data["idempotency_key"] or None,
        )
        session.add(row)
    else:
        row.state = data["state"]
        row.result = data["result"] or row.result
        row.executed = data["executed"]
        row.verified = data["verified"]
        row.payload = data["payload"]
        row.updated_at = utcnow()
    await session.flush()
    return row


async def load_action(session: AsyncSession, action_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(select(PhoneActionRecord).where(PhoneActionRecord.action_id == action_id))
    ).scalar_one_or_none()
    if row is None:
        return None
    payload = dict(row.payload or {})
    payload.setdefault("action_id", row.action_id)
    payload.setdefault("state", row.state)
    payload.setdefault("executed", row.executed)
    payload.setdefault("verified", row.verified)
    return payload


def public_action(row: PhoneActionRecord) -> dict[str, Any]:
    return {
        "action_id": row.action_id,
        "device_id": str(row.device_id),
        "operation": row.operation,
        "state": row.state,
        "result": row.result,
        "executed": row.executed,
        "verified": row.verified,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
