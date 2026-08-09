"""Audit logging for model calls through the gateway."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.service import GatewayCall
from app.models import ModelCallLog

MEMORY_TEXT_LOG_LIMIT = 160


async def log_model_call(
    session: AsyncSession,
    *,
    call: GatewayCall,
    actor: str,
) -> ModelCallLog:
    """Persist one auditable model call with its envelope and validation outcome."""

    usage = call.result.usage or {}
    row = ModelCallLog(
        request_id=call.request_id,
        actor=actor,
        provider=call.provider,
        model=call.model,
        status=call.status,
        latency_ms=call.latency_ms,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        tool_calls=call.tool_calls_dict(),
        envelope=call.envelope.to_dict(memory_text_limit=MEMORY_TEXT_LOG_LIMIT),
        error=call.error,
    )
    session.add(row)
    await session.flush()
    return row


async def list_model_calls(
    session: AsyncSession,
    *,
    limit: int = 50,
    request_id: str | None = None,
) -> list[ModelCallLog]:
    query = select(ModelCallLog).order_by(ModelCallLog.created_at.desc()).limit(min(limit, 200))
    if request_id:
        query = query.where(ModelCallLog.request_id == request_id)
    result = await session.execute(query)
    return list(result.scalars().all())
