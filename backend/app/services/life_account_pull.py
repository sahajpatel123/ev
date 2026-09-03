"""Pull live Google account envelopes for the headless life follower.

Calendar and Gmail OAuth stay in the vault. This module never writes those
rows into live_context; the daemon stores Event envelopes for on-demand recall.
Failures are swallowed so a dead Google grant cannot stop iMessage ingest.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Integration, IntegrationCredential

logger = logging.getLogger("ev.life_account_pull")


async def _oauth_token(session: AsyncSession, integration_id) -> str:
    from app.integrations import vault

    row = (
        await session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == integration_id,
                IntegrationCredential.kind == "oauth",
                IntegrationCredential.revoked_at.is_(None),
            )
        )
    ).scalars().first()
    if row is None or not row.encrypted_access:
        return ""
    try:
        return vault.decrypt(row.encrypted_access)
    except Exception:  # noqa: BLE001 - follower must not crash on a bad vault row
        return ""


async def _google_integrations(session: AsyncSession, adapter: str) -> list[Integration]:
    rows = (
        await session.execute(
            select(Integration).where(
                Integration.adapter == adapter,
                Integration.status == "active",
            )
        )
    ).scalars().all()
    found: list[Integration] = []
    for row in rows:
        config = row.config if isinstance(row.config, dict) else {}
        if str(config.get("provider") or "").strip().lower() == "google":
            found.append(row)
    return found


async def pull_google_calendar(session: AsyncSession) -> list[dict[str, Any]]:
    """Upcoming Google Calendar events as daemon envelopes. Empty if not connected."""
    from app.integrations.adapters import registry

    adapter = registry.get("calendar")
    fetch = getattr(adapter, "_fetch_google_events", None)
    if fetch is None:
        return []
    items: list[dict[str, Any]] = []
    for integration in await _google_integrations(session, "calendar"):
        token = await _oauth_token(session, integration.id)
        if not token:
            continue
        config = dict(integration.config or {})
        try:
            events = await fetch(token, config)
        except Exception as exc:  # noqa: BLE001
            logger.info("google calendar pull skipped: %s", type(exc).__name__)
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            items.append(
                {
                    "event_id": str(event.get("event_id") or event.get("id") or ""),
                    "summary": str(event.get("summary") or event.get("title") or ""),
                    "start": str(event.get("start") or ""),
                    "end": str(event.get("end") or ""),
                    "location": str(event.get("location") or ""),
                }
            )
    return items


async def pull_google_mail(session: AsyncSession, *, limit: int = 15) -> list[dict[str, Any]]:
    """Gmail metadata envelopes (subject/sender only). Empty if not connected."""
    from app.integrations.adapters import registry

    adapter = registry.get("mail")
    fetch = getattr(adapter, "_list_gmail", None)
    if fetch is None:
        return []
    items: list[dict[str, Any]] = []
    for integration in await _google_integrations(session, "mail"):
        token = await _oauth_token(session, integration.id)
        if not token:
            continue
        config = dict(integration.config or {})
        try:
            messages = await fetch(token, config, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.info("gmail pull skipped: %s", type(exc).__name__)
            continue
        for message in messages:
            if isinstance(message, dict):
                items.append(message)
    return items


async def pull_live_channel_calendar(session: AsyncSession) -> list[dict[str, Any]]:
    """Copy already-synced calendar live events into daemon envelopes."""
    try:
        from app.ev.calendar import calendar_event_payloads
    except Exception:  # noqa: BLE001
        return []
    try:
        payloads, _ids = await calendar_event_payloads(session, limit=24)
    except Exception:  # noqa: BLE001
        return []
    items: list[dict[str, Any]] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        items.append(
            {
                "event_id": str(payload.get("event_id") or payload.get("id") or ""),
                "summary": str(payload.get("summary") or payload.get("title") or ""),
                "start": str(payload.get("start") or ""),
                "end": str(payload.get("end") or ""),
                "location": str(payload.get("location") or ""),
            }
        )
    return items
