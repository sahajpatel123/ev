"""Owner identity and trust lifecycle primitives.

This is the single authorization anchor for EV. The owner record binds trusted
devices, voice enrollment, and recovery material; trust escalation means
lightweight conversation is fine with a verified voice, while sensitive actions
require re-verification (a fresh, purpose-bound proof) even inside an unlocked
voice session. Recovery codes provide a safe path back in when a device is lost
or credentials change, without ever storing the codes in plaintext.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.models import (
    Device,
    OwnerIdentity,
    PasskeyCredential,
    RecoveryCode,
    ReVerificationProof,
)
from app.services.access_log import log_access
from app.utils.text import sha256_hex, utcnow

# Trust levels, ordered weakest to strongest. `guest` is reserved so a future
# guest mode is additive rather than a rewrite of every check.
TRUST_GUEST = "guest"
TRUST_DEVICE = "device"
TRUST_OWNER = "owner"
TRUST_MASTER = "master"

TRUST_RANK = {
    TRUST_GUEST: 0,
    TRUST_DEVICE: 1,
    TRUST_OWNER: 2,
    TRUST_MASTER: 3,
}

# Actions a plain (non-master) device may never perform without owner trust.
OWNER_ACTIONS = {
    "voice.enroll",
    "voice.revoke",
    "voice.delete",
    "voice.export",
    "memory.delete",
    "memory.export",
    "device.manage",
    "identity.manage",
    "recovery.rotate",
}

# Sensitive actions that require a fresh, purpose-bound re-verification proof
# even when the voice session is already unlocked.
REVERIFY_ACTIONS = {
    "integration.action",
    "memory.delete",
    "runtime.action",
    "voice.revoke",
    "voice.delete",
    "recovery.rotate",
    "voice.sensitive_action",
}

RECOVERY_CODE_COUNT = 8
RECOVERY_CODE_TTL_DAYS = 30
RECOVERY_MAX_FAILURES = 5
RECOVERY_LOCK_MINUTES = 15
REVERIFY_TTL_SECONDS = 300


class IdentityError(Exception):
    """Domain error with HTTP-ish status and stable error code."""

    def __init__(self, message: str, *, status: int = 400, code: str = "identity_error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@dataclass(frozen=True)
class RecoveryCodeIssue:
    code: str
    expires_at: str | None
    label: str | None = None


def device_trust(device: Device | None) -> str:
    """Master is implicit (no device row). Devices carry an explicit trust level."""
    if device is None:
        return TRUST_MASTER
    return device.trust_level or TRUST_DEVICE


def trust_allows(action: str, ctx: ActorContext, device: Device | None = None) -> bool:
    """Graded permission check: guest < device < owner < master."""
    level = device_trust(device)
    if ctx.is_master:
        return True
    if device is None or device.id != ctx.device_id:
        return False
    required = TRUST_OWNER if action in OWNER_ACTIONS else TRUST_DEVICE
    return TRUST_RANK.get(level, 0) >= TRUST_RANK[required]


def _new_recovery_code() -> str:
    # ~80 bits of entropy per code, formatted for humans.
    groups = []
    raw = secrets.token_bytes(10)
    for i in range(0, 10, 2):
        groups.append(raw[i : i + 2].hex().upper())
    return "-".join(groups)


def as_utc(value: datetime | None) -> datetime | None:
    """Normalize to tz-aware UTC; None stays None (no expiry / no lock)."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


async def get_owner(session: AsyncSession) -> OwnerIdentity | None:
    result = await session.execute(
        select(OwnerIdentity).order_by(OwnerIdentity.created_at.asc()).limit(1)
    )
    return result.scalar_one_or_none()


async def require_owner(session: AsyncSession) -> OwnerIdentity:
    owner = await get_owner(session)
    if owner is None:
        raise IdentityError(
            "Owner identity not established — create it before enrolling devices or voice",
            status=428,
            code="owner_not_established",
        )
    return owner


async def create_owner(
    session: AsyncSession,
    *,
    display_name: str,
    actor: str,
) -> tuple[OwnerIdentity, list[RecoveryCodeIssue]]:
    """Master-only ceremony: establish the single owner record + recovery codes."""
    if await get_owner(session) is not None:
        raise IdentityError(
            "Owner identity already exists",
            status=409,
            code="owner_exists",
        )
    owner = OwnerIdentity(display_name=display_name.strip() or "Owner")
    session.add(owner)
    await session.flush()
    codes = await issue_recovery_codes(
        session,
        owner_id=owner.id,
        actor=actor,
    )
    await log_access(
        session,
        actor=actor,
        action="identity_owner_create",
        endpoint="POST /v1/identity/owner",
        resource_type="owner",
        resource_ids=[owner.id],
        details={"display_name": owner.display_name, "recovery_codes": len(codes)},
    )
    return owner, codes


