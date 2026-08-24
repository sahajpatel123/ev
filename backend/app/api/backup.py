"""Encrypted backup, verify, and restore API (master-key only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_master
from app.db import get_session
from app.ops.metrics import record_restore_drill
from app.schemas import (
    BackupCreateRequest,
    BackupOut,
    BackupPrepareOut,
    BackupRestoreOut,
    BackupRestoreRequest,
    BackupVerifyOut,
    BackupVerifyRequest,
)
from app.services.access_log import log_access
from app.services.backup import (
    BackupError,
    create_backup,
    restore_backup,
    verify_backup,
)
from app.utils.text import utcnow

router = APIRouter(prefix="/v1/backup")


@router.post("", response_model=BackupOut, status_code=201)
async def create_backup_endpoint(
    data: BackupCreateRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> BackupOut:
    """Create an encrypted backup of the event log and audit/devices metadata."""
    try:
        result = await create_backup(
            session,
            passphrase=data.passphrase,
            destination=data.destination,
        )
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await log_access(
        session,
        actor=actor,
        action="backup",
        endpoint="POST /v1/backup",
        resource_type="all",
        details={"path": result["path"], "counts": result["counts"]},
    )
    await session.commit()
    return BackupOut(
        path=result["path"],
        schema_version=result["schema"],
        created_at=result["created_at"],
        checksum=result["checksum"],
        size_bytes=result["size_bytes"],
        counts=result["counts"],
    )


@router.post("/verify", response_model=BackupVerifyOut)
async def verify_backup_endpoint(
    data: BackupVerifyRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> BackupVerifyOut:
    """Decrypt and integrity-check a backup file without touching the database."""
    try:
        result = verify_backup(data.path, data.passphrase)
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await log_access(
        session,
        actor=actor,
        action="backup_verify",
        endpoint="POST /v1/backup/verify",
        resource_type="all",
        details={"path": data.path, "valid": result["valid"]},
    )
    await session.commit()
    return BackupVerifyOut(
        valid=result["valid"],
        schema_version=result["schema"],
        created_at=result["created_at"],
        counts=result["counts"],
        checksum_match=result["checksum_match"],
        reason=result["reason"],
    )


@router.post("/restore/prepare", response_model=BackupPrepareOut)
async def prepare_restore_endpoint(
    data: BackupRestoreRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> BackupPrepareOut:
    """P0 CONTAINMENT (Phase 6/7): mint a short-lived single-use confirmation
    token bound to this backup path + environment + live DB fingerprint.
    Ordinary master bearer tokens alone can no longer run restore."""
    from app.ops.destructive_maintenance import (
        DestructiveOperationError,
        prepare_destructive_operation,
    )

    try:
        prep = prepare_destructive_operation(
            session, operation="backup.restore", target=data.path
        )
    except DestructiveOperationError as exc:
        raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message}) from None
    await log_access(
        session,
        actor=actor,
        action="restore_prepare",
        endpoint="POST /v1/backup/restore/prepare",
        resource_type="all",
        details={"path": data.path, "environment": prep["environment"], "fingerprint": prep["database_fingerprint"]},
    )
    await session.commit()
    return BackupPrepareOut(**prep)


@router.post("/restore", response_model=BackupRestoreOut)
async def restore_backup_endpoint(
    data: BackupRestoreRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> BackupRestoreOut:
    """Restore from a verified backup (merge, or wipe + full restore drill).

    P0 LAW: gated by destructive-maintenance confirmation. Master bearer
    alone is insufficient. Confirmation is single-use, bound to
    path+environment+live database fingerprint, and expires."""
    from app.ops.destructive_maintenance import (
        DestructiveOperationError,
        verify_destructive_confirmation,
    )

    try:
        verify_destructive_confirmation(
            session,
            token=getattr(data, "restore_confirmation", None),
            operation="backup.restore",
            target=data.path,
        )
    except DestructiveOperationError as exc:
        raise HTTPException(status_code=exc.status, detail={"error_code": exc.code, "message": exc.message}) from None
    try:
        result = await restore_backup(
            session,
            path=data.path,
            passphrase=data.passphrase,
            mode=data.mode,
            confirm_wipe=data.confirm_wipe,
            actor=actor,
        )
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await log_access(
        session,
        actor=actor,
        action="restore",
        endpoint="POST /v1/backup/restore",
        resource_type="all",
        details={
            "path": data.path,
            "mode": data.mode,
            "events_restored": result["events_restored"],
        },
    )
    if data.mode == "wipe":
        record_restore_drill()
    await session.commit()
    return BackupRestoreOut(
        mode=result["mode"],
        restored_at=utcnow(),
        events_restored=result["events_restored"],
        events_skipped=result["events_skipped"],
        attachments_restored=result["attachments_restored"],
        blobs_restored=result.get("blobs_restored", 0),
        devices_restored=result["devices_restored"],
        access_log_restored=result["access_log_restored"],
        backup_counts=result["backup_counts"],
        rebuild=result["rebuild"],
    )
