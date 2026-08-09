"""Operational maintenance endpoints (master-key only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_master
from app.db import get_session
from app.services.maintenance import prune_backups, purge_tombstoned_blobs

router = APIRouter(prefix="/v1/maintenance")


class BlobPurgeRequest(BaseModel):
    older_than_days: int | None = Field(default=None, ge=0, le=3650)


class BlobPurgeResponse(BaseModel):
    events_eligible: int
    blobs_deleted: int
    storage_keys: list[str]


class BackupPruneRequest(BaseModel):
    keep: int | None = Field(default=None, ge=0, le=365)


class BackupPruneResponse(BaseModel):
    kept: int
    removed: list[str]


@router.post("/purge-tombstoned-blobs", response_model=BlobPurgeResponse)
async def purge_tombstoned_blobs_endpoint(
    data: BlobPurgeRequest | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> BlobPurgeResponse:
    try:
        result = await purge_tombstoned_blobs(
            session,
            older_than_days=data.older_than_days if data else None,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await session.commit()
    return BlobPurgeResponse(**result)


@router.post("/prune-backups", response_model=BackupPruneResponse)
async def prune_backups_endpoint(
    data: BackupPruneRequest | None = None,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> BackupPruneResponse:
    try:
        result = prune_backups(keep=data.keep if data else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await session.commit()
    return BackupPruneResponse(**result)
