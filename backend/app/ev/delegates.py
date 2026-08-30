"""Time- and scope-boxed delegates. Never a second owner; never life/home/drone/panic."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from dateutil import parser as date_parser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Delegate, Device, Entity
from app.services.access_log import log_access
from app.utils.text import utcnow

ALLOWED_SCOPES = frozenset({"calendar:read", "research:read", "briefing:read"})
FORBIDDEN_SCOPES = frozenset(
    {
        "life.call",
        "phone:act",
        "home.lock",
        "home:act",
        "drone",
        "actuator.drone",
        "panic",
        "lock-all",
    }
)

SCOPE_TO_PERMISSION = {
    "calendar:read": {"calendar:read", "alerts:read"},
    "research:read": {"research:read"},
    "briefing:read": {"alerts:read", "assistant:profile"},
}


def _parse_when(value: str | None, *, now: datetime) -> datetime:
    if not value:
        return now + timedelta(days=2)
    try:
        parsed = date_parser.parse(str(value))
    except (ValueError, TypeError, OverflowError):
        return now + timedelta(days=2)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed


def parse_device_id(value) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _forbidden(scopes: list[str]) -> list[str]:
    bad: list[str] = []
    for scope in scopes:
        lowered = scope.strip().lower()
        if lowered in FORBIDDEN_SCOPES or lowered not in ALLOWED_SCOPES:
            bad.append(scope)
    return bad


async def grant(
    session: AsyncSession,
    *,
    name: str,
    scopes: list[str],
    not_after: str | None = None,
    device_id=None,
    actor: str = "master",
) -> dict:
    now = utcnow()
    bad = _forbidden(scopes)
    if bad:
        await log_access(
            session,
            actor=actor,
            action="delegate_grant",
            endpoint="tool:delegate_grant",
            resource_type="delegate",
            resource_ids=[],
            details={"ok": False, "rejected": bad},
        )
        return {
            "ok": False,
            "error": "forbidden_scope",
            "rejected": bad,
            "spoken": (
                "I can only share calendar, research, or briefing reads. "
                "Never calls, locks, drone, or panic."
            ),
        }
    if not scopes:
        return {
            "ok": False,
            "error": "missing_scopes",
            "spoken": "Name the scopes: calendar:read, research:read, or briefing:read.",
        }
    entity = (
        await session.execute(
            select(Entity).where(
                Entity.entity_type == "person",
                Entity.name.ilike(name),
            ).limit(1)
        )
    ).scalars().first()
    until = _parse_when(not_after, now=now)
    try:
        bound_id = parse_device_id(device_id)
    except (ValueError, TypeError):
        return {
            "ok": False,
            "error": "invalid_device",
            "spoken": "That device id is not valid.",
        }
    if bound_id is not None:
        device = await session.get(Device, bound_id)
        if device is None or device.revoked_at is not None:
            return {
                "ok": False,
                "error": "device_not_found",
                "spoken": "I don't have that device to bind the share to.",
            }
        device.trust_level = "device"
    row = Delegate(
        person_id=entity.id if entity else None,
        person_name=name,
        device_id=bound_id,
        scopes=list(scopes),
        not_after=until,
        granted_by=actor,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="delegate_grant",
        endpoint="tool:delegate_grant",
        resource_type="delegate",
        resource_ids=[str(row.id)],
        details={"person": name, "scopes": scopes, "not_after": until.isoformat()},
    )
    return {
        "ok": True,
        "id": str(row.id),
        "person_name": name,
        "scopes": list(scopes),
        "not_after": until.isoformat(),
        "trust_level": "device",
        "device_id": str(bound_id) if bound_id else None,
        "spoken": f"Shared {', '.join(scopes)} with {name} until {until.isoformat()}.",
    }


async def revoke(
    session: AsyncSession,
    *,
    name: str,
    actor: str = "master",
) -> dict:
    now = utcnow()
    rows = list(
        (
            await session.execute(
                select(Delegate).where(
                    Delegate.person_name.ilike(name),
                    Delegate.revoked_at.is_(None),
                )
            )
        ).scalars().all()
    )
    for row in rows:
        row.revoked_at = now
        await log_access(
            session,
            actor=actor,
            action="delegate_revoke",
            endpoint="tool:delegate_revoke",
            resource_type="delegate",
            resource_ids=[str(row.id)],
            details={"person": name},
        )
    await session.flush()
    if not rows:
        return {
            "ok": False,
            "error": "not_found",
            "spoken": f"No active share for {name}.",
        }
    return {
        "ok": True,
        "revoked": len(rows),
        "spoken": f"Revoked {name}.",
    }


async def active_for_device(session: AsyncSession, device_id) -> Delegate | None:
    now = utcnow()
    return (
        await session.execute(
            select(Delegate).where(
                Delegate.device_id == device_id,
                Delegate.revoked_at.is_(None),
                Delegate.not_after > now,
            ).limit(1)
        )
    ).scalars().first()


async def active_for_name(session: AsyncSession, name: str) -> Delegate | None:
    now = utcnow()
    return (
        await session.execute(
            select(Delegate).where(
                Delegate.person_name.ilike(name),
                Delegate.revoked_at.is_(None),
                Delegate.not_after > now,
            ).limit(1)
        )
    ).scalars().first()


def permission_allowed(permission: str, scopes: list[str]) -> bool:
    if permission in FORBIDDEN_SCOPES:
        return False
    allowed_perms: set[str] = set()
    for scope in scopes:
        allowed_perms |= SCOPE_TO_PERMISSION.get(scope, set())
        allowed_perms.add(scope)
    # Read-only memory stays owner-only unless briefing/research implied it.
    return permission in allowed_perms


async def scope_blocked(
    session: AsyncSession,
    *,
    actor: str,
    permission: str,
    name: str,
    device_id=None,
) -> dict | None:
    """If this device is bound to a delegate row, enforce the boxed scopes."""

    if actor == "master":
        return None
    bound_id = None
    try:
        bound_id = parse_device_id(device_id)
    except (ValueError, TypeError):
        bound_id = None
    if bound_id is None and actor.startswith("device:"):
        device_name = actor.split(":", 1)[1]
        device = (
            await session.execute(
                select(Device).where(Device.name == device_name, Device.revoked_at.is_(None)).limit(1)
            )
        ).scalars().first()
        if device is not None:
            bound_id = device.id
    if bound_id is None:
        return None
    row = await active_for_device(session, bound_id)
    if row is None:
        return None
    if not permission_allowed(permission, list(row.scopes or [])):
        await log_access(
            session,
            actor=actor,
            action="delegate_denied",
            endpoint="tool:dispatch",
            resource_type="delegate",
            resource_ids=[str(row.id)],
            details={"tool": name, "permission": permission, "device_id": str(bound_id)},
        )
        return {
            "ok": False,
            "error": "delegate_scope",
            "spoken": f"{row.person_name} is not allowed to use {name}.",
        }
    await log_access(
        session,
        actor=actor,
        action="delegate_use",
        endpoint="tool:dispatch",
        resource_type="delegate",
        resource_ids=[str(row.id)],
        details={"tool": name, "permission": permission, "device_id": str(bound_id)},
    )
    return None


def refuse_share_account() -> dict:
    return {
        "ok": False,
        "error": "refused",
        "spoken": "I will not share the owner account. I can grant a time-boxed calendar, research, or briefing read instead.",
    }
