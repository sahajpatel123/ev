"""Compliance API: regional policy, transparency center, erasure, retention sweep."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor, require_master, require_owner_trust
from app.compliance.anomaly import detect_access_anomalies
from app.compliance.erasure import erase_biometric_data, retention_sweep
from app.compliance.policy import policy_summary
from app.compliance.schemas import (
    AccessAnomaliesOut,
    AccessAnomalyOut,
    AccessLogEntryOut,
    AccessLogPageOut,
    CompliancePolicyOut,
    ErasureOut,
    ErasureRequest,
    RetentionSweepOut,
    RetentionSweepRequest,
    TransparencyOut,
    TransparencySummaryOut,
)
from app.compliance.transparency import transparency_report, transparency_summary
from app.db import get_session
from app.models import AccessLog
from app.services.access_log import log_access
from app.utils.text import utcnow

router = APIRouter(prefix="/v1/compliance", tags=["compliance"])


@router.get("/policy", response_model=CompliancePolicyOut)
async def compliance_policy(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> CompliancePolicyOut:
    return CompliancePolicyOut(**policy_summary())


@router.get("/transparency", response_model=TransparencyOut)
async def transparency(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> TransparencyOut:
    return TransparencyOut(**await transparency_report(session))


@router.get("/transparency/summary", response_model=TransparencySummaryOut)
async def transparency_summary_endpoint(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> TransparencySummaryOut:
    """Plain-language egress report a human can read in ~30 seconds."""
    return TransparencySummaryOut(
        generated_at=utcnow().isoformat(),
        summary=await transparency_summary(session),
    )


@router.post("/erasure", response_model=ErasureOut)
async def data_erasure(
    data: ErasureRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> ErasureOut:
    manifest = await erase_biometric_data(session, reason=data.reason, actor=actor)
    await session.commit()
    return ErasureOut(requested_at=utcnow(), status="completed", manifest=manifest)


@router.post("/retention/sweep", response_model=RetentionSweepOut)
async def retention_sweep_endpoint(
    data: RetentionSweepRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> RetentionSweepOut:
    result = await retention_sweep(session, reason=data.reason, actor=actor)
    await session.commit()
    return RetentionSweepOut(
        ran_at=utcnow(),
        voiceprints_deleted=result["voiceprints_deleted"],
        enrollment_ids=result["enrollment_ids"],
        faceprints_deleted=result.get("faceprints_deleted", 0),
        face_enrollment_ids=result.get("face_enrollment_ids", []),
        corpus_snapshots_redacted=result["corpus_snapshots_redacted"],
        access_logs_deleted=result["access_logs_deleted"],
        policy_retention_days=result["policy_retention_days"],
    )


@router.get("/access-log", response_model=AccessLogPageOut)
async def access_log_page(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    actor: str | None = Query(default=None, max_length=128),
    action: str | None = Query(default=None, max_length=32),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    ctx=Depends(require_owner_trust),
) -> AccessLogPageOut:
    """Export the access log itself: paged, filterable, owner-trusted."""
    from datetime import datetime

    filters = []
    if actor:
        filters.append(AccessLog.actor == actor)
    if action:
        filters.append(AccessLog.action == action)
    if since:
        filters.append(AccessLog.occurred_at >= datetime.fromisoformat(since))
    if until:
        filters.append(AccessLog.occurred_at <= datetime.fromisoformat(until))

    total = (
        await session.execute(
            select(func.count(AccessLog.id)).where(*filters)
        )
    ).scalar_one()
    rows = list(
        (
            await session.execute(
                select(AccessLog)
                .where(*filters)
                .order_by(AccessLog.occurred_at.desc(), AccessLog.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    await log_access(
        session,
        actor=ctx.actor,
        action="access_log.read",
        endpoint="GET /v1/compliance/access-log",
        resource_type="access_log",
        resource_ids=[],
        details={"limit": limit, "offset": offset, "actor": actor, "action": action},
    )
    await session.commit()
    return AccessLogPageOut(
        logs=[AccessLogEntryOut.model_validate(row) for row in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/anomalies", response_model=AccessAnomaliesOut)
async def access_anomalies(
    window_minutes: int = Query(default=60, ge=5, le=1440),
    deletion_threshold: int = Query(default=5, ge=1, le=1000),
    export_threshold: int = Query(default=3, ge=1, le=1000),
    failure_threshold: int = Query(default=10, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    ctx=Depends(require_owner_trust),
) -> AccessAnomaliesOut:
    """Rule-based anomaly scan over recent access-log patterns."""
    anomalies = await detect_access_anomalies(
        session,
        window_minutes=window_minutes,
        deletion_threshold=deletion_threshold,
        export_threshold=export_threshold,
        failure_threshold=failure_threshold,
    )
    await log_access(
        session,
        actor=ctx.actor,
        action="access_anomaly_scan",
        endpoint="GET /v1/compliance/anomalies",
        resource_type="access_log",
        resource_ids=[],
        details={"window_minutes": window_minutes, "anomalies": len(anomalies)},
    )
    await session.commit()
    return AccessAnomaliesOut(
        detected_at=utcnow(),
        window_minutes=window_minutes,
        anomalies=[AccessAnomalyOut(**item) for item in anomalies],
    )
