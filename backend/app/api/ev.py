"""E.V.-inspired futuristic API: calibration, tactical briefs, EV Sense, health, alerts."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor, require_actor_context
from app.db import get_session
from app.ev import (
    alert_radar,
    ev_sense,
    gear,
    health_radar,
    people,
    personality,
    tactical,
)
from app.ev.calibration import proactive_tuning
from app.ev.decisions import find_decision_loops, record_outcome
from app.ev.edith import record_command
from app.ev.interaction import build_strategy
from app.ev.user_state import build_user_state
from app.filter.policy import active_policy
from app.memory.retrieval import Retriever
from app.models import GearSnapshot, Memory, Prediction
from app.schemas import (
    AlertDismissRequest,
    AlertOut,
    AlertScanResponse,
    BeaconCreate,
    BeaconOut,
    CalibrationReport,
    DecisionOutcomeCreate,
    DecisionOutcomeOut,
    DiagnosticsLastOut,
    GearScanResponse,
    GearSnapshotCreate,
    GearSnapshotOut,
    HealthSnapshotCreate,
    HealthSnapshotOut,
    HealthSummaryOut,
    HealthTrendOut,
    HudQuickCardOut,
    InteractionModeRequest,
    InteractionModeResponse,
    LocationShareCreate,
    LocationShareOut,
    LookoutUtteranceIn,
    OwnerCameraCreate,
    PersonWhereaboutsOut,
    PredictionOut,
    PredictionOutcomeUpdate,
    ProactiveTuningOut,
    PublicFeedCreate,
    SensePredictRequest,
    SensePredictResponse,
    TacticalBriefOut,
    TacticalBriefRequest,
    TacticalQuickRequest,
    TelemetrySampleCreate,
    TelemetrySampleOut,
    TelemetrySessionCreate,
    TelemetrySessionOut,
    WatchlistCreate,
    WatchlistOut,
)
from app.utils.text import utcnow

router = APIRouter(prefix="/v1")


@router.post("/diagnostics/calibrate", response_model=CalibrationReport)
async def calibrate(
    session: AsyncSession = Depends(get_session),
    ctx=Depends(require_actor_context),
) -> CalibrationReport:
    """Run diagnostics through the same policy and audit path as other tools."""
    from app.ev.tools import dispatch

    tool = await dispatch(
        session,
        "calibrate",
        {},
        actor=ctx.actor,
        allow_sensitive=True,
        device_id=ctx.device_id,
        channel="action",
        audit_endpoint="POST /v1/diagnostics/calibrate",
    )
    payload = tool.result if isinstance(tool.result, dict) else {}
    report_payload = payload.get("report") if isinstance(payload.get("report"), dict) else payload
    if not tool.ok or not report_payload.get("checks"):
        error = str(tool.error or payload.get("error") or "diagnostics unavailable")
        status = 503 if error in {"not_connected", "unavailable"} else 403
        raise HTTPException(status_code=status, detail=error)
    report = CalibrationReport.model_validate(report_payload)
    await session.commit()
    return report


@router.get("/diagnostics/last", response_model=DiagnosticsLastOut)
async def diagnostics_last(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> DiagnosticsLastOut:
    from app.ev.assistant import last_calibration_report
    from app.ev.workbench import last_diagnostics_payload

    payload = last_diagnostics_payload(await last_calibration_report(session))
    return DiagnosticsLastOut.model_validate(payload)


@router.post("/telemetry/sessions", response_model=TelemetrySessionOut, status_code=201)
async def start_telemetry_session(
    data: TelemetrySessionCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> TelemetrySessionOut:
    from app.ev.hardware import start_test_session

    row = await start_test_session(session, label=data.label)
    await session.commit()
    return TelemetrySessionOut.model_validate(row)


@router.post("/telemetry/sample", response_model=TelemetrySampleOut, status_code=201)
async def post_telemetry_sample(
    data: TelemetrySampleCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> TelemetrySampleOut:
    from app.ev.hardware import record_sample

    row = await record_sample(
        session,
        source=data.source,
        battery=data.battery,
        alt=data.alt,
        speed=data.speed,
        lat=data.lat,
        lon=data.lon,
        session_id=data.session_id,
        details=data.details,
    )
    await session.commit()
    return TelemetrySampleOut.model_validate(row)


@router.post("/location-shares", response_model=LocationShareOut, status_code=201)
async def create_location_share(
    data: LocationShareCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> LocationShareOut:
    from datetime import timedelta

    from app.models import LocationShare
    from app.utils.text import utcnow

    row = LocationShare(
        person_name=data.name,
        last_lat=data.last_lat,
        last_lon=data.last_lon,
        token_expires=data.token_expires or (utcnow() + timedelta(hours=6)),
        source=data.source,
        owner_family_device=data.owner_family_device,
        consented_at=utcnow(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return LocationShareOut.model_validate(row)


@router.post("/beacons", response_model=BeaconOut, status_code=201)
async def create_beacon(
    data: BeaconCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> BeaconOut:
    from app.models import Beacon
    from app.utils.text import utcnow

    row = Beacon(
        label=data.label,
        kind=data.kind,
        last_lat=data.last_lat,
        last_lon=data.last_lon,
        owner_only=True,
        last_seen_at=utcnow() if data.last_lat is not None else None,
        details=data.details,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return BeaconOut.model_validate(row)


@router.post("/cameras", status_code=201)
async def add_owner_camera(
    data: OwnerCameraCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    from app.models import OwnerCamera

    row = OwnerCamera(
        name=data.name,
        vault_ref=data.vault_ref,
        kind=data.kind,
        clip_attachment_id=data.clip_attachment_id,
    )
    session.add(row)
    await session.commit()
    return {"id": str(row.id), "name": row.name, "discovered_lan": False}


@router.post("/public-feeds", status_code=201)
async def add_public_feed(
    data: PublicFeedCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    from app.models import PublicFeed

    row = PublicFeed(
        kind=data.kind,
        url=data.url,
        label=data.label,
        last_items=list(data.items or []),
    )
    session.add(row)
    await session.commit()
    return {"id": str(row.id), "label": row.label}


@router.post("/lookout/utterance")
async def lookout_utterance(
    data: LookoutUtteranceIn,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    from app.ev.workbench import post_utterance

    result = await post_utterance(
        session,
        data.text,
        conversation_id=data.conversation_id,
        prefer_haptic=data.prefer_haptic,
        actor=actor,
    )
    await session.commit()
    return result


@router.get("/lookout/transcript")
async def lookout_transcript(
    conversation_id: UUID | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    from app.ev.workbench import lookout_transcript as _transcript

    return await _transcript(session, conversation_id)


@router.get("/lookout/live")
async def lookout_live(
    conversation_id: UUID | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
):
    import json

    from fastapi.responses import StreamingResponse

    from app.ev.workbench import lookout_transcript as _transcript

    payload = await _transcript(session, conversation_id)

    async def frames():
        yield f"event: transcript\ndata: {json.dumps(payload, default=str)}\n\n"
        yield "event: done\ndata: {\"ok\": true}\n\n"

    return StreamingResponse(frames(), media_type="text/event-stream")


@router.post("/tactical/brief", response_model=TacticalBriefOut)
async def tactical_brief(
    data: TacticalBriefRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> TacticalBriefOut:
    """Pre-event HUD briefing (ev.hud.briefing.v1) grounded in personal memory."""
    return await tactical.build_briefing(session, data)


@router.post("/tactical/prepare", response_model=HudQuickCardOut)
async def tactical_prepare(
    data: TacticalQuickRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> HudQuickCardOut:
    """Precompute and cache a tactical quick card (ev.hud.quickcard.v1)."""
    card = await tactical.prepare_quick_card(session, data)
    await record_command(
        session,
        command_type="tactical.quickcard.prepare",
        actor=actor,
        target_type="topic",
        target_id=data.topic,
        request={"topic": data.topic, "stakes": data.stakes},
        result={"card": card.objective, "schema_version": card.schema_version},
        status="completed",
    )
    await session.commit()
    return card


@router.get("/tactical/quick", response_model=HudQuickCardOut)
async def tactical_quick(
    topic: str = Query(min_length=1, max_length=500),
    stakes: str | None = None,
    context: str | None = None,
    ttl_seconds: int = Query(default=3600, ge=0, le=86400 * 7),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> HudQuickCardOut:
    """HUD quick card: cached read when fresh, otherwise rebuild + cache (<800 ms target)."""
    request = TacticalQuickRequest(
        topic=topic,
        stakes=stakes,
        context=context,
        ttl_seconds=ttl_seconds,
    )
    card, _ = await tactical.get_quick_card(session, request)
    await session.commit()
    return card


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
    filter_policy = await active_policy(session)
    predictions = await ev_sense.apply_attention_policy(
        session,
        predictions,
        budget_override=tuning.daily_budget,
        confidence_floor=filter_policy.ev_sense_confidence_floor,
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
        permission_state=data.permission_state,
        synced_at=data.synced_at,
        units=data.units,
        source_metadata=data.source_metadata,
    )
    await session.commit()
    if any(flag.get("emergency") for flag in (snapshot.anomalies or [])) or (
        snapshot.readiness is not None and snapshot.readiness < 35
    ):
        try:
            from app.ev.lookout import compose_and_maybe_open

            await compose_and_maybe_open(
                session,
                message="live vitals need a pulse",
                reply=(
                    f"Readiness {snapshot.readiness}. "
                    + "; ".join(
                        str(flag.get("rationale") or flag.get("metric"))
                        for flag in (snapshot.anomalies or [])[:3]
                    )
                ),
                title="Vitals",
                explicit=False,
            )
        except Exception:  # noqa: BLE001 - a HUD miss must not drop the snapshot
            pass
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
    from app.ev.workshop import scan_empties

    await scan_empties(session, emit=True)
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


@router.post("/gear/scan", response_model=GearScanResponse)
async def scan_gear_alerts(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> GearScanResponse:
    """Scan latest device snapshots and create ranked, deduped gear alerts."""
    result = await gear.scan_gear(session)
    await session.commit()
    return GearScanResponse(
        scanned_devices=result["scanned_devices"],
        alerts_created=[AlertOut.model_validate(a) for a in result["alerts_created"]],
        duplicates_skipped=result["duplicates_skipped"],
    )
