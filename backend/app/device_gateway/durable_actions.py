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


async def reconcile_recovered_actions(
    session: AsyncSession, *, device_id: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """A prepared turn receipt is not the current action state."""
    import time

    from .mobile_actions.store import get_action, public_row

    terminal = {"cancelled", "executed", "failed", "expired"}
    resolved: dict[str, dict[str, Any]] = {}

    async def reconcile(action: dict[str, Any]) -> dict[str, Any]:
        action_id = str(action.get("action_id") or "")
        if action_id in resolved:
            return resolved[action_id]
        durable = await load_action(session, action_id)
        memory = get_action(action_id)
        owned = [row for row in (durable, memory) if row is not None and str(row.get("device_id")) == device_id]
        row = next((row for row in owned if row.get("state") in terminal), memory if memory in owned else durable if durable in owned else None)
        state = str((row or {}).get("state") or "expired")
        if row is not None and state not in terminal and float(row.get("exp") or 0) <= time.time():
            state = "expired"
        if row is None or state in terminal:
            prior_card = action.get("card") if isinstance(action.get("card"), dict) else {}
            card = {key: prior_card[key] for key in ("title", "target", "body", "device_label", "operation") if key in prior_card}
            card.update(action_id=action_id, status=state, native_execute=False)
            result = {
                "ok": state in {"cancelled", "executed"}, "action_id": action_id,
                "status": state, "card": card, "recovered": True,
                "native_execute": False, "executed": state == "executed",
                "verified": bool((row or {}).get("receipt", {}).get("verified")) if isinstance((row or {}).get("receipt"), dict) else False,
                "receipt": public_row(row) if row is not None else {"state": state},
            }
        else:
            result = {**action, "recovered": True}
        resolved[action_id] = result
        return result

    result = dict(payload)
    if isinstance(result.get("phone_action"), dict):
        result["phone_action"] = await reconcile(result["phone_action"])
    if isinstance(result.get("phone_actions"), list):
        result["phone_actions"] = [await reconcile(action) for action in result["phone_actions"] if isinstance(action, dict)]
    return result
