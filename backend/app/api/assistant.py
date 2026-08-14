"""Assistant identity, protocols, dedication, callouts, quiet hours."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor
from app.db import get_session
from app.ev import assistant as assistant_mod
from app.ev import callouts as callout_mod
from app.ev import protocols as protocol_mod
from app.notify.proactive import persist_quiet_hours, set_quiet_hours
from app.schemas import (
    AssistantNameRequest,
    AssistantProfileOut,
    CalloutOut,
    DedicationSetRequest,
    ProtocolSheetOut,
    QuietHoursRequest,
)

router = APIRouter(prefix="/v1/assistant")


@router.get("/profile", response_model=AssistantProfileOut)
async def get_profile(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> AssistantProfileOut:
    profile = await assistant_mod.get_profile(session)
    await session.commit()
    return AssistantProfileOut(
        nickname=profile.nickname,
        owner_preferred_name=profile.owner_preferred_name,
        greeting_enabled=profile.greeting_enabled,
        live_conversation_id=profile.live_conversation_id,
        onboarding_completed_at=profile.onboarding_completed_at,
        dedication_text=profile.dedication_text,
        dedication_played_at=profile.dedication_played_at,
        training_wheels_started_at=profile.training_wheels_started_at,
        training_wheels_completed_at=profile.training_wheels_completed_at,
    )


@router.post("/name", response_model=AssistantProfileOut)
async def set_name(
    data: AssistantNameRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> AssistantProfileOut:
    decision = await assistant_mod.set_nickname(session, data.name)
    if not decision.ok:
        raise HTTPException(status_code=400, detail=decision.reason or "invalid_name")
    profile = await assistant_mod.get_profile(session)
    await session.commit()
    return AssistantProfileOut(
        nickname=profile.nickname,
        owner_preferred_name=profile.owner_preferred_name,
        greeting_enabled=profile.greeting_enabled,
        live_conversation_id=profile.live_conversation_id,
        onboarding_completed_at=profile.onboarding_completed_at,
        dedication_text=profile.dedication_text,
        dedication_played_at=profile.dedication_played_at,
        training_wheels_started_at=profile.training_wheels_started_at,
        training_wheels_completed_at=profile.training_wheels_completed_at,
    )


@router.post("/name/reset", response_model=AssistantProfileOut)
async def reset_name(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> AssistantProfileOut:
    await assistant_mod.reset_nickname(session)
    profile = await assistant_mod.get_profile(session)
    await session.commit()
    return AssistantProfileOut(
        nickname=profile.nickname,
        owner_preferred_name=profile.owner_preferred_name,
        greeting_enabled=profile.greeting_enabled,
        live_conversation_id=profile.live_conversation_id,
        onboarding_completed_at=profile.onboarding_completed_at,
        dedication_text=profile.dedication_text,
        dedication_played_at=profile.dedication_played_at,
        training_wheels_started_at=profile.training_wheels_started_at,
        training_wheels_completed_at=profile.training_wheels_completed_at,
    )


@router.get("/protocols", response_model=ProtocolSheetOut)
async def get_protocols(
    include_refused: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> ProtocolSheetOut:
    payload = await protocol_mod.capability_reply(session, include_refused=include_refused)
    await session.commit()
    return ProtocolSheetOut(
        protocols=payload["protocols"],
        enabled=payload["enabled"],
        hud=payload["hud"],
    )


@router.post("/dedication")
async def set_dedication(
    data: DedicationSetRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    try:
        profile = await assistant_mod.set_dedication(
            session, text=data.text, blob_id=data.blob_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return {"ok": True, "text": profile.dedication_text, "blob_id": profile.dedication_blob_id}


@router.post("/dedication/play")
async def play_dedication(
    auto: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    payload = await assistant_mod.play_dedication(session, auto=auto)
    await session.commit()
    return payload


@router.get("/callouts", response_model=list[CalloutOut])
async def get_callouts(
    limit: int = Query(default=10, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[CalloutOut]:
    rows = await callout_mod.list_callouts(session, limit=limit)
    return [CalloutOut.model_validate(row) for row in rows]


@router.post("/callouts")
async def post_callout(
    data: dict,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    text = str(data.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    row = await callout_mod.emit_callout(
        session,
        text,
        source=str(data.get("source") or "api"),
        hud=data.get("hud") if isinstance(data.get("hud"), dict) else None,
        emergency=bool(data.get("emergency")),
        tts_available=data.get("tts_available", True),
    )
    await session.commit()
    return {
        "id": str(row.id),
        "text": row.text,
        "spoken": row.spoken,
        "source": row.source,
    }


@router.post("/quiet-hours")
async def post_quiet_hours(
    data: QuietHoursRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    try:
        hours = set_quiet_hours(until=data.until, start=data.start, end=data.end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await persist_quiet_hours(session)
    await session.commit()
    return hours


@router.post("/training-wheels/start")
async def start_wheels(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    payload = await protocol_mod.start_training_wheels(session)
    await session.commit()
    return payload


@router.post("/training-wheels/complete")
async def complete_wheels(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    payload = await protocol_mod.complete_training_wheels(session)
    await session.commit()
    return payload
