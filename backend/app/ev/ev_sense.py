"""EV Sense: prediction candidates with intervention scoring and 'why now?' rationale."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from dateutil import parser as date_parser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev import live
from app.ev.companionship import scan_isolation
from app.ev.decisions import find_decision_loops, followups_due
from app.ev.maker import reorder_items
from app.models import Alert, HealthSnapshot, Memory, Prediction, WatchlistItem
from app.schemas import SensePrediction
from app.utils.text import fingerprint, normalize_text, utcnow

Tier = Literal["do_nothing", "mention_later", "notify", "notify_card"]


def intervention_tier(score: float) -> tuple[Tier, bool]:
    if score < 0.15:
        return "do_nothing", False
    if score < 0.35:
        return "mention_later", False
    if score < 0.60:
        return "notify", True
    return "notify_card", True


def _score(*, importance: float, urgency: float, confidence: float, goal_relevance: float, benefit: float) -> float:
    return round(importance * urgency * confidence * goal_relevance * benefit, 4)


def quiet_hours_active(now: datetime | None = None) -> bool:
    now = now or utcnow()
    try:
        start = date_parser.parse(settings.quiet_hours_start).time()
        end = date_parser.parse(settings.quiet_hours_end).time()
    except (ValueError, TypeError, OverflowError):
        return False
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


async def apply_attention_policy(
    session: AsyncSession,
    predictions: list[SensePrediction],
    *,
    budget_override: int | None = None,
    confidence_floor: float | None = None,
) -> list[SensePrediction]:
    """Ask Agent 14's attention policy before anything becomes deliverable.

    EV Sense does not invent its own attention budget: each candidate is
    submitted to ``app.notify.policy.decide`` (quiet hours, dedup, daily cap,
    max attempts) and only survives when that policy allows it. An explicit
    ``budget_override`` remains available for tests/calibration but is not the
    production path. ``notify_card`` candidates are emergencies under Agent
    14's policy and are never downgraded there; the confidence floor is an EV
    Sense signal-quality gate and still applies.
    """
    now = utcnow()
    daily_budget = budget_override if budget_override is not None else settings.daily_alert_budget
    remaining = max(0, daily_budget)
    updated: list[SensePrediction] = []
    for prediction in predictions:
        tier = prediction.tier
        deliver = prediction.deliver
        if (
            deliver
            and confidence_floor is not None
            and prediction.confidence < confidence_floor
        ):
            tier, deliver = "mention_later", False
        if deliver:
            # Agent 14 owns the attention budget. The override below (when
            # supplied by tests or calibration) is only an additional cap;
            # it never replaces Agent 14's quiet-hours/dedup/daily-cap verdict.
            from app.notify import policy as notify_policy

            decision = await notify_policy.decide(
                session,
                fingerprint=fingerprint({"kind": "ev_sense", "text": prediction.text}),
                exclude_id=None,
                priority=prediction.intervention_score,
                tier=tier,
                emergency=tier == "notify_card",
                allow_during_quiet_hours=False,
                bypass_policy=False,
                now=now,
            )
            if not decision.allowed:
                tier, deliver = "mention_later", False
                prediction.why_now = (
                    f"{prediction.why_now} [attention policy: {decision.reason}]"
                )
        if deliver and budget_override is not None:
            if remaining <= 0:
                tier, deliver = "mention_later", False
            else:
                remaining -= 1
        data = prediction.model_dump()
        data["tier"] = tier
        data["deliver"] = deliver
        updated.append(SensePrediction(**data))
    return updated


async def generate_predictions(
    session: AsyncSession,
    *,
    context: str | None = None,
    window_days: int = 30,
    active_goal: str | None = None,
) -> list[SensePrediction]:
    since = utcnow() - timedelta(days=window_days)
    goal = normalize_text(active_goal or "")
    predictions: list[SensePrediction] = []

    # 1. Decision loops.
    loops = await find_decision_loops(session, window_days=window_days, min_count=2)
    for loop in loops:
        confidence = loop["confidence"]
        relevance = 0.9 if goal and goal in normalize_text(loop["topic"]) else 0.5
        score = _score(
            importance=0.8,
            urgency=0.6,
            confidence=confidence,
            goal_relevance=relevance,
            benefit=0.7,
        )
        tier, deliver = intervention_tier(score)
        predictions.append(
            SensePrediction(
                kind="decision_loop",
                text=(
                    f"You've evaluated '{loop['topic']}' {loop['count']} times in the last "
                    f"{window_days} days. More research is unlikely to change the outcome."
                ),
                confidence=confidence,
                intervention_score=score,
                why_now=(
                    f"Because {loop['count']} decisions on the same topic exist since "
                    f"{loop['latest_at'].date().isoformat()}, with no recorded outcome to close the loop."
                ),
                basis_ids=loop["memory_ids"],
                tier=tier,
                deliver=deliver,
            )
        )

    # 2. Behavior patterns.
    pattern_rows = (
        await session.execute(
            select(Memory)
            .where(
                Memory.memory_type == "pattern",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
                Memory.event_time >= since,
            )
            .order_by(Memory.confidence.desc())
            .limit(10)
        )
    ).scalars().all()
    for pattern in pattern_rows:
        payload = pattern.payload or {}
        topic = payload.get("topic") or ""
        kind = payload.get("kind") or ""
        relevance = 0.9 if goal and goal in normalize_text(topic) else 0.5
        score = _score(
            importance=0.6,
            urgency=0.4,
            confidence=pattern.confidence,
            goal_relevance=relevance,
            benefit=0.5,
        )
        tier, deliver = intervention_tier(score)
        if kind == "goal_drift":
            text = (
                f"Goal '{topic}' has been quiet for {payload.get('silence_days', '?')} days — "
                "is it still a priority, or should it be closed?"
            )
        elif kind == "project_abandonment":
            text = (
                f"Project '{topic}' hasn't been mentioned in {payload.get('silence_days', '?')} "
                "days — resume it or close it out."
            )
        else:
            text = f"Pattern: '{topic}' recurs frequently — prepare for it before it happens."
        predictions.append(
            SensePrediction(
                kind="pattern",
                text=text,
                confidence=pattern.confidence,
                intervention_score=score,
                why_now=(
                    f"Because '{topic}' appeared {payload.get('count')} times between "
                    f"{payload.get('first_observed', '?')[:10]} and "
                    f"{payload.get('latest_observed', '?')[:10]}."
                ),
                basis_ids=[str(pattern.id)],
                tier=tier,
                deliver=deliver,
            )
        )

    # 3. Deadlines from the watchlist.
    watch_rows = (
        await session.execute(
            select(WatchlistItem).where(WatchlistItem.active.is_(True), WatchlistItem.kind == "deadline")
        )
    ).scalars().all()
    for watch in watch_rows:
        score = _score(
            importance=0.95,
            urgency=0.95,
            confidence=0.9,
            goal_relevance=0.9,
            benefit=0.9,
        )
        tier, deliver = intervention_tier(score)
        predictions.append(
            SensePrediction(
                kind="deadline",
                text=f"Deadline approaching: {watch.value}.",
                confidence=0.8,
                intervention_score=score,
                why_now=f"Because '{watch.value}' is on your watchlist with deadline metadata {watch.metadata_}.",
                basis_ids=[str(watch.id)],
                tier=tier,
                deliver=deliver,
            )
        )

    # 3b. Real calendar deadline: the next commitment from Agent 12's stored
    # calendar live events. Only emitted when an event actually exists, and the
    # basis ids are the live-event rows, never a synthetic date.
    from app.ev import calendar as calendar_feed

    cal = await calendar_feed.calendar_signals(session)
    next_event = cal.get("next_event")
    cal_event_ids = (cal.get("source") or {}).get("event_ids") or []
    if next_event and cal_event_ids:
        proximity = float(cal.get("deadline_proximity") or 0.0)
        score = _score(
            importance=0.85,
            urgency=round(max(0.5, proximity), 3),
            confidence=0.9,
            goal_relevance=0.75,
            benefit=0.8,
        )
        tier, deliver = intervention_tier(score)
        predictions.append(
            SensePrediction(
                kind="calendar_deadline",
                text=(
                    f"Next commitment: {next_event.get('summary')} at "
                    f"{next_event.get('start')}."
                ),
                confidence=0.9,
                intervention_score=score,
                why_now=(
                    f"Because the calendar integration stored {len(cal_event_ids)} live "
                    f"event(s); the next one starts at {next_event.get('start')} "
                    f"(proximity {proximity:.2f})."
                ),
                basis_ids=cal_event_ids[:5],
                tier=tier,
                deliver=deliver,
            )
        )

    # 4. Health anomalies.
    health_rows = (
        await session.execute(
            select(HealthSnapshot)
            .where(HealthSnapshot.occurred_at >= since)
            .order_by(HealthSnapshot.occurred_at.desc())
            .limit(5)
        )
    ).scalars().all()
    for snapshot in health_rows:
        for anomaly in snapshot.anomalies or []:
            metric = anomaly.get("metric", "")
            score = _score(
                importance=0.9,
                urgency=0.8 if anomaly.get("sustained") else 0.6,
                confidence=0.85,
                goal_relevance=0.7,
                benefit=0.8,
            )
            tier, deliver = intervention_tier(score)
            predictions.append(
                SensePrediction(
                    kind="health_anomaly",
                    text=f"{metric} is outside your normal range ({anomaly.get('value')}).",
                    confidence=0.85,
                    intervention_score=score,
                    why_now=f"Because {anomaly.get('rationale', 'a z-score anomaly was detected')}.",
                    basis_ids=[str(snapshot.id)],
                    tier=tier,
                    deliver=deliver,
                )
            )

    # 4b. Companion guardrail (anti-dependency / isolation detector).
    isolation = await scan_isolation(session)
    if isolation.detected:
        score = _score(
            importance=0.9,
            urgency=0.6,
            confidence=isolation.confidence,
            goal_relevance=0.7,
            benefit=0.8,
        )
        tier, deliver = intervention_tier(score)
        predictions.append(
            SensePrediction(
                kind="isolation_guardrail",
                text=isolation.recommendation or "A social reset could help break the current loop.",
                confidence=isolation.confidence,
                intervention_score=score,
                why_now="Because recent events show loneliness language with little social contact.",
                basis_ids=isolation.evidence_ids,
                tier=tier,
                deliver=deliver,
            )
        )

    # 4c. Maker reorder signals.
    reorders = await reorder_items(session)
    if reorders:
        score = _score(
            importance=0.7,
            urgency=0.6,
            confidence=0.8,
            goal_relevance=0.8,
            benefit=0.7,
        )
        tier, deliver = intervention_tier(score)
        names = ", ".join(item["name"] for item in reorders[:3])
        predictions.append(
            SensePrediction(
                kind="maker_reorder",
                text=f"Reorder needed: {names}.",
                confidence=0.8,
                intervention_score=score,
                why_now="Because BOM quantities are at or below their reorder thresholds.",
                basis_ids=[item["item_id"] for item in reorders],
                tier=tier,
                deliver=deliver,
            )
        )

    # 4d. Live sensor signals from permissioned collectors (EV Sense integration).
    for signal in await live.sense_signals(session, since=since):
        score = _score(
            importance=signal["importance"],
            urgency=signal["urgency"],
            confidence=signal["confidence"],
            goal_relevance=signal["goal_relevance"],
            benefit=signal["benefit"],
        )
        tier, deliver = intervention_tier(score)
        predictions.append(
            SensePrediction(
                kind=signal["kind"],
                text=signal["text"],
                confidence=signal["confidence"],
                intervention_score=score,
                why_now=signal["why_now"],
                basis_ids=signal["basis_ids"],
                tier=tier,
                deliver=deliver,
            )
        )

    # 5. Decision follow-ups due.
    due = await followups_due(session)
    if due:
        score = _score(
            importance=0.7,
            urgency=0.5,
            confidence=0.7,
            goal_relevance=0.8,
            benefit=0.6,
        )
        tier, deliver = intervention_tier(score)
        predictions.append(
            SensePrediction(
                kind="decision_followup",
                text=f"{len(due)} decision follow-up(s) are due for review.",
                confidence=0.7,
                intervention_score=score,
                why_now="Because those decisions were made more than 7 days ago and no outcome was recorded.",
                basis_ids=[str(d.id) for d in due],
                tier=tier,
                deliver=deliver,
            )
        )

    # 6. Recurring dismissed alerts.
    dismissed = (
        await session.execute(
            select(Alert).where(Alert.status == "dismissed", Alert.created_at >= since)
        )
    ).scalars().all()
    if len(dismissed) >= 3:
        score = _score(
            importance=0.5,
            urgency=0.4,
            confidence=0.6,
            goal_relevance=0.6,
            benefit=0.5,
        )
        tier, deliver = intervention_tier(score)
        predictions.append(
            SensePrediction(
                kind="alert_fatigue",
                text=f"{len(dismissed)} alerts were dismissed recently — consider pruning the watchlist.",
                confidence=0.6,
                intervention_score=score,
                why_now="Because repeated dismissals suggest the watchlist is producing noise, not signal.",
                basis_ids=[str(a.id) for a in dismissed],
                tier=tier,
                deliver=deliver,
            )
        )

    # Outcome calibration: reviewed prediction outcomes tune future confidence
    # for the same kind, so outcomes improve later decisions.
    accuracy_by_kind = await _accuracy_by_kind(session)
    calibrated: list[SensePrediction] = []
    for p in predictions:
        accuracy = accuracy_by_kind.get(p.kind)
        if accuracy is None:
            calibrated.append(p)
            continue
        old_confidence = p.confidence
        new_confidence = round(min(0.95, max(0.2, old_confidence + (accuracy - 0.5) * 0.2)), 3)
        factor = new_confidence / old_confidence if old_confidence else 1.0
        new_score = round(min(1.0, p.intervention_score * factor), 4)
        tier, deliver = intervention_tier(new_score)
        data = p.model_dump()
        data.update(
            {
                "confidence": new_confidence,
                "intervention_score": new_score,
                "tier": tier,
                "deliver": deliver,
            }
        )
        calibrated.append(SensePrediction(**data))
    predictions = calibrated

    # Rank by intervention score; only candidates with evidence survive.
    predictions.sort(key=lambda p: p.intervention_score, reverse=True)
    return [p for p in predictions if p.basis_ids]


async def _accuracy_by_kind(session: AsyncSession) -> dict[str, float]:
    rows = (
        await session.execute(
            select(Prediction).where(Prediction.reviewed_at.is_not(None))
        )
    ).scalars().all()
    counts: dict[str, list[bool]] = {}
    for row in rows:
        counts.setdefault(row.kind, []).append(row.outcome == "correct")
    return {
        kind: round(sum(values) / len(values), 3)
        for kind, values in counts.items()
    }


async def persist_predictions(session: AsyncSession, predictions: list[SensePrediction]) -> list[Prediction]:
    """Store scored predictions for outcome tracking (dedup within 24h)."""
    stored: list[Prediction] = []
    for p in predictions:
        if p.intervention_score < 0.15:
            continue
        fp = fingerprint({"kind": p.kind, "text": p.text})
        recent = (
            await session.execute(
                select(Prediction).where(
                    Prediction.kind == p.kind,
                    Prediction.created_at >= utcnow() - timedelta(hours=24),
                )
            )
        ).scalars().all()
        if any(fingerprint({"kind": r.kind, "text": r.text}) == fp for r in recent):
            continue
        row = Prediction(
            kind=p.kind,
            text=p.text,
            confidence=p.confidence,
            basis_ids=p.basis_ids,
            rationale=p.why_now,
            intervention_score=p.intervention_score,
            outcome="pending",
            details={"tier": p.tier},
        )
        session.add(row)
        stored.append(row)
    await session.flush()
    return stored
