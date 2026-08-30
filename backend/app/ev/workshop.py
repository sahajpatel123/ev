"""Gear mode tutor and empty-consumable warnings (source=22)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.callouts import emit_callout
from app.models import BomItem, Callout, GearSnapshot
from app.utils.text import utcnow

DEFAULT_BATTERY_THRESHOLD = 15.0

DEFAULT_PRINTER_MODES = (
    {"name": "draft", "description": "Faster, lower-detail draft prints."},
    {"name": "normal", "description": "Standard quality."},
    {"name": "fine", "description": "Slower, high-detail prints."},
)


async def gear_modes(session: AsyncSession, device: str) -> list[dict]:
    snapshot = await _latest_snapshot(session, device)
    if snapshot is not None:
        stored = (snapshot.details or {}).get("modes")
        if isinstance(stored, list) and stored:
            return [
                {
                    "name": str(item.get("name")),
                    "description": str(item.get("description") or ""),
                    "current": bool(item.get("current")),
                }
                for item in stored
                if isinstance(item, dict) and item.get("name")
            ]
    if "print" in (device or "").lower():
        current = "normal"
        if snapshot is not None:
            current = str((snapshot.details or {}).get("mode") or "normal")
        return [
            {**mode, "current": mode["name"] == current}
            for mode in DEFAULT_PRINTER_MODES
        ]
    from app.ev.home import resolve_entity

    entity = await resolve_entity(session, device)
    if entity is not None:
        current = entity.state
        names = {
            "light": ("on", "off"),
            "lock": ("locked", "unlocked"),
            "cover": ("open", "closed"),
        }.get(entity.domain, ())
        return [
            {"name": name, "description": f"{entity.domain} {name}", "current": current == name}
            for name in names
        ]
    return []


async def gear_explain(session: AsyncSession, device: str) -> dict:
    modes = await gear_modes(session, device)
    if not modes:
        return {
            "ok": True,
            "modes": [],
            "spoken": "that device has no modes yet.",
        }
    lines = [
        f"{item['name']}" + (" (current)" if item.get("current") else "")
        for item in modes
    ]
    return {
        "ok": True,
        "modes": modes,
        "spoken": f"{device} modes: " + ", ".join(lines) + ".",
        "hud": {
            "schema_version": "ev.hud.card.v1",
            "title": f"{device} modes",
            "items": lines,
            "current": next((item["name"] for item in modes if item.get("current")), None),
        },
    }


async def gear_set_mode(session: AsyncSession, device: str, mode: str) -> dict:
    modes = await gear_modes(session, device)
    if not modes:
        return {
            "ok": False,
            "error": "no_modes",
            "spoken": "that device has no modes yet.",
            "valid": [],
        }
    wanted = (mode or "").strip().lower()
    valid = [item["name"] for item in modes]
    match = next((item for item in modes if item["name"].lower() == wanted), None)
    if match is None:
        return {
            "ok": False,
            "error": "unknown_mode",
            "spoken": f"Unknown mode. Valid: {', '.join(valid)}.",
            "valid": valid,
        }
    snapshot = await _latest_snapshot(session, device)
    if snapshot is None:
        snapshot = GearSnapshot(
            device_id=device,
            reported_at=utcnow(),
            details={},
        )
        session.add(snapshot)
    details = dict(snapshot.details or {})
    details["mode"] = match["name"]
    details["modes"] = [
        {**item, "current": item["name"] == match["name"]}
        for item in modes
    ]
    snapshot.details = details
    snapshot.reported_at = utcnow()
    await session.flush()
    return {
        "ok": True,
        "mode": match["name"],
        "modes": details["modes"],
        "spoken": f"{device} is now {match['name']}.",
    }


async def _latest_snapshot(session: AsyncSession, device: str) -> GearSnapshot | None:
    stmt = select(GearSnapshot).order_by(GearSnapshot.reported_at.desc()).limit(1)
    if device:
        stmt = (
            select(GearSnapshot)
            .where(GearSnapshot.device_id == device)
            .order_by(GearSnapshot.reported_at.desc())
            .limit(1)
        )
    return (await session.execute(stmt)).scalars().first()


def _empty_fingerprint(kind: str, key: str) -> str:
    return f"{kind}:{key}"


async def collect_empties(session: AsyncSession) -> list[dict]:
    items: list[dict] = []
    bom_rows = list((await session.execute(select(BomItem))).scalars().all())
    for row in bom_rows:
        if row.reorder_at is None:
            continue
        if float(row.qty) <= float(row.reorder_at):
            items.append(
                {
                    "kind": "bom",
                    "name": row.name,
                    "qty": row.qty,
                    "reorder_at": row.reorder_at,
                    "fingerprint": _empty_fingerprint("bom", str(row.id)),
                }
            )
    snapshots = list(
        (
            await session.execute(
                select(GearSnapshot).order_by(GearSnapshot.reported_at.desc())
            )
        ).scalars().all()
    )
    seen_devices: set[str] = set()
    for snap in snapshots:
        if snap.device_id in seen_devices:
            continue
        seen_devices.add(snap.device_id)
        threshold = float((snap.details or {}).get("battery_threshold") or DEFAULT_BATTERY_THRESHOLD)
        if snap.battery_percent is None:
            continue
        if float(snap.battery_percent) < threshold:
            items.append(
                {
                    "kind": "battery",
                    "name": snap.device_id,
                    "qty": snap.battery_percent,
                    "reorder_at": threshold,
                    "fingerprint": _empty_fingerprint("battery", snap.device_id),
                }
            )
    return items


async def _already_called(session: AsyncSession, fingerprint: str) -> bool:
    row = (
        await session.execute(
            select(Callout.id).where(
                Callout.source == "22",
                Callout.source_item == fingerprint,
            ).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def scan_empties(session: AsyncSession, *, emit: bool = True) -> dict:
    items = await collect_empties(session)
    emitted = 0
    if emit:
        for item in items:
            if await _already_called(session, item["fingerprint"]):
                continue
            text = f"{item['name']} is low ({item['qty']} / {item['reorder_at']})."
            await emit_callout(
                session,
                text,
                source="22",
                source_item=item["fingerprint"],
                hud={"schema_version": "ev.hud.card.v1", "title": "Empty", "body": text},
            )
            emitted += 1
    return {"count": len(items), "empties": items, "emitted": emitted}


async def list_empties(session: AsyncSession) -> dict:
    result = await scan_empties(session, emit=False)
    names = [item["name"] for item in result["empties"]]
    spoken = (
        "You're out of: " + ", ".join(names) + "."
        if names
        else "Nothing is below a threshold."
    )
    return {
        "ok": True,
        "count": result["count"],
        "empties": result["empties"],
        "spoken": spoken,
    }
