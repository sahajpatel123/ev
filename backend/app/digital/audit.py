"""Consequential Digital Operations audit — no credentials."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.digital.vault_bound import strip_secrets


async def record_op(
    session: Any,
    *,
    goal_id: str | None,
    service: str,
    operation: str,
    target: str | None,
    risk: str | None,
    result: str,
    verification: dict[str, Any] | None,
    approval_id: str | None = None,
) -> None:
    if session is None:
        return
    try:
        from app.models import DigitalOpAudit

        row = DigitalOpAudit(
            id=uuid4(),
            goal_id=(goal_id or "")[:64] or None,
            service=service[:32],
            operation=operation[:64],
            target=(target or "")[:256] or None,
            risk=(risk or "")[:8] or None,
            approval_id=(approval_id or "")[:64] or None,
            result=result[:32],
            verification=strip_secrets(verification or {}),
        )
        session.add(row)
        await session.flush()
    except Exception:
        return
