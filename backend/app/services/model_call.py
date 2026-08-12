"""Audit logging for model calls through the gateway."""

from __future__ import annotations

import math
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.costs import actual_cost_usd
from app.gateway.service import GatewayCall
from app.models import ModelCallLog
from app.utils.text import utcnow

MEMORY_TEXT_LOG_LIMIT = 160


async def log_model_call(
    session: AsyncSession,
    *,
    call: GatewayCall,
    actor: str,
) -> ModelCallLog:
    """Persist one auditable model call with its envelope and validation outcome."""

    usage = call.result.usage or {}
    envelope_hash = call.envelope.metadata.get("envelope_hash")
    cost_usd = actual_cost_usd(
        provider=call.provider,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
    )
    envelope_dict = call.envelope.to_dict(memory_text_limit=MEMORY_TEXT_LOG_LIMIT)
    envelope_dict.setdefault("metadata", {})["cost_usd"] = cost_usd
    if call.degraded:
        envelope_dict["metadata"]["degraded"] = True
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
        envelope=envelope_dict,
        envelope_hash=envelope_hash,
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


def _summarize(rows: list[ModelCallLog]) -> dict:
    """One latency/error/token summary bucket (totals or per provider/model)."""

    n = len(rows)
    latencies = sorted(r.latency_ms for r in rows)
    p95 = latencies[min(n - 1, max(0, math.ceil(0.95 * n) - 1))] if n else 0.0
    media_refs = [
        ref
        for row in rows
        for ref in ((row.envelope or {}).get("media_refs") or [])
    ]
    return {
        "calls": n,
        "errors": sum(1 for r in rows if r.status == "error"),
        "blocked": sum(1 for r in rows if r.status == "blocked"),
        "avg_latency_ms": round(sum(latencies) / n, 1) if n else 0.0,
        "p95_latency_ms": round(p95, 1) if n else 0.0,
        "prompt_tokens": sum(r.prompt_tokens or 0 for r in rows),
        "completion_tokens": sum(r.completion_tokens or 0 for r in rows),
        "media_refs": len(media_refs),
        "raw_media_sent": sum(1 for ref in media_refs if ref.get("raw")),
        "derived_media_only": sum(1 for ref in media_refs if not ref.get("raw")),
    }


async def model_call_stats(session: AsyncSession, *, window_hours: int = 24) -> dict:
    """Aggregate the model-call audit trail into routing/eval evidence.

    Routing between fast and deep providers stays gated by evaluation; this is
    the evidence base that evaluation consumes (latency, errors, tokens per
    provider/model).
    """

    cutoff = utcnow() - timedelta(hours=window_hours)
    rows = list(
        (
            await session.execute(
                select(ModelCallLog).where(ModelCallLog.created_at >= cutoff)
            )
        ).scalars().all()
    )
    buckets: dict[tuple[str, str], list[ModelCallLog]] = {}
    for row in rows:
        buckets.setdefault((row.provider, row.model or "unknown"), []).append(row)
    return {
        "generated_at": utcnow().isoformat(),
        "window_hours": window_hours,
        "totals": _summarize(rows),
        "by_provider_model": [
            {"provider": provider, "model": model, **_summarize(bucket)}
            for (provider, model), bucket in sorted(buckets.items())
        ],
    }
