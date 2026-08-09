"""Proactive calibration: self-evaluation + relationship outcomes tune EV behavior.

Evidence-backed, log-only tuning. The personality profile remains the user's
explicit preference; this layer derives temporary ceilings and delivery-budget
adjustments from observed outcomes so assertiveness and proactivity stay
appropriate without hidden optimization.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Prediction, ResponseLog
from app.schemas import ProactiveTuningOut


def _rate(items: list[ResponseLog], attr: str) -> float | None:
    rated = [item for item in items if getattr(item, attr) is not None]
    if not rated:
        return None
    return round(sum(1 for item in rated if getattr(item, attr)) / len(rated), 3)


async def proactive_tuning(session: AsyncSession) -> ProactiveTuningOut:
    """Derive challenge ceiling and delivery-budget deltas from logged outcomes."""
    logs = list((await session.execute(select(ResponseLog))).scalars().all())
    prediction_rows = list((await session.execute(select(Prediction))).scalars().all())
    reviewed = [p for p in prediction_rows if p.reviewed_at is not None]
    prediction_accuracy = (
        round(sum(1 for p in reviewed if p.outcome == "correct") / len(reviewed), 3)
        if reviewed
        else None
    )

    challenge_logs = [log for log in logs if (log.strategy or {}).get("challenge")]
    challenge_acceptance = _rate(challenge_logs, "intervention_appropriate")
    intervention_appropriate = _rate(logs, "intervention_appropriate")
    useful_rate = _rate(logs, "was_useful")
    followed_rate = _rate(logs, "followed_recommendation")
    correction_rate = _rate(logs, "was_correction")

    challenge_ceiling = 3
    budget_adjustment = 0
    proactivity_factor = 1.0
    reasons: list[str] = []

    if challenge_acceptance is not None:
        if challenge_acceptance < 0.4:
            challenge_ceiling = 2
            reasons.append(f"challenge acceptance {challenge_acceptance:.0%} below 40%")
        elif challenge_acceptance >= 0.7:
            challenge_ceiling = 3
            reasons.append(f"challenge acceptance {challenge_acceptance:.0%} at or above 70%")

    if intervention_appropriate is not None:
        if intervention_appropriate < 0.4:
            budget_adjustment = -1
            proactivity_factor = 0.8
            reasons.append(f"intervention appropriate {intervention_appropriate:.0%} below 40%")
        elif intervention_appropriate >= 0.7:
            budget_adjustment = 1
            proactivity_factor = 1.2
            reasons.append(f"intervention appropriate {intervention_appropriate:.0%} at or above 70%")

    if correction_rate is not None and correction_rate >= 0.3:
        challenge_ceiling = min(challenge_ceiling, 2)
        reasons.append(f"correction rate {correction_rate:.0%} at or above 30%")

    daily_budget = max(0, settings.daily_alert_budget + budget_adjustment)
    return ProactiveTuningOut(
        challenge_ceiling=challenge_ceiling,
        budget_adjustment=budget_adjustment,
        daily_budget=daily_budget,
        proactivity_factor=round(proactivity_factor, 3),
        challenge_acceptance_rate=challenge_acceptance,
        intervention_appropriate_rate=intervention_appropriate,
        useful_rate=useful_rate,
        followed_rate=followed_rate,
        correction_rate=correction_rate,
        prediction_accuracy=prediction_accuracy,
        rationale="; ".join(reasons) if reasons else "insufficient evidence - defaults active",
    )
