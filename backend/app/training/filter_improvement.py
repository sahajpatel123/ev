"""Filter self-improvement driven by the filter/decision ledger (7.5).

Aggregates ledger decisions (blocks, redactions, softenings, repairs,
over-refinement) with user correction/usefulness signals and derives
deterministic, evidence-backed threshold proposals. Reports are versioned,
consent-gated, rollback-able, and erasable — calibration is proposed, never
silently applied.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.filter.ledger import ledger_aggregate
from app.models import FilterRecalibration, Prediction, ResponseLog
from app.services.access_log import log_access
from app.training.consent import require_consent

TRACK = "filter_self_improvement"


def _rate(items: list[ResponseLog], attr: str) -> float | None:
    rated = [item for item in items if getattr(item, attr) is not None]
    if not rated:
        return None
    return round(sum(1 for item in rated if getattr(item, attr)) / len(rated), 3)


async def gather_metrics(session: AsyncSession) -> dict:
    aggregate = await ledger_aggregate(session)
    metrics = aggregate.model_dump()

    logs = list((await session.execute(select(ResponseLog))).scalars().all())
    metrics["correction_rate"] = _rate(logs, "was_correction")
    metrics["useful_rate"] = _rate(logs, "was_useful")
    metrics["followed_rate"] = _rate(logs, "followed_recommendation")
    metrics["intervention_appropriate_rate"] = _rate(logs, "intervention_appropriate")

    predictions = list(
        (
            await session.execute(
                select(Prediction).where(Prediction.reviewed_at.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    metrics["prediction_accuracy"] = (
        round(sum(1 for p in predictions if p.outcome == "correct") / len(predictions), 3)
        if predictions
        else None
    )
    return metrics


def derive_proposals(metrics: dict) -> list[dict]:
    """Deterministic threshold proposals from ledger + user-signal metrics."""
    proposals: list[dict] = []
    total = int(metrics.get("total", 0))
    over_refinement_rate = metrics.get("over_refinement_rate")
    if over_refinement_rate is not None and over_refinement_rate > 0.02:
        proposals.append(
            {
                "name": "critic_iterations_cap",
                "direction": "decrease",
                "current_value": over_refinement_rate,
                "target_value": 0.02,
                "rationale": "over-refinement above 2% erodes trust and adds latency",
                "evidence": {"over_refinement_rate": over_refinement_rate},
            }
        )

    block_rate = metrics.get("blocked_inputs", 0) / total if total else None
    if block_rate is not None and block_rate > 0.05:
        proposals.append(
            {
                "name": "input_guard_severity",
                "direction": "increase",
                "current_value": round(block_rate, 3),
                "target_value": 0.05,
                "rationale": "input block rate above 5% suggests under-tuned guard",
                "evidence": {
                    "blocked_inputs": metrics.get("blocked_inputs", 0),
                    "total": total,
                },
            }
        )

    correction_rate = metrics.get("correction_rate")
    if correction_rate is not None and correction_rate >= 0.3:
        proposals.append(
            {
                "name": "grounding_min_evidence",
                "direction": "increase",
                "current_value": correction_rate,
                "target_value": 0.3,
                "rationale": "high correction rate means grounding/persona drift",
                "evidence": {"correction_rate": correction_rate},
            }
        )

    useful_rate = metrics.get("useful_rate")
    if useful_rate is not None and useful_rate < 0.5:
        proposals.append(
            {
                "name": "persona_style_enforcement",
                "direction": "increase",
                "current_value": useful_rate,
                "target_value": 0.5,
                "rationale": "usefulness below 50% suggests style/length mismatch",
                "evidence": {"useful_rate": useful_rate},
            }
        )

    prediction_accuracy = metrics.get("prediction_accuracy")
    if prediction_accuracy is not None and prediction_accuracy < 0.5:
        proposals.append(
            {
                "name": "ev_sense_confidence_floor",
                "direction": "increase",
                "current_value": prediction_accuracy,
                "target_value": 0.5,
                "rationale": "prediction accuracy below 50% warrants a higher confidence floor",
                "evidence": {"prediction_accuracy": prediction_accuracy},
            }
        )
    return proposals


async def current_recalibration(session: AsyncSession) -> FilterRecalibration | None:
    result = await session.execute(
        select(FilterRecalibration)
        .where(FilterRecalibration.is_current.is_(True))
        .order_by(FilterRecalibration.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def recalibrate(
    session: AsyncSession,
    *,
    actor: str,
    reason: str | None = None,
) -> FilterRecalibration:
    consent = await require_consent(session, TRACK)
    metrics = await gather_metrics(session)
    proposals = derive_proposals(metrics)
    current = await current_recalibration(session)
    max_version = max(
        (
            (
                await session.execute(select(FilterRecalibration.version))
            )
            .scalars()
            .all()
        ),
        default=0,
    )
    if current is not None:
        current.is_current = False
    row = FilterRecalibration(
        version=max_version + 1,
        is_current=True,
        metrics=metrics,
        proposals=proposals,
        reason_for_change=reason or "monthly ledger-driven recalibration report",
        consent_id=consent.id,
        supersedes_id=current.id if current is not None else None,
    )
    session.add(row)
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="filter_recalibrate",
        endpoint="POST /v1/training/filter/self-improve",
        resource_type="filter_recalibration",
        resource_ids=[row.id],
        details={
            "version": row.version,
            "proposals": len(proposals),
            "applied": False,
        },
    )
    return row


async def list_recalibrations(session: AsyncSession) -> list[FilterRecalibration]:
    result = await session.execute(
        select(FilterRecalibration).order_by(FilterRecalibration.version.desc())
    )
    return list(result.scalars().all())


async def get_recalibration(
    session: AsyncSession, version: int
) -> FilterRecalibration:
    result = await session.execute(
        select(FilterRecalibration).where(FilterRecalibration.version == version)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise KeyError(f"Filter recalibration version {version} not found")
    return row


async def rollback(
    session: AsyncSession,
    *,
    target_version: int,
    actor: str,
    reason: str | None = None,
) -> FilterRecalibration:
    await require_consent(session, TRACK)
    target = await get_recalibration(session, target_version)
    current = await current_recalibration(session)
    if current is not None and current.id != target.id:
        current.is_current = False
    target.is_current = True
    if reason:
        target.reason_for_change = reason
    await log_access(
        session,
        actor=actor,
        action="filter_recalibration_rollback",
        endpoint="POST /v1/training/filter/recalibration/rollback",
        resource_type="filter_recalibration",
        resource_ids=[target.id],
        details={"target_version": target_version},
    )
    return target


async def delete_all(session: AsyncSession, *, actor: str, reason: str) -> int:
    rows = list((await session.execute(select(FilterRecalibration))).scalars().all())
    for row in rows:
        row.metrics = {}
        row.proposals = []
        row.is_current = False
        row.redacted = True
        row.reason_for_change = f"{reason} (redacted)"
    await log_access(
        session,
        actor=actor,
        action="filter_recalibration_delete",
        endpoint="POST /v1/training/filter/recalibration/delete",
        resource_type="filter_recalibration",
        resource_ids=[r.id for r in rows],
        details={"redacted": len(rows), "reason": reason},
    )
    return len(rows)
