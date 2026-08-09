"""Sandboxed code/file tool API (plan 11.4), owner-trusted and audited."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_owner_trust
from app.db import get_session
from app.services.access_log import log_access
from app.tools import sandbox
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
    result = sandbox.run_command(
        data.command,
        cwd=data.cwd,
        timeout_seconds=data.timeout_seconds,
    )
    await log_access(
        session,
        actor=ctx.actor,
        action="tool.execute",
        endpoint="POST /v1/tools/execute",
        resource_type="sandbox",
        resource_ids=[],
        details={"command": result["command"], "exit_code": result["exit_code"]},
    )
    await session.commit()
    return ToolsExecuteResponse(**result)


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
