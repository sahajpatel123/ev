"""Cognitive Kernel HTTP surface. Voice Edge posts OwnerTurns here."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_master
from app.cognitive.kernel import handle_turn
from app.cognitive.mode import is_voice_edge
from app.cognitive.telemetry import snapshot
from app.db import get_session

router = APIRouter(prefix="/v1/cognitive", tags=["cognitive"])


class TurnIn(BaseModel):
    transcript: str = Field(min_length=1, max_length=8000)
    live_session_id: str | None = None
    device_id: str | None = None
    modality: str = "voice"


class MacExecuteIn(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    live_session_id: str | None = None


@router.post("/turn")
async def cognitive_turn(
    body: TurnIn,
    session: AsyncSession = Depends(get_session),
    _: str = Depends(require_master),
) -> dict[str, Any]:
    if is_voice_edge():
        raise HTTPException(status_code=409, detail="this process is the voice edge, not the kernel")
    result = await handle_turn(
        transcript=body.transcript,
        live_session_id=body.live_session_id,
        device_id=body.device_id,
        modality=body.modality,
        session=session,
    )
    # This endpoint owns the session it was handed (`get_session` never
    # commits). Without this, a tool that wrote state was flushed and then
    # rolled back while the response still described the action as done.
    await session.commit()
    return result.as_dict()


@router.post("/mac-execute")
async def mac_execute(
    body: MacExecuteIn,
    session: AsyncSession = Depends(get_session),
    _: str = Depends(require_master),
) -> dict[str, Any]:
    """Voice Edge: run a Mac-bound tool through the live session when present."""

    from app.ev.tools import dispatch
    from app.voice.live.layer import active_lives

    lives = active_lives()
    live_id = body.live_session_id
    if lives and not live_id:
        live_id = str(lives[0].session_id)
    try:
        result = await dispatch(
            session,
            body.name,
            dict(body.arguments or {}),
            actor="master",
            allow_sensitive=True,
            live_session_id=live_id,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": "CAPABILITY_UNAVAILABLE",
            "diagnosis": type(exc).__name__,
            "spoken": "I couldn't complete that on the Mac.",
        }
    # The kernel forwards Mac-bound tools here and trusts the reply, so a write
    # must be durable before this returns. `get_session` never commits: without
    # this the timer/reminder/goal was flushed, rolled back on close, and the
    # owner was still told it was set.
    await session.commit()
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return dict(result) if isinstance(result, dict) else {"ok": True, "result": result}


@router.get("/health")
async def cognitive_health(_: str = Depends(require_master)) -> dict[str, Any]:
    from app.cognitive.mode import cognitive_mode, cognitive_role
    from app.gateway.roles import text_role_available, text_role_model

    return {
        "mode": cognitive_mode(),
        "role": cognitive_role(),
        "kernel": True,
        "mimo_model": text_role_model(),
        "mimo_available": text_role_available(),
        "telemetry": snapshot(),
    }


class DelegateIn(BaseModel):
    task: str = Field(min_length=1, max_length=8000)
    request_id: str = Field(min_length=1, max_length=256)
    live_session_id: str | None = None
    device_id: str | None = None


@router.post("/delegations")
async def cognitive_delegate(body: DelegateIn, _: str = Depends(require_master)) -> dict[str, Any]:
    from app.cognitive.delegation import submit_delegate

    return await submit_delegate(**body.model_dump())


@router.get("/delegations/{job_id}")
async def cognitive_delegate_status(job_id: str, _: str = Depends(require_master)) -> dict[str, Any]:
    from app.cognitive.delegation import get_delegate

    receipt = await get_delegate(job_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="delegated task not found")
    return receipt


@router.post("/delegations/{job_id}/cancel")
async def cognitive_delegate_cancel(job_id: str, _: str = Depends(require_master)) -> dict[str, Any]:
    from app.cognitive.delegation import cancel_delegate

    receipt = await cancel_delegate(job_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="delegated task not found")
    return receipt


@router.get("/delegations")
async def cognitive_delegate_list(
    live_session_id: str | None = None, device_id: str | None = None,
    _: str = Depends(require_master),
) -> dict[str, Any]:
    from app.cognitive.delegation import list_delegates

    return {"tasks": await list_delegates(live_session_id=live_session_id, device_id=device_id)}
