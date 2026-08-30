"""API schemas for the sandboxed tools surface."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToolsExecuteRequest(BaseModel):
    operation: str = Field(min_length=1, max_length=128)
    cwd: str | None = Field(default=None, max_length=512)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    confirm: bool = False


class ToolsExecuteResponse(BaseModel):
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    command: str
    operation: str | None = None
    ok: bool = True
    error: str | None = None
    needs_confirm: bool = False
    action_id: str | None = None
    spoken: str | None = None


class FileReadRequest(BaseModel):
    path: str = Field(min_length=1, max_length=512)


class FileReadResponse(BaseModel):
    path: str
    size_bytes: int
    content: str
    truncated: bool = False


class FileWriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(default="", max_length=1024 * 1024)


class FileWriteResponse(BaseModel):
    path: str
    bytes: int
