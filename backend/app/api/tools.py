"""Sandboxed code/file tool API (plan 11.4), owner-trusted and audited."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_owner_trust
from app.db import get_session
from app.ev.policy import Confirmation
from app.services.access_log import log_access
from app.tools import sandbox
from app.tools.operations import resolve_operation
from app.tools.schemas import (
    FileReadRequest,
    FileReadResponse,
    FileWriteRequest,
    FileWriteResponse,
    ToolsExecuteRequest,
    ToolsExecuteResponse,
)

router = APIRouter(prefix="/v1/tools", tags=["tools"])


@router.post("/execute", response_model=ToolsExecuteResponse)
async def execute_tool(
    data: ToolsExecuteRequest,
    session: AsyncSession = Depends(get_session),
    ctx=Depends(require_owner_trust),
) -> ToolsExecuteResponse:
    """Owner-trust is necessary, not sufficient. POL R4 still has to admit the run."""

    from app.ev.tools import dispatch

    operation = resolve_operation(data.operation)
    if operation is None:
        await log_access(
            session,
            actor=ctx.actor,
            action="tool.execute",
            endpoint="POST /v1/tools/execute",
            resource_type="sandbox",
            resource_ids=[],
            details={"operation": data.operation, "status": "denied", "error": "operation_not_allowed"},
        )
        await session.commit()
        return ToolsExecuteResponse(
            command="",
            operation=data.operation,
            ok=False,
            error="operation_not_allowed",
            spoken="That software operation is not allowlisted.",
        )

    tool = await dispatch(
        session,
        "execute_command",
        {
            "command": operation.command,
            "cwd": data.cwd,
            "timeout_seconds": data.timeout_seconds,
            "confirm": data.confirm,
        },
        actor=ctx.actor,
        allow_sensitive=True,
        device_id=ctx.device_id,
        channel="action",
        confirmation=(
            Confirmation(
                factor="master_key",
                confirmed=True,
                target=operation.command,
            )
            if data.confirm
            else None
        ),
        audit_endpoint="POST /v1/tools/execute",
    )
    body = tool.result if isinstance(tool.result, dict) else {}
    if tool.error == "confirmation_required" or body.get("needs_confirm"):
        await session.commit()
        return ToolsExecuteResponse(
            command=operation.command,
            operation=operation.name,
            ok=False,
            error="confirmation_required",
            needs_confirm=True,
            action_id=str(body.get("action_id") or "") or None,
            spoken=str(body.get("spoken") or ""),
        )
    await log_access(
        session,
        actor=ctx.actor,
        action="tool.execute",
        endpoint="POST /v1/tools/execute",
        resource_type="sandbox",
        resource_ids=[],
        details={
            "command": body.get("command") or operation.command,
            "operation": operation.name,
            "exit_code": body.get("exit_code"),
            "ok": tool.ok,
            "error": tool.error,
            "idempotent_replay": body.get("idempotent_replay"),
        },
    )
    await session.commit()
    code = body.get("exit_code")
    exit_code = int(code) if isinstance(code, (int, float, str)) else -1
    return ToolsExecuteResponse(
        exit_code=exit_code,
        stdout=str(body.get("stdout") or ""),
        stderr=str(body.get("stderr") or ""),
        stdout_truncated=bool(body.get("stdout_truncated")),
        stderr_truncated=bool(body.get("stderr_truncated")),
        command=str(body.get("command") or operation.command),
        operation=operation.name,
        ok=bool(tool.ok and body.get("ok", tool.ok)),
        error=tool.error or (None if body.get("ok", True) else str(body.get("error") or "")),
        spoken=str(body.get("spoken") or "") or None,
    )


@router.post("/files/read", response_model=FileReadResponse)
async def read_tool_file(
    data: FileReadRequest,
    session: AsyncSession = Depends(get_session),
    ctx=Depends(require_owner_trust),
) -> FileReadResponse:
    result = sandbox.read_file(data.path)
    await log_access(
        session,
        actor=ctx.actor,
        action="tool.file_read",
        endpoint="POST /v1/tools/files/read",
        resource_type="sandbox",
        resource_ids=[],
        details={"path": result["path"], "size_bytes": result["size_bytes"]},
    )
    await session.commit()
    return FileReadResponse(**result)


@router.post("/files/write", response_model=FileWriteResponse)
async def write_tool_file(
    data: FileWriteRequest,
    session: AsyncSession = Depends(get_session),
    ctx=Depends(require_owner_trust),
) -> FileWriteResponse:
    result = sandbox.write_file(data.path, data.content)
    await log_access(
        session,
        actor=ctx.actor,
        action="tool.file_write",
        endpoint="POST /v1/tools/files/write",
        resource_type="sandbox",
        resource_ids=[],
        details={"path": result["path"], "bytes": result["bytes"]},
    )
    await session.commit()
    return FileWriteResponse(**result)
