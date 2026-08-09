"""Operations API: aggregate health, latency, and cost-budget metrics."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_master
from app.db import get_session
from app.ops.metrics import collect_metrics

router = APIRouter(prefix="/v1/ops", tags=["ops"])


@router.get("/metrics")
async def ops_metrics(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> dict:
    """Aggregate model-call latency/error/cost metrics against budgets."""

    return await collect_metrics(session)
