from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AccessLog


async def log_access(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    endpoint: str | None = None,
    resource_type: str | None = None,
    resource_ids: list[str | UUID] | None = None,
    request_id: str | None = None,
    details: dict | None = None,
) -> None:
    if not settings.access_log_enabled:
        return
    session.add(
        AccessLog(
            actor=actor,
            action=action,
            endpoint=endpoint,
            resource_type=resource_type,
            resource_ids=[str(r) for r in (resource_ids or [])],
            request_id=request_id,
            details=details or {},
        )
    )

