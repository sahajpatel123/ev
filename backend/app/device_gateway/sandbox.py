"""Sandbox memory isolation. Production Memory OS is unreachable from here."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Device, Memory, SandboxFact
from app.utils.text import utcnow

from . import SANDBOX_NAMESPACE

_REMEMBER = re.compile(
    r"\bremember that (?:the )?(?:sandbox )?(?:satellite )?code is\s+(.+?)\.?\s*$",
    re.IGNORECASE,
)
_RECALL_CODE = re.compile(
    r"\bwhat is the sandbox (?:satellite )?code\b",
    re.IGNORECASE,
)
_PERSONAL = re.compile(
    r"\b(what do you remember about me|who is rahul|what is my (?:job|birthday|editor)|"
    r"what (?:project|preference) did we|"
    r"search your memory|what did we decide about evie)\b",
    re.IGNORECASE,
)


def memory_scope_of(device: Device | None) -> str:
    if device is None:
        return "owner"
    return (device.memory_scope or "owner").strip().lower() or "owner"


def is_sandbox_device(device: Device | None) -> bool:
    return memory_scope_of(device) == "sandbox"


def production_memory_enabled() -> bool:
    return bool(settings.cross_platform_production_memory)


async def remember_fact(
    session: AsyncSession,
    *,
    key: str,
    value: str,
    device_id: UUID | None,
    namespace: str | None = None,
) -> SandboxFact:
    ns = namespace or settings.sandbox_namespace or SANDBOX_NAMESPACE
    row = (
        await session.execute(
            select(SandboxFact).where(SandboxFact.namespace == ns, SandboxFact.fact_key == key)
        )
    ).scalar_one_or_none()
    now = utcnow()
    if row is None:
        row = SandboxFact(
            namespace=ns,
            fact_key=key[:160],
            value=value[:2000],
            source_device_id=device_id,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.value = value[:2000]
        row.source_device_id = device_id
        row.updated_at = now
    await session.flush()
    return row


async def recall_fact(session: AsyncSession, key: str, *, namespace: str | None = None) -> str | None:
    ns = namespace or settings.sandbox_namespace or SANDBOX_NAMESPACE
    row = (
        await session.execute(
            select(SandboxFact).where(SandboxFact.namespace == ns, SandboxFact.fact_key == key)
        )
    ).scalar_one_or_none()
    return None if row is None else row.value


async def production_memory_leak_probe(session: AsyncSession, query: str) -> dict[str, Any]:
    """Return whether production Memory rows would match. Never returns their text."""

    del query
    count = 0
    try:
        rows = (
            await session.execute(
                select(Memory.id).where(Memory.is_current.is_(True), Memory.redacted.is_(False)).limit(1)
            )
        ).all()
        count = len(rows)
    except Exception:  # noqa: BLE001
        count = 0
    return {
        "production_memory_reachable": False,
        "production_row_exists": count > 0,
        "injected": False,
        "scope": "sandbox",
    }


def extract_remember(text: str) -> tuple[str, str] | None:
    match = _REMEMBER.search((text or "").strip())
    if not match:
        return None
    return ("satellite_code", match.group(1).strip().rstrip("."))


def wants_code_recall(text: str) -> bool:
    return bool(_RECALL_CODE.search(text or ""))


def looks_like_personal_probe(text: str) -> bool:
    return bool(_PERSONAL.search(text or ""))


async def clear_cross_platform_sandbox(
    session: AsyncSession,
    *,
    namespace: str | None = None,
) -> int:
    """Erase synthetic PWA facts only. Never touches Memory OS rows."""

    from sqlalchemy import delete

    from .telemetry import emit

    ns = namespace or settings.sandbox_namespace or SANDBOX_NAMESPACE
    result = await session.execute(delete(SandboxFact).where(SandboxFact.namespace == ns))
    count = int(result.rowcount or 0)
    emit("sandbox.cleared", namespace=ns, count=count)
    return count
