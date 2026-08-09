"""Identity & trust lifecycle API: owner ceremony, recovery, re-verification."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor_context, require_master, ActorContext
from app.db import get_session
from app.identity import service as identity
from app.models import Device, OwnerIdentity, RecoveryCode
from app.utils.text import utcnow
from app.schemas import (
    DeviceOut,
    IdentityStatusOut,
    OwnerCreateRequest,
    OwnerCreateResponse,
    RecoveryCodesResponse,
    RecoveryCodeOut,
    RecoveryRedeemRequest,
    RecoveryRedeemResponse,
    ReverificationConsumeRequest,
    ReverificationConsumeResponse,
    ReverificationRequest,
    ReverificationResponse,
    TrustMatrixOut,
)

router = APIRouter(prefix="/v1/identity", tags=["identity"])


def _http(exc: identity.IdentityError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail=exc.message,
        headers={"X-Error-Code": exc.code},
    )


@router.post("/owner", response_model=OwnerCreateResponse, status_code=201)
async def create_owner(
    data: OwnerCreateRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> OwnerCreateResponse:
    try:
        owner, codes = await identity.create_owner(
            session,
            display_name=data.display_name,
            actor=actor,
        )
    except identity.IdentityError as exc:
        raise _http(exc) from exc
    await session.commit()
    return OwnerCreateResponse(
        owner_id=owner.id,
        display_name=owner.display_name,
        recovery_codes=[
            RecoveryCodeOut(code=c.code, expires_at=c.expires_at, label=c.label)
            for c in codes
        ],
    )


@router.get("/status", response_model=IdentityStatusOut)
async def identity_status(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> IdentityStatusOut:
    owner = await identity.get_owner(session)
    devices_active = 0
    recovery_remaining = 0
    recovery_locked = False
    if owner is not None:
        now = utcnow()
        devices_active = (
            await session.execute(
                select(func.count(Device.id)).where(
                    Device.owner_id == owner.id,
                    Device.revoked_at.is_(None),
                )
            )
        ).scalar_one()
        recovery_remaining = (
            await session.execute(
                select(func.count(RecoveryCode.id)).where(
                    RecoveryCode.owner_id == owner.id,
                    RecoveryCode.consumed_at.is_(None),
                    RecoveryCode.revoked_at.is_(None),
                    RecoveryCode.expires_at > now,
                )
            )
        ).scalar_one()
        recovery_locked = (
            owner.recovery_locked_until is not None and owner.recovery_locked_until > now
        )
    return IdentityStatusOut(
        owner_established=owner is not None,
        owner_id=owner.id if owner else None,
        display_name=owner.display_name if owner else None,
        trust_level=identity.device_trust(ctx.device),
        actor=ctx.actor,
        devices_active=devices_active or 0,
        recovery_codes_remaining=recovery_remaining or 0,
        recovery_locked=recovery_locked,
    )


@router.post("/recovery/codes", response_model=RecoveryCodesResponse)
async def rotate_recovery_codes(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> RecoveryCodesResponse:
    try:
        owner = await identity.require_owner(session)
        codes = await identity.issue_recovery_codes(session, owner_id=owner.id, actor=actor)
    except identity.IdentityError as exc:
        raise _http(exc) from exc
    await session.commit()
    return RecoveryCodesResponse(
        owner_id=owner.id,
        recovery_codes=[
            RecoveryCodeOut(code=c.code, expires_at=c.expires_at, label=c.label)
            for c in codes
        ],
    )


@router.post("/recovery/redeem", response_model=RecoveryRedeemResponse, status_code=201)
async def redeem_recovery(
    data: RecoveryRedeemRequest,
    session: AsyncSession = Depends(get_session),
) -> RecoveryRedeemResponse:
    """Recovery is deliberately unauthenticated: it is the path back in after
    every credential is lost. Protection comes from high-entropy single-use
    codes, expiry, and a brute-force lockout."""
    try:
        device, token, owner = await identity.redeem_recovery_code(
            session,
            code=data.code,
            device_name=data.device_name,
            capabilities=data.capabilities,
        )
    except identity.IdentityError as exc:
        raise _http(exc) from exc
    await session.commit()
    return RecoveryRedeemResponse(
        device=DeviceOut.model_validate(device),
        token=token,
        owner_id=owner.id,
    )


@router.post("/reverification", response_model=ReverificationResponse)
async def create_reverification(
    data: ReverificationRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> ReverificationResponse:
    try:
        owner = await identity.require_owner(session)
        proof, token = await identity.issue_reverification(
            session,
            owner=owner,
            purpose=data.purpose,
            ctx=ctx,
            device=ctx.device,
            voice_session_id=data.voice_session_id,
        )
    except identity.IdentityError as exc:
        raise _http(exc) from exc
    await session.commit()
    return ReverificationResponse(
        token=token,
        purpose=proof.purpose,
        expires_at=proof.expires_at.isoformat(),
    )


@router.post("/reverification/consume", response_model=ReverificationConsumeResponse)
async def consume_reverification(
    data: ReverificationConsumeRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> ReverificationConsumeResponse:
    try:
        await identity.consume_reverification(
            session,
            token=data.token,
            purpose=data.purpose,
            ctx=ctx,
        )
    except identity.IdentityError as exc:
        raise _http(exc) from exc
    await session.commit()
    return ReverificationConsumeResponse(valid=True, purpose=data.purpose)


@router.get("/trust", response_model=TrustMatrixOut)
async def trust_matrix(
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> TrustMatrixOut:
    return TrustMatrixOut(
        owner_required_actions=sorted(identity.OWNER_ACTIONS),
        reverify_required_actions=sorted(identity.REVERIFY_ACTIONS),
        levels=dict(identity.TRUST_RANK),
    )
