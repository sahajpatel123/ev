"""Compliance API: regional policy, transparency center, erasure, retention sweep."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor
from app.compliance.erasure import erase_biometric_data, retention_sweep
from app.compliance.policy import policy_summary
from app.compliance.schemas import (
    CompliancePolicyOut,
    ErasureOut,
    ErasureRequest,
    RetentionSweepOut,
    RetentionSweepRequest,
    TransparencyOut,
)
from app.compliance.transparency import transparency_report
from app.db import get_session
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


@router.post("/erasure", response_model=ErasureOut)
async def data_erasure(
    data: ErasureRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ErasureOut:
    manifest = await erase_biometric_data(session, reason=data.reason, actor=actor)
    await session.commit()
    return ErasureOut(requested_at=utcnow(), status="completed", manifest=manifest)


@router.post("/retention/sweep", response_model=RetentionSweepOut)
async def retention_sweep_endpoint(
    data: RetentionSweepRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> RetentionSweepOut:
    result = await retention_sweep(session, reason=data.reason, actor=actor)
    await session.commit()
    return RetentionSweepOut(
        ran_at=utcnow(),
        voiceprints_deleted=result["voiceprints_deleted"],
        enrollment_ids=result["enrollment_ids"],
        corpus_snapshots_redacted=result["corpus_snapshots_redacted"],
        access_logs_deleted=result["access_logs_deleted"],
        policy_retention_days=result["policy_retention_days"],
    )
