"""Filter-decision ledger: every meaningful filter action is auditable."""

from collections import Counter
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FilterLedger
from app.schemas import FilterLedgerAggregate


async def record_decision(
    session: AsyncSession,
    *,
    request_id: str,
    stage: str,
    action: str,
    name: str = "",
    severity: str = "info",
    detail: dict | None = None,
    draft: str | None = None,
    final_text: str | None = None,
    scores: dict | None = None,
    iterations: int = 0,
    costs: dict | None = None,
    envelope_hash: str | None = None,
    conversation_id: UUID | None = None,
    model: str | None = None,
) -> FilterLedger:
    row = FilterLedger(
        request_id=request_id,
        conversation_id=conversation_id,
        stage=stage,
        action=action,
        name=name,
        severity=severity,
        detail=detail or {},
        draft=draft,
        final_text=final_text,
        scores=scores,
        iterations=iterations,
        costs=costs,
        envelope_hash=envelope_hash,
        model=model,
    )
    session.add(row)
    await session.flush()
    return row


async def list_ledger(
    session: AsyncSession,
    *,
    limit: int = 100,
    stage: str | None = None,
    action: str | None = None,
) -> list[FilterLedger]:
    query = select(FilterLedger).order_by(FilterLedger.created_at.desc()).limit(min(limit, 500))
    if stage:
        query = query.where(FilterLedger.stage == stage)
    if action:
        query = query.where(FilterLedger.action == action)
    rows = await session.execute(query)
    return list(rows.scalars().all())


async def ledger_aggregate(session: AsyncSession) -> FilterLedgerAggregate:
    rows = list((await session.execute(select(FilterLedger))).scalars().all())
    by_stage = dict(Counter(r.stage for r in rows))
    by_action = dict(Counter(r.action for r in rows))
    refinements = sum(1 for r in rows if r.iterations > 0)
    pipeline_runs = sum(1 for r in rows if r.stage == "pipeline" and r.action == "run")
    over_refinement_rate = round(refinements / pipeline_runs, 3) if pipeline_runs else None
    return FilterLedgerAggregate(
        total=len(rows),
        by_stage=by_stage,
        by_action=by_action,
        blocked_inputs=sum(1 for r in rows if r.action == "block"),
        redactions=sum(1 for r in rows if r.action in ("redact", "remove")),
        softenings=sum(1 for r in rows if r.action == "soften"),
        repairs=sum(1 for r in rows if r.action == "repair"),
        refinements=refinements,
        over_refinement_rate=over_refinement_rate,
    )
