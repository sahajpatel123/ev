"""Origin vs target vs response device routing."""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device

_THIS_PHONE = re.compile(r"\b(this phone|the device i(?:'m| am) using)\b", re.IGNORECASE)
_MY_MAC = re.compile(r"\b(my mac|the mac|home station|on my macbook)\b", re.IGNORECASE)
_PRIMARY = re.compile(r"\b(my primary phone|primary iphone)\b", re.IGNORECASE)
_SECONDARY = re.compile(r"\b(my secondary phone|the se|secondary iphone)\b", re.IGNORECASE)
_MAC_CAMERA = re.compile(r"\b(mac camera|camera on my mac|use the mac camera)\b", re.IGNORECASE)
_OPEN_CALC = re.compile(r"\bopen (?:the )?calculator\b", re.IGNORECASE)
_CLOSE_CALC = re.compile(r"\bclose (?:the )?calculator\b|\bclose it\b", re.IGNORECASE)
_LOOK = re.compile(r"\blook at this\b", re.IGNORECASE)


async def device_by_role(session: AsyncSession, role: str) -> Device | None:
    return (
        await session.execute(
            select(Device)
            .where(Device.role == role, Device.revoked_at.is_(None))
            .order_by(Device.paired_at.desc())
        )
    ).scalars().first()


async def resolve_target(
    session: AsyncSession,
    text: str,
    *,
    origin: Device,
) -> Device:
    if _MY_MAC.search(text) or _MAC_CAMERA.search(text):
        mac = await device_by_role(session, "home_station")
        return mac or origin
    if _PRIMARY.search(text):
        return (await device_by_role(session, "primary_companion")) or origin
    if _SECONDARY.search(text):
        return (await device_by_role(session, "secondary_companion")) or origin
    if _THIS_PHONE.search(text):
        return origin
    return origin


def wants_mac_action(text: str) -> str | None:
    if _OPEN_CALC.search(text):
        return "open_calculator"
    if _CLOSE_CALC.search(text):
        return "close_calculator"
    return None


def wants_camera(text: str) -> bool:
    return bool(_LOOK.search(text) or _MAC_CAMERA.search(text))


def response_device_id(origin_id: UUID, *, speak_on_mac: bool = False) -> UUID:
    return origin_id if not speak_on_mac else origin_id
