"""Intelligence filter API: draft evaluation/replay and ledger inspection."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor
from app.db import get_session
from app.ev.interaction import build_strategy
from app.filter.envelope import SpeakerIdentity
from app.filter.input_filter import InputFilter
from app.filter.ledger import ledger_aggregate, list_ledger, record_decision
from app.filter.output_filter import run_output_filter
from app.filter.policy import active_policy
from app.gateway.providers import get_chat_provider
from app.schemas import (
    FilterEvaluateRequest,
    FilterEvaluateResponse,
    FilterLedgerAggregate,
    FilterLedgerOut,
    FilterReportOut,
)

router = APIRouter(prefix="/v1/filter")


@router.post("/evaluate", response_model=FilterEvaluateResponse)
async def evaluate_filter(
    data: FilterEvaluateRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> FilterEvaluateResponse:
    """Run the input filter on a message and the output filter on a draft.

    When ``draft`` is omitted, the full provider pipeline runs (input filter →
    gateway → output filter → ledger). This is the replay/tuning surface for the
    filter: any draft can be evaluated against the same rules that guard chat.
    """

    from app.filter.pipeline import run_full_filter_pipeline

    request_id = str(uuid4())
    speaker = SpeakerIdentity(
        actor_id=actor,
        verified=True,
        confidence=1.0,
        method="auth_token",
    )
    if data.draft is not None:
        policy = await active_policy(session)
        input_filter = InputFilter(session)
        decision, memories, grounding, _ = await input_filter.run(
            message=data.message,
            speaker=speaker,
            k=50,
            policy=policy,
        )
        ledger_ids = []
        await record_decision(
            session,
            request_id=request_id,
            conversation_id=data.conversation_id,
            stage="input",
            action="block" if decision.blocked else "run",
            name="input_filter",
            severity="high" if decision.blocked else "info",
            detail={"flags": [f.to_dict() for f in decision.flags]},
            draft=data.message,
            final_text=decision.provider_message,
        )
        strategy = build_strategy(decision.provider_message)
        report = await run_output_filter(
            data.draft,
            strategy=strategy,
            grounding=grounding,
            policy=policy,
        )
        for flag in report.flags:
            if flag.action != "allow":
                ledger_ids.append(
                    (
                        await record_decision(
                            session,
                            request_id=request_id,
                            conversation_id=data.conversation_id,
                            stage="output",
                            action=flag.action,
                            name=flag.name,
                            severity=flag.severity,
                            detail={"flag": flag.detail},
                            draft=report.draft,
                            final_text=report.final_text,
                            scores=report.critic,
                            iterations=report.iterations,
                        )
                    ).id
                )
        pipeline_row = await record_decision(
            session,
            request_id=request_id,
            conversation_id=data.conversation_id,
            stage="pipeline",
            action="run",
            name="intelligence_filter",
            detail={
                "mode": "draft_replay",
                "claims": [c.to_dict() for c in report.claims],
                "iterations": report.iterations,
                "passed": report.passed,
            },
            final_text=report.final_text,
            scores=report.critic,
        )
        ledger_ids.append(pipeline_row.id)
        await session.commit()
        return FilterEvaluateResponse(
            request_id=request_id,
            input=decision.to_dict(),
            output=FilterReportOut.model_validate(report.to_dict()),
            ledger_ids=ledger_ids,
            blocked=decision.blocked,
            block_reason=decision.block_reason,
        )

    run = await run_full_filter_pipeline(
        session,
        message=data.message,
        provider=get_chat_provider(),
        speaker=speaker,
        request_id=request_id,
        conversation_id=data.conversation_id,
    )
    return FilterEvaluateResponse(
        request_id=request_id,
        input=run.input_decision.to_dict() if run.input_decision else {},
        output=FilterReportOut.model_validate(run.report.to_dict()) if run.report else None,
        context_tokens=run.context_tokens,
        ledger_ids=run.ledger_ids,
        blocked=run.blocked,
        block_reason=run.block_reason,
    )


@router.get("/ledger", response_model=list[FilterLedgerOut])
async def get_ledger(
    limit: int = Query(default=100, ge=1, le=500),
    stage: str | None = None,
    action: str | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[FilterLedgerOut]:
    rows = await list_ledger(session, limit=limit, stage=stage, action=action)
    return [FilterLedgerOut.model_validate(row) for row in rows]


@router.get("/ledger/aggregate", response_model=FilterLedgerAggregate)
async def get_ledger_aggregate(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> FilterLedgerAggregate:
    return await ledger_aggregate(session)
