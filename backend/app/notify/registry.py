"""Trusted fleet registry: Mac + Phone A + Phone B presence contract."""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, OwnerIdentity
from app.utils.text import utcnow

FLEET_DEVICES = (
    {
        "name": "Mac",
        "device_type": "mac",
        "platform": "apple",
        "capabilities": [
            "wake",
            "voice",
            "notifications",
            "attention",
            "messaging",
            "call",
        ],
    },
    {
        "name": "Phone A",
        "device_type": "phone",
        "platform": "apple",
        "capabilities": ["notifications", "attention", "messaging", "call", "push"],
    },
    {
        "name": "Phone B",
        "device_type": "phone",
        "platform": "apple",
        "capabilities": ["notifications", "attention", "messaging", "call", "push"],
    },
)


async def ensure_fleet_devices(
    session: AsyncSession,
    *,
    create_tokens: bool = False,
) -> dict:
    """Idempotently create the three trusted fleet devices (no duplicates)."""
    existing = {
        row.name: row
        for row in (
            await session.execute(select(Device))
        ).scalars().all()
    }
    owner = (
        await session.execute(
            select(OwnerIdentity).order_by(OwnerIdentity.created_at.asc()).limit(1)
        )
    ).scalar_one_or_none()
    created: list[str] = []
    tokens: dict[str, str] = {}
    for spec in FLEET_DEVICES:
        name = str(spec["name"])
        if name in existing:
            continue
        device = Device(
            name=name,
            device_type=spec["device_type"],
            platform=spec["platform"],
            capabilities=list(spec["capabilities"]),
            trust_level="owner",
            owner_id=owner.id if owner else None,
            paired_at=utcnow(),
        )
        if create_tokens:
            import secrets

            from app.utils.text import sha256_hex

            token = secrets.token_urlsafe(32)
            device.token_hash = sha256_hex(token)
            tokens[name] = token
        session.add(device)
        created.append(name)
    await session.flush()
    return {"created": created, "tokens": tokens}


def seed_sync(*, create_tokens: bool = False) -> dict:
    """CLI entrypoint: ``python -m app.notify.registry``."""
    from app.db import SessionLocal, init_db

    async def _run() -> dict:
        await init_db()
        async with SessionLocal() as session:
            result = await ensure_fleet_devices(session, create_tokens=create_tokens)
            await session.commit()
            return result

    return asyncio.run(_run())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Seed the EV fleet registry")
    parser.add_argument(
        "--tokens",
        action="store_true",
        help="Generate bearer tokens for newly created devices (printed once)",
    )
    args = parser.parse_args()
    result = seed_sync(create_tokens=args.tokens)
    print(result)
