"""Periodic routines scheduler worker (24/7 tick loop).

On every tick the scheduler advances due routines (with missed-run catch-up)
and evaluates trigger automations that arrive through the API.  Failures are
isolated per run and surfaced in run history; worker-level failures go to the
dead-letter queue instead of being silently dropped.
"""

from __future__ import annotations

import asyncio
import time

from app.config import settings
from app.services.runtime import record_dead_letter_sync


async def tick_once() -> dict:
    from app.db import SessionLocal
    from app.routines.service import tick

    async with SessionLocal() as session:
        outcome = await tick(session)
        await session.commit()
        return {
            "created": outcome.created,
            "skipped": outcome.skipped,
            "failed": outcome.failed,
            "failure_alerts": outcome.failure_alerts,
            "errors": outcome.errors,
        }


def main() -> None:
    interval = max(1, settings.scheduler_tick_seconds)
    while True:
        try:
            asyncio.run(tick_once())
        except Exception as exc:  # noqa: BLE001 - worker boundary: record and keep going
            record_dead_letter_sync(
                queue="scheduler",
                job_id="routine-tick",
                payload={},
                error=f"{type(exc).__name__}: {exc}",
            )
        time.sleep(interval)


if __name__ == "__main__":
    main()
