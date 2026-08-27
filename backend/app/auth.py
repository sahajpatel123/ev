from __future__ import annotations

import secrets
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import Device
from app.utils.text import sha256_hex, utcnow


@dataclass(frozen=True)
class ActorContext:
    """Who is acting: the master key or a registered, non-revoked device."""

    actor: str
    device_id: UUID | None = None
    is_master: bool = False
    device: Device | None = None

    @property
    def is_device(self) -> bool:
        return self.device_id is not None

    @property
    def data_scope(self) -> str:
        """Canonical data scope for THIS caller (G2 ONE-EVIE law).

        Owner identity is device-independent: any non-sandbox trusted device
        resolves to the SAME canonical owner scope, while gateway-paired
        sandbox devices stay isolated regardless of which surface they call.
        Services must receive this scope — never the raw provenance actor.
        """
        from app.everywhere.owner import owner_scope

        return owner_scope(self.actor, device=self.device)


async def require_auth(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="CREDENTIAL_MISSING: Missing bearer token",
            headers={"X-Error-Code": "CREDENTIAL_MISSING"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="CREDENTIAL_MISSING: Empty bearer token",
            headers={"X-Error-Code": "CREDENTIAL_MISSING"},
        )
    if not secrets.compare_digest(token, settings.master_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MASTER_KEY_INVALID: Invalid master key",
            headers={"X-Error-Code": "MASTER_KEY_INVALID"},
        )
    return token


async def require_master(authorization: str | None = Header(default=None)) -> str:
    """Master-key-only gate for privileged surfaces (device management, export).

    A registered device token authenticates the device, but must not grant
    device-credential management or full-data export privileges. Those remain
    exclusive to the user-held master key.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="CREDENTIAL_MISSING: Missing bearer token",
            headers={"X-Error-Code": "CREDENTIAL_MISSING"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="CREDENTIAL_MISSING: Empty bearer token",
            headers={"X-Error-Code": "CREDENTIAL_MISSING"},
        )
    if not secrets.compare_digest(token, settings.master_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="MASTER_KEY_INVALID: Master key required for this operation",
            headers={"X-Error-Code": "MASTER_KEY_INVALID"},
        )
    return "master"


async def _resolve_actor(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> tuple[str, Device | None]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="CREDENTIAL_MISSING: Missing bearer token",
            headers={"X-Error-Code": "CREDENTIAL_MISSING"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="CREDENTIAL_MISSING: Empty bearer token",
            headers={"X-Error-Code": "CREDENTIAL_MISSING"},
        )
    # Master is still accepted for administrative paths, but normal device
    # flow no longer depends on it. Keep this branch for recovery/rotation.
    if secrets.compare_digest(token, settings.master_key):
        return "master", None
    token_hash = sha256_hex(token)
    # Check any device with this hash, including revoked, to distinguish
    # DEVICE_REVOKED vs DEVICE_TOKEN_INVALID.
    result_any = await session.execute(
        select(Device).where(Device.token_hash == token_hash)
    )
    device_any = result_any.scalar_one_or_none()
    if device_any is None:
        # Neither master nor device — distinguish from master-only failure.
        # Keep legacy phrase for backwards compat but prefix with code.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="DEVICE_TOKEN_INVALID: Invalid device token (master mismatch)",
            headers={"X-Error-Code": "DEVICE_TOKEN_INVALID", "X-Master-Mismatch": "true"},
        )
    if device_any.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="DEVICE_REVOKED: Device revoked",
            headers={"X-Error-Code": "DEVICE_REVOKED"},
        )
    device_any.last_seen_at = utcnow()
    return f"device:{device_any.name}", device_any


async def require_actor(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> str:
    """Accept the master key or a registered, non-revoked device token."""
    actor, _ = await _resolve_actor(authorization, session)
    return actor


async def require_actor_context(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> ActorContext:
    """Richer actor resolution for command surfaces that need device scoping."""
    actor, device = await _resolve_actor(authorization, session)
    return ActorContext(
        actor=actor,
        device_id=device.id if device else None,
        is_master=device is None,
        device=device,
    )


async def require_owner_trust(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> ActorContext:
    """Require the master key or an owner-trusted device (biometric-grade).

    Plain devices may chat and capture lightweight context, but enrolling or
    revoking voiceprints and other owner-level operations require a device that
    the owner ceremony explicitly promoted to owner trust.
    """
    ctx = await require_actor_context(authorization, session)
    if ctx.is_master:
        return ctx
    if ctx.device is None or ctx.device.trust_level != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Owner-level trust required for this operation",
            headers={"X-Error-Code": "owner_trust_required"},
        )
    return ctx


def require_reverification(purpose: str):
    """Require a fresh, purpose-bound re-verification proof for a sensitive action.

    The master key is the strongest factor and bypasses the proof (it is the
    recovery root); any device actor must present a proof issued for exactly
    this purpose, bound to the same device, and not yet consumed.
    """

    async def dependency(
        authorization: str | None = Header(default=None),
        x_ev_reverify: str | None = Header(default=None),
        session: AsyncSession = Depends(get_session),
    ) -> ActorContext:
        ctx = await require_actor_context(authorization, session)
        if ctx.is_master:
            return ctx
        if not x_ev_reverify:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Re-verification required for this sensitive action",
                headers={"X-Error-Code": "reverification_required"},
            )
        from app.identity.service import IdentityError, consume_reverification

        try:
            await consume_reverification(
                session,
                token=x_ev_reverify,
                purpose=purpose,
                ctx=ctx,
            )
        except IdentityError as exc:
            raise HTTPException(
                status_code=exc.status,
                detail=exc.message,
                headers={"X-Error-Code": exc.code},
            ) from exc
        return ctx

    return dependency
