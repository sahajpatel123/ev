"""Gear telemetry alerts: turn device snapshots into ranked, deduped alerts.

The plan requires health/gear anomalies to generate ranked, quiet-hours-aware
alerts (M5 acceptance). Battery, storage, CPU, and memory thresholds map to
tiers; non-urgent alerts are suppressed during quiet hours.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.ev_sense import quiet_hours_active
from app.gateway.providers import get_chat_provider
from app.ml.arbiter import create_default_arbiter
from app.ml.settings import get_ml_settings
from app.models import Alert, GearSnapshot
from app.ops.metrics import collect_metrics
from app.utils.text import fingerprint, utcnow

LOW_BATTERY_PCT = 20.0
CRITICAL_BATTERY_PCT = 10.0
LOW_STORAGE_BYTES = 5 * 1024**3
HIGH_CPU_PCT = 90.0
HIGH_MEMORY_PCT = 90.0


def _probe_mac() -> dict:
    """Real, stdlib-only probe of what this Mac can observe right now.

    Battery is read from ``pmset`` when available; storage and CPU load come
    from the OS. Every field that cannot be observed is ``None`` with a note,
    never guessed.
    """
    observed: dict[str, Any] = {
        "probed_at": utcnow().isoformat(),
        "system": platform.system(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "hostname": platform.node() or None,
    }
    try:
        usage = shutil.disk_usage("/")
        observed["storage_free_bytes"] = usage.free
        observed["storage_total_bytes"] = usage.total
    except OSError:
        observed["storage_free_bytes"] = None
        observed["storage_total_bytes"] = None
    try:
        observed["load_avg_1m"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        observed["load_avg_1m"] = None

    battery = None
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in result.stdout.splitlines():
            if "%" not in line:
                continue
            try:
                percent = float(line.split("%", 1)[0].rsplit(" ", 1)[-1])
                battery = {"percent": percent, "source": "pmset"}
                break
            except ValueError:
                continue
    except (OSError, subprocess.TimeoutExpired):
        battery = None
    observed["battery_percent"] = battery["percent"] if battery else None
    observed["battery_note"] = (
        "read live from pmset"
        if battery
        else "no battery readable via pmset (desktop/VM or permission denied)"
    )
    return observed


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


async def report(session: AsyncSession) -> dict:
    """Honest gear telemetry for this Mac and the rest of the fleet.

    Reports only what EV can actually observe today: device snapshots that a
    collector posted, the newest on-disk backup, configured provider health
    from audit rows, and model residency from Agent 2's arbiter. Everything
    else is listed in ``hardware_gaps`` instead of being silently assumed.
    """
    rows = (
        await session.execute(
            select(GearSnapshot).order_by(GearSnapshot.reported_at.desc())
        )
    ).scalars().all()
    latest: dict[str, GearSnapshot] = {}
    for row in rows:
        if row.device_id not in latest:
            latest[row.device_id] = row
    snapshots: list[dict[str, Any]] = []
    for device_id, snap in sorted(
        latest.items(), key=lambda item: item[1].reported_at, reverse=True
    ):
        snapshots.append(
            {
                "device_id": device_id,
                "reported_at": snap.reported_at.isoformat(),
                "battery_percent": snap.battery_percent,
                "storage_free_bytes": snap.storage_free_bytes,
                "memory_used_percent": snap.memory_used_percent,
                "cpu_percent": snap.cpu_percent,
                "uptime_seconds": snap.uptime_seconds,
                "details": snap.details,
            }
        )

    backup_dir = Path(settings.storage_root) / "backups"
    backup_files = sorted(backup_dir.glob("ev-backup-*.evbackup")) if backup_dir.exists() else []
    backup = None
    if backup_files:
        newest = backup_files[-1]
        try:
            envelope = json.loads(newest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            envelope = {}
        created = envelope.get("created_at")
        age_hours = None
        if isinstance(created, str):
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_hours = round((utcnow() - created_dt).total_seconds() / 3600.0, 2)
            except (ValueError, TypeError, OverflowError):
                age_hours = None
        backup = {
            "path": str(newest),
            "created_at": created,
            "age_hours": age_hours,
            "size_bytes": newest.stat().st_size,
            "checksum": envelope.get("plaintext_sha256"),
        }

    metrics = await collect_metrics(session)
    try:
        provider = get_chat_provider()
        provider_health = {
            "configured": True,
            "provider": provider.name,
            "live_health_check": "not_run_offline",
            "recent_errors_30d": metrics["calls"]["errors"],
            "recent_ok_calls_30d": metrics["calls"]["ok"],
        }
    except Exception as exc:  # pragma: no cover - defensive
        provider_health = {
            "configured": False,
            "error": str(exc),
        }

    try:
        arbiter = create_default_arbiter(get_ml_settings())
        model_residency = arbiter.stats()
    except Exception as exc:  # pragma: no cover - defensive
        model_residency = {"error": str(exc)}

    return {
        "generated_at": utcnow().isoformat(),
        "mac_snapshot": next(
            (s for s in snapshots if "mac" in s["device_id"].lower() or "m2" in s["device_id"].lower()),
            None,
        ),
        "mac_observed": _probe_mac(),
        "all_device_snapshots": snapshots,
        "backup": backup,
        "backup_note": (
            "newest encrypted backup found"
            if backup
            else "no ev-backup-*.evbackup file exists under storage_root/backups yet"
        ),
        "provider_health": provider_health,
        "model_residency": model_residency,
        "hardware_gaps": [
            "AR glasses / HUD hardware: no physical display exists; software emits schema-valid cards only.",
            "Wearable sensors: only snapshots a collector posts; no implicit body sensor access on this Mac.",
            "Real routing/transit API: travel time is an estimate, not a live route.",
            "OctoPrint/printer: no printer adapter is connected.",
            "Notification delivery: content is produced here; Agent 14 owns delivery.",
        ],
    }
