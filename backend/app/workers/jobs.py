"""RQ job entrypoints for background memory processing."""

from __future__ import annotations

from app.services.processor import process_event_sync


def process_event(event_id: str) -> list[dict]:
    """Called by RQ workers; runs extraction + memory writing."""
    import asyncio
    from uuid import UUID

    try:
        result = asyncio.run(process_event_sync(UUID(event_id)))
    except Exception as exc:  # noqa: BLE001 - worker boundary: record and re-raise
        from app.services.runtime import record_dead_letter_sync

        record_dead_letter_sync(
            queue="ingestion",
            job_id=event_id,
            payload={
                "event_id": event_id,
                "entrypoint": "app.workers.jobs.process_event",
                "args": [event_id],
            },
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        from app.services.runtime import resolve_dead_letter_sync

        resolve_dead_letter_sync(queue="ingestion", job_id=event_id)
        return result


def run_live_retention(days: int | None = None) -> dict:
    """Scheduled/CLI entrypoint for the live-event retention window."""
    import asyncio

    from app.services.live_retention import apply_live_retention

    async def _run() -> dict:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            result = await apply_live_retention(session, days=days, actor="scheduler")
            await session.commit()
            return result

    return asyncio.run(_run())


def run_live_rebuild(reason: str = "scheduled rebuild") -> dict:
    """Scheduled/CLI entrypoint for the live-derived-state rebuild."""
    import asyncio

    from app.services.live_rebuild import rebuild_live_derived_state

    async def _run() -> dict:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            result = await rebuild_live_derived_state(
                session,
                actor="scheduler",
                reason=reason,
            )
            await session.commit()
            return result

    return asyncio.run(_run())


def run_compliance_retention(reason: str = "retention policy") -> dict:
    """Scheduled/CLI entrypoint for the biometric retention sweep.

    Enforces the configured voiceprint retention window (``EV_RETENTION_*`` /
    ``EV_REGION``) so revocation and residency rules are applied by the
    scheduler, not only on demand.
    """
    import asyncio

    from app.compliance.erasure import retention_sweep

    async def _run() -> dict:
        from app.db import SessionLocal

        async with SessionLocal() as session:
            result = await retention_sweep(session, reason=reason, actor="scheduler")
            await session.commit()
            return result

    return asyncio.run(_run())


def run_research_job(job_id: str) -> dict:
    """RQ entry point for one durable research job.

    The job row is the checkpoint/restart boundary; the worker owns no
    in-memory state that must survive a process restart.
    """
    import asyncio
    from uuid import UUID

    async def _run() -> dict:
        from app.db import SessionLocal
        from app.ev.research import ResearchService

        async with SessionLocal() as session:
            result = await ResearchService(session, actor="worker").run_job(UUID(job_id))
            await session.commit()
            return result

    try:
        result = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - worker boundary: record and re-raise
        from app.services.runtime import record_dead_letter_sync

        record_dead_letter_sync(
            queue="research",
            job_id=job_id,
            payload={
                "research_job_id": job_id,
                "entrypoint": "app.workers.jobs.run_research_job",
                "args": [job_id],
            },
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        from app.services.runtime import resolve_dead_letter_sync

        resolve_dead_letter_sync(queue="research", job_id=job_id)
        return result


def run_life_stream_tick() -> dict:
    """Headless iMessage/contacts/mail/calendar follower. No windows. Opt-in."""
    import asyncio

    from app.services.life_stream_daemon import (
        get_life_stream_daemon,
        life_stream_should_run,
    )

    if not life_stream_should_run():
        return {"ok": True, "skipped": True, "reason": "life_stream_disabled"}

    async def _run() -> dict:
        from app.config import settings
        from app.db import SessionLocal

        daemon = get_life_stream_daemon()
        contacts: list[dict] = []
        mail_items: list[dict] = []
        contacts_error = ""
        mail_error = ""
        calendar_error = ""
        gmail_error = ""
        helper = (getattr(settings, "life_helper_path", "") or "").strip()
        if helper:
            try:
                from app.integrations.life_helper import run_life_helper

                result = await run_life_helper("contacts.list", {}, helper_path=helper)
                raw = result.data.get("contacts") if result.data else None
                if isinstance(raw, list):
                    contacts = [row for row in raw if isinstance(row, dict)]
            except Exception as exc:  # noqa: BLE001 - follower must not crash the scheduler
                contacts = []
                contacts_error = f"{type(exc).__name__}: {exc}"
            try:
                from app.integrations.life_helper import run_life_helper

                mail_result = await run_life_helper(
                    "mail.list", {"limit": 20}, helper_path=helper
                )
                raw_mail = mail_result.data.get("messages") if mail_result.data else None
                if isinstance(raw_mail, list):
                    mail_items = [row for row in raw_mail if isinstance(row, dict)]
            except Exception as exc:  # noqa: BLE001 - follower must not crash the scheduler
                mail_items = []
                mail_error = f"{type(exc).__name__}: {exc}"
        async with SessionLocal() as session:
            calendar_items: list[dict] = []
            try:
                from app.services.life_account_pull import pull_live_channel_calendar

                calendar_items = await pull_live_channel_calendar(session)
            except Exception as exc:  # noqa: BLE001
                calendar_items = []
                calendar_error = f"{type(exc).__name__}: {exc}"
            if daemon.account_pull_due():
                try:
                    from app.services.life_account_pull import (
                        pull_google_calendar,
                        pull_google_mail,
                    )

                    google_cal = await pull_google_calendar(session)
                    if google_cal:
                        seen = {
                            str(item.get("event_id") or item.get("summary") or "")
                            for item in calendar_items
                        }
                        for item in google_cal:
                            key = str(item.get("event_id") or item.get("summary") or "")
                            if key and key in seen:
                                continue
                            if key:
                                seen.add(key)
                            calendar_items.append(item)
                except Exception as exc:  # noqa: BLE001
                    calendar_error = calendar_error or f"{type(exc).__name__}: {exc}"
                try:
                    from app.services.life_account_pull import pull_google_mail

                    gmail_items = await pull_google_mail(session)
                    if gmail_items:
                        mail_items = list(mail_items) + gmail_items
                except Exception as exc:  # noqa: BLE001
                    gmail_error = f"{type(exc).__name__}: {exc}"
                daemon.mark_account_pulled()
            outcome = await daemon.tick(
                session,
                sample_contacts=contacts or None,
                sample_mail=mail_items or None,
                sample_calendar=calendar_items or None,
            )
            await session.commit()
            if contacts_error:
                outcome = {**outcome, "contacts_error": contacts_error}
            if mail_error:
                outcome = {**outcome, "mail_error": mail_error}
            if calendar_error:
                outcome = {**outcome, "calendar_error": calendar_error}
            if gmail_error:
                outcome = {**outcome, "gmail_error": gmail_error}
            return outcome

    return asyncio.run(_run())
