"""Evie file-sandbox API: one jail for Mac, iPhone, and the brain.

Every endpoint requires owner trust (master key or trusted device) and
returns the same receipt shape the Mac runner and iPhone relay use, so a
command spoken on either surface runs the same executor with the same
confirm/dry-run gates.

Mutating verbs accept confirm + dry_run:
  dry_run=True  -> validate, change nothing, needs_confirm=True on success
  confirm=False -> mutating op parks with needs_confirm=True (unless
                   EV_FILE_SANDBOX_AUTONOMY=auto)
  confirm=True  -> execute and verify

Brain lanes (/brain/run, /brain/test) hand the whole turn to Muse Spark
1.3 Contributor: Spark plans {ops}, the runner tests then executes.
Degraded (no key) still runs via the deterministic parser with
degraded=True — never faked as intelligence.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_owner_trust
from app.db import get_session
from app.services.access_log import log_access

router = APIRouter(prefix="/v1/file-sandbox", tags=["file-sandbox"])


class OpRequest(BaseModel):
    path: str | None = Field(default=None, max_length=1024)
    query: str | None = Field(default=None, max_length=512)
    content: str | None = Field(default=None, max_length=256 * 1024)
    dest: str | None = Field(default=None, max_length=1024)
    kind: str | None = Field(default=None, max_length=64)
    limit: int | None = Field(default=None, ge=1, le=200)
    origin: str = Field(default="api", max_length=32)
    confirm: bool = False
    dry_run: bool = False


class ExecuteRequest(BaseModel):
    op: str = Field(min_length=1, max_length=32)
    args: dict[str, Any] = Field(default_factory=dict)
    origin: str = Field(default="api", max_length=32)
    confirm: bool = False
    dry_run: bool = False


class BrainRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    origin: str = Field(default="api", max_length=32)
    confirm: bool = False
    dry_run: bool = False


class IndexRequest(BaseModel):
    rebuild: bool = False
    origin: str = Field(default="api", max_length=32)


def _args(req: OpRequest) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if req.path is not None:
        out["path"] = req.path
    if req.query is not None:
        out["query"] = req.query
    if req.content is not None:
        out["content"] = req.content
    if req.dest is not None:
        out["dest"] = req.dest
    if req.kind is not None:
        out["kind"] = req.kind
    if req.limit is not None:
        out["limit"] = req.limit
    return out


async def _audit(session: AsyncSession, ctx: Any, op: str, receipt: dict[str, Any]) -> None:
    try:
        await log_access(
            session, actor=getattr(ctx, "actor", "owner"), action=f"file_sandbox.{op}",
            endpoint="/v1/file-sandbox", resource_type="file",
            resource_ids=[str(receipt.get("path") or "")] if receipt.get("path") else [],
            details={"op": op, "ok": receipt.get("ok"), "origin": receipt.get("origin"), "dry_run": receipt.get("dry_run")},
        )
        await session.commit()
    except Exception:
        pass


@router.post("/execute")
async def execute(
    data: ExecuteRequest, session: AsyncSession = Depends(get_session), ctx=Depends(require_owner_trust),
) -> dict[str, Any]:
    from app.ev import file_sandbox

    receipt = file_sandbox.execute_op(data.op, data.args, origin=data.origin or "api", confirm=data.confirm, dry_run=data.dry_run)
    await _audit(session, ctx, data.op, receipt)
    return receipt


@router.post("/discover")
async def discover(data: OpRequest, session: AsyncSession = Depends(get_session), ctx=Depends(require_owner_trust)) -> dict[str, Any]:
    from app.ev import file_sandbox

    receipt = file_sandbox.discover(origin=data.origin or "api")
    await _audit(session, ctx, "discover", receipt)
    return receipt


@router.post("/index")
async def index(data: IndexRequest, session: AsyncSession = Depends(get_session), ctx=Depends(require_owner_trust)) -> dict[str, Any]:
    from app.ev import file_sandbox

    receipt = file_sandbox.index_status(rebuild=data.rebuild, origin=data.origin or "api")
    await _audit(session, ctx, "index", receipt)
    return receipt


@router.post("/search")
async def search(data: OpRequest, session: AsyncSession = Depends(get_session), ctx=Depends(require_owner_trust)) -> dict[str, Any]:
    from app.ev import file_sandbox

    receipt = file_sandbox.search(data.query or "", kind=data.kind or "", limit=data.limit or 40, origin=data.origin or "api")
    await _audit(session, ctx, "search", receipt)
    return receipt


def _op_endpoint(op: str):  # factory keeps the 10 verb routes identical
    async def handler(data: OpRequest, session: AsyncSession = Depends(get_session), ctx=Depends(require_owner_trust)) -> dict[str, Any]:
        from app.ev import file_sandbox

        receipt = file_sandbox.execute_op(op, _args(data), origin=data.origin or "api", confirm=data.confirm, dry_run=data.dry_run)
        await _audit(session, ctx, op, receipt)
        return receipt

    handler.__name__ = f"file_sandbox_{op}"
    return handler


# DDL/DML verbs share one shape; registered explicitly for OpenAPI/contract.
read = _op_endpoint("read")
lst = _op_endpoint("list")
write = _op_endpoint("write")
edit = _op_endpoint("edit")
append = _op_endpoint("append")
mkdir = _op_endpoint("mkdir")
delete = _op_endpoint("delete")
copy = _op_endpoint("copy")
move = _op_endpoint("move")
rename = _op_endpoint("rename")
run = _op_endpoint("run")

router.post("/read")(read)
router.post("/list")(lst)
router.post("/write")(write)
router.post("/edit")(edit)
router.post("/append")(append)
router.post("/mkdir")(mkdir)
router.post("/delete")(delete)
router.post("/copy")(copy)
router.post("/move")(move)
router.post("/rename")(rename)
router.post("/run")(run)


@router.post("/undo")
async def undo(data: OpRequest, session: AsyncSession = Depends(get_session), ctx=Depends(require_owner_trust)) -> dict[str, Any]:
    from app.ev import file_sandbox

    receipt = file_sandbox.undo(origin=data.origin or "api")
    await _audit(session, ctx, "undo", receipt)
    return receipt


@router.post("/brain/run")
async def brain_run(data: BrainRequest, session: AsyncSession = Depends(get_session), ctx=Depends(require_owner_trust)) -> dict[str, Any]:
    from app.ev import brain_file_runner

    receipt = await brain_file_runner.run_brain_command(data.text, origin=data.origin or "api", confirm=data.confirm, dry_run=data.dry_run)
    await _audit(session, ctx, "brain_run", {"path": "", "ok": receipt.get("ok"), "origin": receipt.get("origin"), "dry_run": receipt.get("dry_run")})
    return receipt


@router.post("/brain/test")
async def brain_test(data: BrainRequest, session: AsyncSession = Depends(get_session), ctx=Depends(require_owner_trust)) -> dict[str, Any]:
    """Brain tests first (dry-run), then runs for real. The verify lane."""
    from app.ev import brain_file_runner

    receipt = await brain_file_runner.test_brain_command(data.text, origin=data.origin or "api")
    await _audit(session, ctx, "brain_test", {"path": "", "ok": receipt.get("ok"), "origin": receipt.get("origin"), "dry_run": False})
    return receipt
