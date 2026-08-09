"""Self-evaluation: response log + aggregate calibration signals."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ResponseLog
from app.schemas import EvaluationUpdate, SelfEvalAggregate


async def log_response(
    session: AsyncSession,
    *,
    request_text: str,
    reply_text: str,
    mode: str,
    strategy: dict,
    provenance_ids: list[str],
    context_tokens: int,
    model: str | None,
) -> ResponseLog:
    row = ResponseLog(
        request_text=request_text,
        reply_text=reply_text,
        mode=mode,
        strategy=strategy,
        provenance_ids=provenance_ids,
        context_tokens=context_tokens,
        model=model,
    )
    session.add(row)
    await session.flush()
    return row


async def update_evaluation(
    session: AsyncSession,
    response_id: UUID,
    data: EvaluationUpdate,
) -> ResponseLog:
    row = await session.get(ResponseLog, response_id)
    if row is None:
        raise KeyError(f"Response log {response_id} not found")
    if data.was_useful is not None:
        row.was_useful = data.was_useful
    if data.followed_recommendation is not None:
        row.followed_recommendation = data.followed_recommendation
    if data.was_correction is not None:
        row.was_correction = data.was_correction
    if data.intervention_appropriate is not None:
        row.intervention_appropriate = data.intervention_appropriate
    return row


async def aggregate(session: AsyncSession) -> SelfEvalAggregate:
    rows = list((await session.execute(select(ResponseLog))).scalars().all())

    def rate(items: list[ResponseLog], attr: str) -> float | None:
        rated = [item for item in items if getattr(item, attr) is not None]
        if not rated:
            return None
        return round(sum(1 for item in rated if getattr(item, attr)) / len(rated), 3)

    by_mode: dict[str, dict] = {}
    modes = sorted({row.mode for row in rows})
    for mode in modes:
        mode_rows = [row for row in rows if row.mode == mode]
        by_mode[mode] = {
            "count": len(mode_rows),
            "useful_rate": rate(mode_rows, "was_useful"),
            "followed_rate": rate(mode_rows, "followed_recommendation"),
            "correction_rate": rate(mode_rows, "was_correction"),
            "intervention_appropriate_rate": rate(mode_rows, "intervention_appropriate"),
        }
    return SelfEvalAggregate(
        total=len(rows),
        useful_rate=rate(rows, "was_useful"),
        followed_rate=rate(rows, "followed_recommendation"),
        correction_rate=rate(rows, "was_correction"),
        intervention_appropriate_rate=rate(rows, "intervention_appropriate"),
        by_mode=by_mode,
    )


async def list_logs(session: AsyncSession, *, limit: int = 50) -> list[ResponseLog]:
    result = await session.execute(
        select(ResponseLog).order_by(ResponseLog.created_at.desc()).limit(min(limit, 200))
    )
    return list(result.scalars().all())