async def issue_recovery_codes(
    session: AsyncSession,
    *,
    owner_id: UUID,
    actor: str,
    count: int = RECOVERY_CODE_COUNT,
) -> list[RecoveryCodeIssue]:
    """Rotate recovery codes. Existing unconsumed codes are revoked."""
    owner = await session.get(OwnerIdentity, owner_id)
    if owner is None:
        raise IdentityError("Owner not found", status=404, code="owner_not_found")
    now = utcnow()
    result = await session.execute(
        select(RecoveryCode).where(
            RecoveryCode.owner_id == owner.id,
            RecoveryCode.consumed_at.is_(None),
            RecoveryCode.revoked_at.is_(None),
        )
    )
    for row in result.scalars().all():
        row.revoked_at = now
        row.revoked_reason = "rotated"
    codes: list[RecoveryCodeIssue] = []
    for i in range(count):
        raw = _new_recovery_code()
        expires_at = now + timedelta(days=RECOVERY_CODE_TTL_DAYS)
        session.add(
            RecoveryCode(
                owner_id=owner.id,
                code_hash=sha256_hex(raw),
                label=f"recovery-{i + 1}",
                expires_at=expires_at,
            )
        )
        codes.append(RecoveryCodeIssue(code=raw, expires_at=expires_at.isoformat(), label=f"recovery-{i + 1}"))
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="recovery_codes_issue",
        endpoint="POST /v1/identity/recovery/codes",
        resource_type="owner",
        resource_ids=[owner.id],
        details={"count": len(codes), "previous_revoked": True},
    )
    return codes


async def redeem_recovery_code(
    session: AsyncSession,
    *,
    code: str,
    device_name: str,
    capabilities: list[str] | None = None,
) -> tuple[Device, str, OwnerIdentity]:
    """Recovery path intentionally does not require the master key or a device.

    A valid code proves ownership and resets the device fleet: every previously
    trusted device is revoked (a lost device may be in the wrong hands) and a
    fresh owner-trusted device token is issued.
    """
    owner = await require_owner(session)
    now = utcnow()
    locked_until = owner.recovery_locked_until
    locked_until_aware = as_utc(locked_until) if locked_until is not None else None
    if locked_until_aware is not None and locked_until_aware > now:
        raise IdentityError(
            "Recovery temporarily locked after too many failed attempts — try again later",
            status=429,
            code="recovery_locked",
        )
    code_hash = sha256_hex(code.strip().upper())
    result = await session.execute(
        select(RecoveryCode).where(RecoveryCode.code_hash == code_hash)
    )
    row = result.scalar_one_or_none()
    expires_at = as_utc(row.expires_at) if row is not None else None
    if (
        row is None
        or row.owner_id != owner.id
        or row.consumed_at is not None
        or row.revoked_at is not None
        or (expires_at is not None and expires_at < now)
    ):
        owner.recovery_failures += 1
        if owner.recovery_failures >= RECOVERY_MAX_FAILURES:
            owner.recovery_locked_until = now + timedelta(minutes=RECOVERY_LOCK_MINUTES)
            owner.recovery_failures = 0
        await log_access(
            session,
            actor="recovery",
            action="recovery_redeem_failed",
            endpoint="POST /v1/identity/recovery/redeem",
            resource_type="owner",
            resource_ids=[owner.id],
            details={"failures": owner.recovery_failures},
        )
        raise IdentityError(
            "Invalid or expired recovery code",
            status=401,
            code="recovery_invalid",
        )

    row.consumed_at = now
    token = secrets.token_urlsafe(32)
    device = Device(
        name=device_name.strip() or "Recovered device",
        token_hash=sha256_hex(token),
        capabilities=capabilities or [],
        owner_id=owner.id,
        trust_level=TRUST_OWNER,
    )
    session.add(device)
    await session.flush()

    others = await session.execute(
        select(Device).where(Device.id != device.id, Device.revoked_at.is_(None))
    )
    revoked: list[Device] = []
    for old in others.scalars().all():
        old.revoked_at = now
        old.revoked_reason = "recovery redeemed"
        revoked.append(old)
    owner.recovery_failures = 0
    owner.recovery_locked_until = None
    await log_access(
        session,
        actor="recovery",
        action="recovery_redeem",
        endpoint="POST /v1/identity/recovery/redeem",
        resource_type="device",
        resource_ids=[device.id],
        details={"owner_id": str(owner.id), "devices_revoked": len(revoked)},
    )
    return device, token, owner


