"""Pairing and device credentials. No provider keys. No amateur crypto."""

from __future__ import annotations

import hmac
import secrets
import time
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.config import settings
from app.db import get_session
from app.models import Device, DevicePairingToken, OwnerIdentity
from app.utils.text import sha256_hex, utcnow

from . import PAIRABLE_ROLES, PROTOCOL_VERSION
from .sandbox import is_sandbox_device, memory_scope_of
from .telemetry import emit


def _when(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _hmac_key() -> bytes:
    return sha256(f"{settings.master_key}|device-gateway-v1".encode()).digest()


def issue_access_token(device: Device) -> str:
    exp = int(time.time()) + max(60, int(settings.device_access_ttl_seconds))
    revision = int(getattr(device, "auth_revision", 1) or 1)
    payload = f"{device.id}|{exp}|{memory_scope_of(device)}|{revision}"
    digest = hmac.new(_hmac_key(), payload.encode(), sha256).hexdigest()[:32]
    return f"evie1.{payload}.{digest}"


def parse_access_token(token: str) -> tuple[UUID, int, str, int] | None:
    if not token.startswith("evie1."):
        return None
    try:
        _, payload, digest = token.split(".", 2)
        expected = hmac.new(_hmac_key(), payload.encode(), sha256).hexdigest()[:32]
        if not hmac.compare_digest(digest, expected):
            return None
        parts = payload.split("|")
        if len(parts) == 3:
            device_id, exp_raw, scope = parts
            revision = 0
        elif len(parts) == 4:
            device_id, exp_raw, scope, rev_raw = parts
            revision = int(rev_raw)
        else:
            return None
        exp = int(exp_raw)
        if exp < int(time.time()):
            return None
        return UUID(device_id), exp, scope, revision
    except (ValueError, TypeError):
        return None


async def create_pairing_token(
    session: AsyncSession,
    *,
    role: str,
    display_name: str,
) -> tuple[DevicePairingToken, str]:
    role = (role or "companion").strip().lower()
    if role not in PAIRABLE_ROLES:
        raise HTTPException(status_code=400, detail="Role is not pairable for PWA devices")
    raw = "evie-pair." + secrets.token_urlsafe(24)
    row = DevicePairingToken(
        token_hash=sha256_hex(raw),
        role=role,
        display_name=(display_name or "Evie phone")[:128],
        expires_at=utcnow() + timedelta(seconds=max(60, settings.pairing_ttl_seconds)),
    )
    session.add(row)
    await session.flush()
    return row, raw


async def pair_device(
    session: AsyncSession,
    *,
    pairing_token: str,
    display_name: str | None,
    capabilities: list[str],
    client_version: str | None,
    protocol_version: str | None,
    platform: str = "web",
) -> tuple[Device, str]:
    token_hash = sha256_hex((pairing_token or "").strip())
    row = (
        await session.execute(select(DevicePairingToken).where(DevicePairingToken.token_hash == token_hash))
    ).scalar_one_or_none()
    if row is None or row.used_at is not None or (_when(row.expires_at) or utcnow()) <= utcnow():
        raise HTTPException(status_code=401, detail="Invalid or expired pairing token")
    owner = (
        await session.execute(select(OwnerIdentity).order_by(OwnerIdentity.created_at.asc()).limit(1))
    ).scalar_one_or_none()
    device_token = secrets.token_urlsafe(32)
    device = Device(
        name=(display_name or row.display_name)[:128],
        token_hash=sha256_hex(device_token),
        capabilities=list(capabilities or ["foreground_voice", "camera", "text"]),
        trust_level="device",
        owner_id=owner.id if owner else None,
        device_type="phone",
        platform=platform[:32],
        paired_at=utcnow(),
        role=row.role,
        memory_scope="sandbox",
        client_version=(client_version or "")[:64] or None,
        protocol_version=(protocol_version or PROTOCOL_VERSION)[:16],
    )
    session.add(device)
    await session.flush()
    row.used_at = utcnow()
    row.device_id = device.id
    return device, device_token


async def resolve_gateway_device(
    session: AsyncSession,
    authorization: str | None,
) -> Device:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Empty bearer token")
    parsed = parse_access_token(token)
    if parsed is not None:
        device_id, _, _, token_revision = parsed
        device = await session.get(Device, device_id)
        if device is None or device.revoked_at is not None:
            raise HTTPException(status_code=401, detail="Invalid or revoked device")
        current_revision = int(getattr(device, "auth_revision", 1) or 1)
        # v1 tokens (revision 0) are valid only before the first trust bump.
        if token_revision not in {0, current_revision}:
            raise HTTPException(status_code=401, detail="Access token expired after a trust change")
        if token_revision == 0 and current_revision != 1:
            raise HTTPException(status_code=401, detail="Access token expired after a trust change")
        device.last_seen_at = utcnow()
        return device
    token_hash = sha256_hex(token)
    device = (
        await session.execute(
            select(Device).where(Device.token_hash == token_hash, Device.revoked_at.is_(None))
        )
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked device token")
    device.last_seen_at = utcnow()
    return device


async def require_gateway_device(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Device:
    return await resolve_gateway_device(session, authorization)


def actor_for(device: Device) -> ActorContext:
    return ActorContext(
        actor=f"device:{device.name}",
        device_id=device.id,
        is_master=False,
        device=device,
    )


def assert_not_sandbox_production(device: Device | None) -> None:
    if is_sandbox_device(device):
        emit(
            "cross_platform.memory_scope_violation",
            device_id=str(device.id) if device is not None else None,
            memory_scope="sandbox",
        )
        raise HTTPException(
            status_code=403,
            detail="Sandbox devices cannot access production Memory OS",
            headers={"X-Error-Code": "sandbox_memory_blocked"},
        )
