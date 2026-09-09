"""Cognitive Kernel HTTP surface. Voice Edge posts OwnerTurns here."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_master
from app.cognitive.kernel import handle_turn
from app.cognitive.mode import is_voice_edge, muse_kernel_active
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
    if not muse_kernel_active():
        raise HTTPException(status_code=409, detail="cognitive_mode is not muse_kernel")
    if is_voice_edge():
        raise HTTPException(status_code=409, detail="this process is the voice edge, not the kernel")
    result = await handle_turn(
        transcript=body.transcript,
        live_session_id=body.live_session_id,
        device_id=body.device_id,
        modality=body.modality,
        session=session,
    )
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
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return dict(result) if isinstance(result, dict) else {"ok": True, "result": result}


@router.get("/health")
async def cognitive_health(_: str = Depends(require_master)) -> dict[str, Any]:
    from app.cognitive.mode import cognitive_mode, cognitive_role

    from app.gateway.muse import muse_counters_snapshot, muse_spark_base_url, muse_spark_inference_route

    muse = muse_counters_snapshot()
    return {
        "mode": cognitive_mode(),
        "role": cognitive_role(),
        "muse_kernel": muse_kernel_active(),
        "muse_provider": muse_spark_inference_route(),
        "muse_base_url": muse_spark_base_url(),
        "spark_calls": muse.get("spark_calls", 0),
        "spark_meta_calls": muse.get("spark_meta_calls", 0),
        "spark_zen_calls": muse.get("spark_zen_calls", 0),
        "telemetry": snapshot(),
    }
