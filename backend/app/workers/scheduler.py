"""Periodic routines + live-data maintenance scheduler worker (24/7 tick loop).

On every tick the scheduler advances due routines (with missed-run catch-up),
evaluates trigger automations that arrive through the API, and runs the
live-data maintenance jobs on their own cadences: retention (physical
deletion, once a day by default) and derived-state rebuild (cheap, hourly by
default).  Failures are isolated per job and surfaced in run history or the
dead-letter queue instead of being silently dropped.
"""

from __future__ import annotations

import asyncio
import time

from app.config import settings
from app.db import init_db
from app.services.runtime import record_dead_letter_sync
from app.workers.jobs import run_life_stream_tick, run_live_rebuild, run_live_retention


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


class LiveMaintenance:
    """Runs live retention/rebuild on their configured cadences."""

    def __init__(
        self,
        *,
        retention_interval_seconds: int | None = None,
        rebuild_interval_seconds: int | None = None,
    ) -> None:
        self.retention_interval = (
            retention_interval_seconds or settings.live_retention_interval_seconds
        )
        self.rebuild_interval = rebuild_interval_seconds or settings.live_rebuild_interval_seconds
        self.life_interval = max(5, int(getattr(settings, "life_stream_interval_seconds", 20) or 20))
        # Negative infinity: the scheduler runs each job once at startup, then
        # every ``*_interval_seconds`` after that.
        self._last_retention_run: float = float("-inf")
        self._last_rebuild_run: float = float("-inf")
        self._last_life_run: float = float("-inf")

    def due(self, now: float | None = None) -> dict[str, bool]:
        """Which maintenance jobs are due at monotonic time ``now``."""
        now = time.monotonic() if now is None else now
        due = {
            "retention": now - self._last_retention_run >= self.retention_interval,
            "rebuild": now - self._last_rebuild_run >= self.rebuild_interval,
        }
        from app.services.life_stream_daemon import life_stream_should_run

        if life_stream_should_run():
            due["life_stream"] = now - self._last_life_run >= self.life_interval
        return due

    def run_due(self, now: float | None = None) -> dict:
        """Run each due job once; failures go to the dead-letter queue."""
        now = time.monotonic() if now is None else now
        results: dict = {}
        due = self.due(now)
        if due["retention"]:
            try:
                results["retention"] = run_live_retention()
            except Exception as exc:  # noqa: BLE001 - worker boundary: record and keep going
                results["retention_error"] = f"{type(exc).__name__}: {exc}"
                record_dead_letter_sync(
                    queue="scheduler",
                    job_id="live-retention",
                    payload={"cadence_seconds": self.retention_interval},
                    error=results["retention_error"],
                )
            self._last_retention_run = now
        if due["rebuild"]:
            try:
                results["rebuild"] = run_live_rebuild()
            except Exception as exc:  # noqa: BLE001 - worker boundary: record and keep going
                results["rebuild_error"] = f"{type(exc).__name__}: {exc}"
                record_dead_letter_sync(
                    queue="scheduler",
                    job_id="live-rebuild",
                    payload={"cadence_seconds": self.rebuild_interval},
                    error=results["rebuild_error"],
                )
            self._last_rebuild_run = now
        if due.get("life_stream"):
            try:
                results["life_stream"] = run_life_stream_tick()
            except Exception as exc:  # noqa: BLE001 - worker boundary: record and keep going
                results["life_stream_error"] = f"{type(exc).__name__}: {exc}"
                record_dead_letter_sync(
                    queue="scheduler",
                    job_id="life-stream",
                    payload={"cadence_seconds": self.life_interval},
                    error=results["life_stream_error"],
                )
            self._last_life_run = now
        return results


def main() -> None:
    # Ensure the schema exists before the first tick so the scheduler never
    # races the API's startup create_all (idempotent, Postgres-safe).
    asyncio.run(init_db())
    interval = max(1, settings.scheduler_tick_seconds)
    maintenance = LiveMaintenance()
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
        try:
            maintenance.run_due()
        except Exception as exc:  # noqa: BLE001 - worker boundary: keep the loop alive
            record_dead_letter_sync(
                queue="scheduler",
                job_id="live-maintenance",
                payload={},
                error=f"{type(exc).__name__}: {exc}",
            )
        time.sleep(interval)


if __name__ == "__main__":
    main()
