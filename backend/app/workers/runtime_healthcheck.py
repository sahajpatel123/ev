"""Compose healthcheck and 72 h soak audit for the ``runtime`` service.

The runtime daemon is a long-lived loop, so a process-alive check would be
meaningless.  This healthcheck verifies the daemon's actual contract: it can
reach Postgres *and* has recorded a recent ``daemon`` RuntimeEvent tick.  A
service that is running but not emitting heartbeats is marked unhealthy.

``--soak`` turns the same evidence into the Agent 14 acceptance audit: over a
window (default 72 h) it counts daemon ticks, finds the largest inter-tick
gap, and exits non-zero when any gap exceeds the tolerance — the "0 missed
daemon ticks" proof for launchd supervision.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta


async def _recent_daemon_tick(max_age_seconds: float) -> bool:
    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models import RuntimeEvent
    from app.utils.text import utcnow

    async with SessionLocal() as session:
        count = int(
            (
                await session.execute(
                    select(func.count(RuntimeEvent.id)).where(
                        RuntimeEvent.kind == "daemon",
                        RuntimeEvent.occurred_at
                        >= utcnow() - timedelta(seconds=max_age_seconds),
                    )
                )
            ).scalar_one()
        )
        return count > 0


def _aware(value) -> datetime:
    if value is None:
        raise ValueError("missing timestamp")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _soak_report(
    window_hours: float,
    tolerance_gaps: int,
) -> dict:
    """Count daemon ticks in the window and measure the largest gap."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import RuntimeEvent
    from app.utils.text import utcnow

    now = utcnow()
    since = now - timedelta(hours=window_hours)
    async with SessionLocal() as session:
        ticks = list(
            (
                await session.execute(
                    select(RuntimeEvent.occurred_at)
                    .where(
                        RuntimeEvent.kind == "daemon",
                        RuntimeEvent.occurred_at >= since,
                    )
                    .order_by(RuntimeEvent.occurred_at.asc())
                )
            ).scalars().all()
        )
    if not ticks:
        return {
            "window_hours": window_hours,
            "ticks": 0,
            "max_gap_seconds": None,
            "healthy": False,
            "reason": "no daemon ticks in window",
        }
    max_gap = 0.0
    for previous, current in zip(ticks, ticks[1:], strict=False):
        gap = (_aware(current) - _aware(previous)).total_seconds()
        max_gap = max(max_gap, gap)
    last = max(_aware(tick) for tick in ticks)
    max_gap = max(max_gap, (now - last).total_seconds())

    # A tick lands every runtime_daemon_tick_seconds; allow the healthcheck to
    # observe after the fact plus one full tick of scheduling slack.
    from app.config import settings

    interval = max(1, settings.runtime_daemon_tick_seconds)
    tolerance = interval * (tolerance_gaps + 1) + 10
    healthy = max_gap <= tolerance
    return {
        "window_hours": window_hours,
        "ticks": len(ticks),
        "max_gap_seconds": round(max_gap, 1),
        "interval_seconds": interval,
        "tolerance_seconds": tolerance,
        "healthy": healthy,
        "reason": None if healthy else f"gap {max_gap:.1f}s exceeds tolerance {tolerance:.1f}s",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="EV runtime daemon audit")
    parser.add_argument(
        "--soak",
        action="store_true",
        help="Run the 72 h (configurable) missed-tick acceptance audit",
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=float(os.environ.get("EV_SOAK_WINDOW_HOURS", "72")),
    )
    parser.add_argument(
        "--tolerance-gaps",
        type=int,
        default=int(os.environ.get("EV_SOAK_TOLERANCE_GAPS", "1")),
    )
    args = parser.parse_args()

    if args.soak:
        try:
            report = asyncio.run(_soak_report(args.window_hours, args.tolerance_gaps))
        except Exception as exc:  # noqa: BLE001 - audit boundary: report and fail
            print(
                f"runtime soak audit failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
        print(
            "runtime soak audit: "
            + ", ".join(
                f"{key}={value}" for key, value in sorted(report.items()) if key != "reason"
            )
        )
        if report["reason"]:
            print(f"runtime soak audit: {report['reason']}", file=sys.stderr)
        return 0 if report["healthy"] else 1

    try:
        max_age = float(os.environ.get("EV_RUNTIME_HEALTHCHECK_MAX_AGE_SECONDS", "120"))
        healthy = asyncio.run(_recent_daemon_tick(max_age))
    except Exception as exc:  # noqa: BLE001 - healthcheck boundary: report and fail
        print(f"runtime healthcheck failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if not healthy:
        print(
            f"runtime daemon has no tick in the last {max_age:g}s "
            "(RuntimeEvent kind=daemon missing)",
            file=sys.stderr,
        )
        return 1
    print("runtime daemon healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
