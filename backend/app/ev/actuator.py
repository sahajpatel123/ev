"""Shared actuator mechanics for life I/O and one physical adapter.

Not a capability registry. Names, schemas, and scopes stay in TOOL_SPECS,
ACTION_SPECS, FLEET_TOOL_SPECS, and IntegrationRegistry. This module only
implements the side-effect contract: idempotency, timeout, cancellation,
evidence, and audit.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccessLog
from app.services.access_log import log_access
from app.utils.text import utcnow

DEFAULT_TIMEOUT_SECONDS = 10.0
CALL_IDEMPOTENCY_TTL = timedelta(seconds=30)
AUDIT_ACTION = "life_act"


def fingerprint(*parts: object) -> str:
    raw = "|".join("" if item is None else str(item).strip().lower() for item in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def evidence_base(
    *,
    source: str,
    accepted: bool,
    observed: bool | None = None,
    now: datetime | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "source": source,
        "timestamp": (now or utcnow()).isoformat(),
        "accepted": bool(accepted),
        "observed": bool(accepted if observed is None else observed),
    }
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


def timeout_result(*, spoken: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "timeout",
        "spoken": spoken or "That timed out. I will not claim it succeeded.",
    }


def cancelled_result(*, spoken: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "cancelled",
        "spoken": spoken or "Cancelled.",
    }


async def with_timeout[T](
    awaitable: Awaitable[T],
    *,
    seconds: float = DEFAULT_TIMEOUT_SECONDS,
    spoken: str | None = None,
) -> T | dict[str, Any]:
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except TimeoutError:
        return timeout_result(spoken=spoken)
    except asyncio.CancelledError:
        return cancelled_result()


async def with_retry[T](
    factory: Callable[[], Awaitable[T]],
    *,
    attempts: int = 2,
    delay_seconds: float = 0.05,
    retry_on: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError, OSError),
) -> T:
    last: BaseException | None = None
    for index in range(max(1, attempts)):
        try:
            return await factory()
        except retry_on as exc:
            last = exc
            if index + 1 >= attempts:
                raise
            await asyncio.sleep(delay_seconds)
    assert last is not None
    raise last


async def prior_result(
    session: AsyncSession,
    *,
    name: str,
    key: str,
    max_age: timedelta | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(AccessLog)
            .where(
                AccessLog.action == AUDIT_ACTION,
                AccessLog.resource_type == name[:32],
                AccessLog.request_id == key[:128],
            )
            .order_by(AccessLog.occurred_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return None
    clock = now or utcnow()
    occurred = row.occurred_at
    if occurred is not None and occurred.tzinfo is None and clock.tzinfo is not None:
        occurred = occurred.replace(tzinfo=clock.tzinfo)
    if max_age is not None and occurred is not None and clock - occurred > max_age:
        return None
    details = row.details or {}
    result = details.get("result")
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    replayed = dict(result)
    replayed["idempotent_replay"] = True
    return replayed


async def record_actuator(
    session: AsyncSession,
    *,
    name: str,
    actor: str,
    key: str | None,
    result: dict[str, Any],
    target: str | None = None,
) -> None:
    details = {
        "result": {
            key_name: value
            for key_name, value in result.items()
            if key_name not in {"hud", "raw", "blob"}
        },
        "ok": bool(result.get("ok")),
        "error": result.get("error"),
        "target": target,
    }
    await log_access(
        session,
        actor=actor,
        action=AUDIT_ACTION,
        endpoint=f"tool:{name}",
        resource_type=name[:32],
        resource_ids=[str(target)] if target else [name],
        request_id=(key or "")[:128] or None,
        details=details,
    )
