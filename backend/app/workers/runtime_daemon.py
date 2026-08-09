"""24/7 runtime daemon worker.

On every tick the daemon expires stale runtime sessions, re-enqueues dead
letters that are marked for retry, and records a structured runtime health
report. Worker-level failures go to the dead-letter queue so the runtime never
fails silently.
"""

from __future__ import annotations

import asyncio
import os
import time

from app.config import settings
from app.services.runtime import record_dead_letter_sync

_COMPLIANCE_LAST_RUN = 0.0


def _compliance_due() -> bool:
    """True when the scheduled biometric retention sweep should run now.

    Cadence is ``EV_COMPLIANCE_SWEEP_HOURS`` (default 24); a value <= 0
    disables the scheduled sweep (the on-demand API still works).
    """
    global _COMPLIANCE_LAST_RUN
    raw = os.getenv("EV_COMPLIANCE_SWEEP_HOURS", "24")
    try:
        hours = float(raw)
    except ValueError:
        hours = 24.0
    if hours <= 0:
        return False
    if time.monotonic() - _COMPLIANCE_LAST_RUN < hours * 3600:
        return False
    _COMPLIANCE_LAST_RUN = time.monotonic()
    return True


async def tick_once() -> dict:
    from app.db import SessionLocal
    from app.services.runtime import daemon_tick

    async with SessionLocal() as session:
        report = await daemon_tick(session)
        if _compliance_due():
            from app.compliance.erasure import retention_sweep

            report["compliance"] = await retention_sweep(
                session, reason="scheduled compliance sweep", actor="scheduler"
            )
        await session.commit()
        return report


def main() -> None:
    interval = max(1, settings.runtime_daemon_tick_seconds)
    while True:
        try:
            asyncio.run(tick_once())
        except Exception as exc:  # noqa: BLE001 - worker boundary: record and keep going
            record_dead_letter_sync(
                queue="runtime_daemon",
                job_id="runtime-tick",
                payload={},
                error=f"{type(exc).__name__}: {exc}",
            )
        time.sleep(interval)


if __name__ == "__main__":
    main()
