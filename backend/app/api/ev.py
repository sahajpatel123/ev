"""E.V.-inspired futuristic API: calibration, tactical briefs, EV Sense, health, alerts."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor
from app.db import get_session
from app.ev import alert_radar, diagnostics, ev_sense, health_radar, people, personality, tactical
from app.ev.calibration import proactive_tuning
from app.ev.decisions import find_decision_loops, record_outcome
from app.ev.interaction import build_strategy
from app.ev.user_state import build_user_state
from app.memory.retrieval import Retriever
from app.models import GearSnapshot, Memory, Prediction
from app.schemas import (
    AlertDismissRequest,
    AlertOut,
    AlertScanResponse,
    CalibrationReport,
    DecisionOutcomeCreate,
    DecisionOutcomeOut,
    GearSnapshotCreate,
    GearSnapshotOut,
    HealthSnapshotCreate,
    HealthSnapshotOut,
    HealthSummaryOut,
    HealthTrendOut,
    InteractionModeRequest,
    InteractionModeResponse,
    PersonWhereaboutsOut,
    PredictionOut,
    PredictionOutcomeUpdate,
    ProactiveTuningOut,
    SensePredictRequest,
    SensePredictResponse,
    TacticalBriefOut,
    TacticalBriefRequest,
    WatchlistCreate,
    WatchlistOut,
)
from app.utils.text import utcnow

router = APIRouter(prefix="/v1")


@router.post("/diagnostics/calibrate", response_model=CalibrationReport)
async def calibrate(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> CalibrationReport:
    """E.V.-style self-calibration: database, embeddings, gateway, retrieval, storage."""
    return await diagnostics.run_calibration(session)


@router.post("/tactical/brief", response_model=TacticalBriefOut)
async def tactical_brief(
    data: TacticalBriefRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> TacticalBriefOut:
    """Pre-event HUD briefing (ev.hud.briefing.v1) grounded in personal memory."""
    return await tactical.build_briefing(session, data)


@router.post("/sense/predict", response_model=SensePredictResponse)
async def sense_predict(
    data: SensePredictRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> SensePredictResponse:
    """EV Sense: ranked predictions with intervention scoring and 'why now?' rationale."""
    state = await build_user_state(session, access="model")
    predictions = await ev_sense.generate_predictions(
        session,
        context=data.context,
        window_days=data.window_days,
        active_goal=state.active_goal,
    )
    tuning = await proactive_tuning(session)
    predictions = await ev_sense.apply_attention_policy(
        session,
        predictions,
        budget_override=tuning.daily_budget,
    )
    stored = await ev_sense.persist_predictions(session, predictions)
    await alert_radar.promote_predictions(session, stored)
    await session.commit()
    return SensePredictResponse(
        predictions=predictions,
        model_used="rule-based",
        generated_at=utcnow(),
    )


@router.get("/sense/predictions", response_model=list[PredictionOut])
async def list_predictions(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[PredictionOut]:
    stmt = select(Prediction).order_by(Prediction.created_at.desc()).limit(min(limit, 200))
    if status:
        stmt = stmt.where(Prediction.outcome == status)
    rows = list((await session.execute(stmt)).scalars().all())
    return [PredictionOut.model_validate(r) for r in rows]


@router.post("/sense/predictions/{prediction_id}/outcome", response_model=PredictionOut)
async def record_prediction_outcome(
    prediction_id: UUID,
    data: PredictionOutcomeUpdate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PredictionOut:
    prediction = await session.get(Prediction, prediction_id)
    if prediction is None:
        raise HTTPException(status_code=404, detail="Prediction not found")
    prediction.outcome = data.outcome
    if data.outcome != "pending":
        prediction.reviewed_at = utcnow()
    await session.commit()
    return PredictionOut.model_validate(prediction)


@router.post("/health/snapshot", response_model=HealthSnapshotOut, status_code=201)
async def health_snapshot(
    data: HealthSnapshotCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> HealthSnapshotOut:
    snapshot = await health_radar.create_snapshot(
        session,
        metrics=data.metrics,
        source=data.source,
        device_id=data.device_id,
        occurred_at=data.occurred_at,
    )
    await session.commit()
    return HealthSnapshotOut.model_validate(snapshot)


@router.get("/health/trends", response_model=HealthTrendOut)
async def health_trends(
    metric: str = Query(min_length=1, max_length=64),
    window_days: int = Query(default=14, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> HealthTrendOut:
    result = await health_radar.trend(session, metric=metric, window_days=window_days)
    return HealthTrendOut.model_validate(result)


@router.get("/health/summary", response_model=HealthSummaryOut)
async def health_summary(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> HealthSummaryOut:
    return HealthSummaryOut.model_validate(await health_radar.morning_brief(session))


@router.post("/alerts/watchlist", response_model=WatchlistOut, status_code=201)
async def create_watch_item(
    data: WatchlistCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> WatchlistOut:
    item = await alert_radar.upsert_watch_item(session, data)
    await session.commit()
    return WatchlistOut.model_validate(item)


@router.get("/alerts/watchlist", response_model=list[WatchlistOut])
async def list_watch_items(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[WatchlistOut]:
    rows = await alert_radar.list_watch_items(session)
    return [WatchlistOut.model_validate(r) for r in rows]


@router.delete("/alerts/watchlist/{item_id}", response_model=WatchlistOut)
async def delete_watch_item(
    item_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> WatchlistOut:
    try:
        item = await alert_radar.deactivate_watch_item(session, item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Watchlist item not found") from None
    await session.commit()
    return WatchlistOut.model_validate(item)


@router.get("/alerts/scan", response_model=AlertScanResponse)
async def scan_alerts(
    window_days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=1000, ge=10, le=5000),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> AlertScanResponse:
    result = await alert_radar.scan(session, window_days=window_days, limit=limit)
    await session.commit()
    return AlertScanResponse(
        scanned_events=result["scanned_events"],
        scanned_memories=result["scanned_memories"],
        alerts_created=[AlertOut.model_validate(a) for a in result["alerts_created"]],
        existing_alerts=result["existing_alerts"],
    )


@router.get("/alerts", response_model=list[AlertOut])
async def list_alerts(
    status: str | None = None,
    kind: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[AlertOut]:
    rows = await alert_radar.list_alerts(session, status=status, kind=kind, limit=limit)
    return [AlertOut.model_validate(r) for r in rows]


@router.post("/alerts/{alert_id}/dismiss", response_model=AlertOut)
async def dismiss_alert(
    alert_id: UUID,
    data: AlertDismissRequest | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> AlertOut:
    try:
        alert = await alert_radar.dismiss_alert(
            session,
            alert_id,
            reason=data.reason if data else "dismissed",
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Alert not found") from None
    await session.commit()
    return AlertOut.model_validate(alert)


@router.get("/people/{name}/whereabouts", response_model=PersonWhereaboutsOut)
async def person_whereabouts(
    name: str,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PersonWhereaboutsOut:
    """Person finder over user-owned memory: last seen, mentions, related memories."""
    return await people.whereabouts(session, name)


@router.post("/interaction/mode", response_model=InteractionModeResponse)
async def interaction_mode(
    data: InteractionModeRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> InteractionModeResponse:
    """Determine communication mode, intent, urgency, and assertiveness for a message."""
    loops = await find_decision_loops(session, min_count=2)
    patterns = (
        await session.execute(
            select(Memory).where(
                Memory.memory_type == "pattern",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
        )
    ).scalars().all()
    pattern_confidence = max((p.confidence for p in patterns), default=0.0)
    pending_alerts = await alert_radar.list_alerts(session, status="pending", limit=10)
    alert_priority = max((a.priority for a in pending_alerts), default=0.0)
    alert_tier = next(
        (a.tier for a in pending_alerts if a.priority == alert_priority),
        None,
    )
    retriever = Retriever(session)
    hits = await retriever.search(data.message, k=20, access="model")
    loop_count = max((loop["count"] for loop in loops), default=0)
    profile = await personality.get_current(session)
    tuning = await proactive_tuning(session)
    strategy = build_strategy(
        data.message,
        context=data.context,
        decision_loop_count=loop_count,
        pattern_confidence=pattern_confidence,
        evidence_count=len(hits),
        profile=personality.to_dict(profile),
        pending_alert_priority=alert_priority,
        pending_alert_tier=alert_tier,
        challenge_ceiling=tuning.challenge_ceiling,
    )
    return InteractionModeResponse(message=data.message, mode=strategy.mode, strategy=strategy)


@router.get("/calibration/tuning", response_model=ProactiveTuningOut)
async def calibration_tuning(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ProactiveTuningOut:
    """Expose the derived proactive calibration so behavior changes are inspectable."""
    return await proactive_tuning(session)


@router.post("/decisions/{decision_id}/outcome", response_model=DecisionOutcomeOut, status_code=201)
async def decision_outcome(
    decision_id: UUID,
    data: DecisionOutcomeCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> DecisionOutcomeOut:
    try:
        outcome = await record_outcome(
            session,
            decision_id,
            expected_outcome=data.expected_outcome,
            actual_outcome=data.actual_outcome,
            lesson=data.lesson,
            actor=actor,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Decision memory not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await session.commit()
    return DecisionOutcomeOut.model_validate(outcome)


@router.post("/gear/snapshot", response_model=GearSnapshotOut, status_code=201)
async def gear_snapshot(
    data: GearSnapshotCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> GearSnapshotOut:
    row = GearSnapshot(
        device_id=data.device_id,
        reported_at=data.reported_at or utcnow(),
        battery_percent=data.battery_percent,
        storage_free_bytes=data.storage_free_bytes,
        memory_used_percent=data.memory_used_percent,
        cpu_percent=data.cpu_percent,
        uptime_seconds=data.uptime_seconds,
        details=data.details,
    )
    session.add(row)
    await session.commit()
    return GearSnapshotOut.model_validate(row)


@router.get("/gear", response_model=list[GearSnapshotOut])
async def list_gear(
    device_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[GearSnapshotOut]:
    stmt = select(GearSnapshot).order_by(GearSnapshot.reported_at.desc()).limit(min(limit, 100))
    if device_id:
        stmt = stmt.where(GearSnapshot.device_id == device_id)
    rows = list((await session.execute(stmt)).scalars().all())
    return [GearSnapshotOut.model_validate(r) for r in rows]
