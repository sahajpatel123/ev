"""Gear telemetry alerts: turn device snapshots into ranked, deduped alerts.

The plan requires health/gear anomalies to generate ranked, quiet-hours-aware
alerts (M5 acceptance). Battery, storage, CPU, and memory thresholds map to
tiers; non-urgent alerts are suppressed during quiet hours.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.ev_sense import quiet_hours_active
from app.models import Alert, GearSnapshot
from app.utils.text import fingerprint

LOW_BATTERY_PCT = 20.0
CRITICAL_BATTERY_PCT = 10.0
LOW_STORAGE_BYTES = 5 * 1024**3
HIGH_CPU_PCT = 90.0
HIGH_MEMORY_PCT = 90.0


async def scan_gear(session: AsyncSession) -> dict:
    """Scan the latest snapshot per device and create deduped gear alerts."""
    rows = (
        await session.execute(
            select(GearSnapshot).order_by(GearSnapshot.reported_at.desc())
        )
    ).scalars().all()
    latest: dict[str, GearSnapshot] = {}
    for row in rows:
        if row.device_id not in latest:
            latest[row.device_id] = row

    existing = set(
        (
            await session.execute(
                select(Alert.fingerprint).where(Alert.status == "pending")
            )
        ).scalars().all()
    )
    quiet = quiet_hours_active()
    created: list[Alert] = []
    duplicates = 0

    for device_id, snap in sorted(
        latest.items(), key=lambda item: item[1].reported_at, reverse=True
    ):
        checks: list[tuple[str, str, float, str]] = []
        if snap.battery_percent is not None and snap.battery_percent <= LOW_BATTERY_PCT:
            urgent = snap.battery_percent <= CRITICAL_BATTERY_PCT
            checks.append(
                (
                    "battery",
                    f"Battery at {snap.battery_percent:.0f}%",
                    0.8 if urgent else 0.55,
                    "urgent" if urgent else "useful",
                )
            )
        if snap.storage_free_bytes is not None and snap.storage_free_bytes < LOW_STORAGE_BYTES:
            free_gb = round(snap.storage_free_bytes / 1024**3, 1)
            checks.append(("storage", f"Only {free_gb} GB free", 0.6, "useful"))
        if snap.cpu_percent is not None and snap.cpu_percent >= HIGH_CPU_PCT:
            checks.append(("cpu", f"CPU at {snap.cpu_percent:.0f}%", 0.4, "background"))
        if snap.memory_used_percent is not None and snap.memory_used_percent >= HIGH_MEMORY_PCT:
            checks.append(
                ("memory", f"Memory at {snap.memory_used_percent:.0f}%", 0.4, "background")
            )

        for metric, body, priority, tier in checks:
            if quiet and tier != "urgent":
                continue
            fp = fingerprint({"kind": "gear", "device": device_id, "metric": metric})
            if fp in existing:
                duplicates += 1
                continue
            existing.add(fp)
            alert = Alert(
                kind="gear",
                title=f"Gear: {device_id} — {metric}",
                body=body,
                priority=priority,
                tier=tier,
                source=f"gear:{device_id}",
                trigger_ids=[str(snap.id)],
                rationale=(
                    f"Latest {metric} snapshot from {device_id} "
                    f"at {snap.reported_at.isoformat()}."
                ),
                fingerprint=fp,
                details={
                    "device_id": device_id,
                    "snapshot_id": str(snap.id),
                    "metric": metric,
                },
            )
            session.add(alert)
            created.append(alert)

    await session.flush()
    return {
        "scanned_devices": len(latest),
        "alerts_created": created,
        "duplicates_skipped": duplicates,
    }
