"""Enforceable monthly cost meter for model calls (CORTEX follow-up 5).

The observability gate asserts a $40/month cost budget; this module makes that
budget *enforceable*: before a provider call, the gateway projects the request
cost from the prompt (plus a conservative completion ceiling) and refuses when
the current calendar-month spend plus the projection exceeds the configured
cap. Actual usage is measured after every call and recorded in the audit
envelope via ``log_model_call``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.contracts import ChatMessage
from app.models import ModelCallLog
from app.ops.metrics import estimate_cost_usd
from app.utils.text import utcnow


class CostCapExceeded(RuntimeError):
    """A request was refused because the monthly cost cap would be exceeded."""


def estimate_prompt_tokens(messages: Sequence[ChatMessage]) -> int:
    """Cheap deterministic prompt-token estimate (chars / 4, like echo)."""

    return sum(len(message.content) // 4 for message in messages)


def projected_request_cost_usd(
    *,
    provider: str,
    messages: Sequence[ChatMessage] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    estimated_completion_tokens: int | None = None,
) -> float:
    """Project one request's cost before it is made (conservative)."""

    if messages is not None:
        prompt_tokens = max(prompt_tokens, estimate_prompt_tokens(messages))
    completion = completion_tokens or estimated_completion_tokens or (
        settings.model_estimated_max_completion_tokens
    )
    return estimate_cost_usd(
        provider=provider,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion,
    )


def month_start(now: datetime | None = None) -> datetime:
    """First instant of the current UTC calendar month."""

    now = now or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def monthly_cost_usd(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> float:
    """Estimated USD spent on model calls in the current calendar month."""

    start = month_start(now)
    rows = list(
        (
            await session.execute(
                select(ModelCallLog).where(ModelCallLog.created_at >= start)
            )
        ).scalars().all()
    )
    return round(
        sum(
            estimate_cost_usd(
                provider=row.provider,
                prompt_tokens=row.prompt_tokens or 0,
                completion_tokens=row.completion_tokens or 0,
            )
            for row in rows
        ),
        6,
    )


async def check_cost_cap(
    session: AsyncSession,
    *,
    provider: str,
    messages: Sequence[ChatMessage] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    estimated_completion_tokens: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Raise :class:`CostCapExceeded` when the projected spend breaks the cap."""

    if not settings.cost_cap_enabled:
        return {
            "enforced": False,
            "monthly_cost_usd": 0.0,
            "cap_usd": settings.monthly_cost_cap_usd,
            "projected_request_usd": 0.0,
        }
    used = await monthly_cost_usd(session, now=now)
    projected = projected_request_cost_usd(
        provider=provider,
        messages=messages,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_completion_tokens=estimated_completion_tokens,
    )
    if used + projected > settings.monthly_cost_cap_usd:
        raise CostCapExceeded(
            f"monthly cost cap exceeded: ${used:.2f} used + ~${projected:.2f} "
            f"projected > ${settings.monthly_cost_cap_usd:.2f} cap"
        )
    return {
        "enforced": True,
        "monthly_cost_usd": used,
        "cap_usd": settings.monthly_cost_cap_usd,
        "projected_request_usd": projected,
    }


def actual_cost_usd(
    *,
    provider: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Measured cost of one completed call from its real usage."""

    return estimate_cost_usd(
        provider=provider,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