async def issue_reverification(
    session: AsyncSession,
    *,
    owner: OwnerIdentity,
    purpose: str,
    ctx: ActorContext,
    device: Device | None = None,
    voice_session_id: UUID | None = None,
) -> tuple[ReVerificationProof, str]:
    """Issue a fresh, purpose-bound proof for a sensitive action."""
    if purpose not in REVERIFY_ACTIONS:
        raise IdentityError(
            f"Purpose {purpose!r} does not require re-verification",
            status=422,
            code="reverify_purpose_unsupported",
        )
    if not ctx.is_master and device is None:
        device = await session.get(Device, ctx.device_id) if ctx.device_id else None
    token = secrets.token_urlsafe(32)
    now = utcnow()
    proof = ReVerificationProof(
        owner_id=owner.id,
        device_id=device.id if device else None,
        voice_session_id=voice_session_id,
        purpose=purpose,
        token_hash=sha256_hex(token),
        expires_at=now + timedelta(seconds=REVERIFY_TTL_SECONDS),
    )
    session.add(proof)
    await session.flush()
    await log_access(
        session,
        actor="master" if ctx.is_master else "device",
        action="reverification_issue",
        endpoint="POST /v1/identity/reverification",
        resource_type="owner",
        resource_ids=[owner.id],
        details={"purpose": purpose, "device_id": str(device.id) if device else None},
    )
    return proof, token


async def consume_reverification(
    session: AsyncSession,
    *,
    token: str,
    purpose: str,
    ctx: ActorContext,
) -> ReVerificationProof:
    """Validate and single-use consume a re-verification proof."""
    token_hash = sha256_hex(token)
    result = await session.execute(
        select(ReVerificationProof).where(ReVerificationProof.token_hash == token_hash)
    )
    proof = result.scalar_one_or_none()
    now = utcnow()
    proof_expires_at = as_utc(proof.expires_at) if proof is not None else None
    if (
        proof is None
        or proof.purpose != purpose
        or proof.consumed_at is not None
        or (proof_expires_at is not None and proof_expires_at < now)
        or (proof.device_id is not None and proof.device_id != ctx.device_id)
    ):
        raise IdentityError(
            "Invalid, expired, consumed, or device-mismatched re-verification proof",
            status=403,
            code="reverification_rejected",
        )
    proof.consumed_at = now
    await log_access(
        session,
        actor="master" if ctx.is_master else "device",
        action="reverification_consume",
        endpoint="POST /v1/identity/reverification/consume",
        resource_type="owner",
        resource_ids=[proof.owner_id] if proof.owner_id else [],
        details={"purpose": purpose, "proof_id": str(proof.id)},
    )
    return proof


async def register_passkey(
    session: AsyncSession,
    *,
    owner: OwnerIdentity,
    credential_id: str,
    name: str,
    actor: str,
    device_id: UUID | None = None,
) -> PasskeyCredential:
    """Bind a WebAuthn passkey to the owner record (credential ID hashed at rest)."""
    if len(credential_id) < 8:
        raise IdentityError(
            "credential_id looks too short to be a WebAuthn credential",
            status=422,
            code="passkey_credential_invalid",
        )
    credential_hash = sha256_hex(credential_id)
    result = await session.execute(
        select(PasskeyCredential).where(
            PasskeyCredential.credential_id_hash == credential_hash,
            PasskeyCredential.revoked_at.is_(None),
        )
    )
    if result.scalar_one_or_none() is not None:
        raise IdentityError(
            "Passkey already registered",
            status=409,
            code="passkey_exists",
        )
    if device_id is not None:
        device = await session.get(Device, device_id)
        if device is None or device.owner_id != owner.id:
            raise IdentityError(
                "Device does not belong to the owner",
                status=422,
                code="passkey_device_mismatch",
            )
    row = PasskeyCredential(
        owner_id=owner.id,
        device_id=device_id,
        credential_id_hash=credential_hash,
        name=name.strip() or "passkey",
    )
    session.add(row)
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="passkey_register",
        endpoint="POST /v1/identity/passkeys",
        resource_type="passkey",
        resource_ids=[row.id],
        details={"owner_id": str(owner.id), "device_id": str(device_id) if device_id else None},
    )
    return row


async def list_passkeys(
    session: AsyncSession,
    *,
    owner_id: UUID,
) -> list[PasskeyCredential]:
    result = await session.execute(
        select(PasskeyCredential)
        .where(
            PasskeyCredential.owner_id == owner_id,
            PasskeyCredential.revoked_at.is_(None),
        )
        .order_by(PasskeyCredential.created_at.asc())
    )
    return list(result.scalars().all())


async def revoke_passkey(
    session: AsyncSession,
    *,
    passkey_id: UUID,
    owner_id: UUID,
    reason: str,
    actor: str,
) -> PasskeyCredential:
    row = await session.get(PasskeyCredential, passkey_id)
    if row is None or row.owner_id != owner_id:
        raise IdentityError("Passkey not found", status=404, code="passkey_not_found")
    now = utcnow()
    row.revoked_at = now
    row.revoked_reason = reason
    await log_access(
        session,
        actor=actor,
        action="passkey_revoke",
        endpoint=f"DELETE /v1/identity/passkeys/{row.id}",
        resource_type="passkey",
        resource_ids=[row.id],
        details={"owner_id": str(owner_id), "reason": reason},
    )
    return row
