"""Calendar write (after re-consent) and ticket hold/search — never silent-buy."""

from __future__ import annotations

from urllib.parse import quote_plus

from dateutil.parser import isoparse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.actions import autonomy_mode
from app.ev.actuator import (
    DEFAULT_TIMEOUT_SECONDS,
    evidence_base,
    fingerprint,
    prior_result,
    record_actuator,
    with_timeout,
)
from app.ev.resolve import is_near_duplicate
from app.models import Integration, LiveEvent
from app.services.access_log import log_access
from app.utils.text import utcnow

GOOGLE_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
TICKET_SEARCH = "https://www.google.com/search?q="


def _calendar_input_error(*, title: str, start: str, end: str) -> str | None:
    if not title.strip():
        return "calendar title is required"
    try:
        start_at = isoparse(start)
        end_at = isoparse(end)
    except (TypeError, ValueError, OverflowError):
        return "calendar start and end must be ISO timestamps"
    if start_at.tzinfo is None or end_at.tzinfo is None:
        return "calendar start and end must include a timezone"
    if end_at <= start_at:
        return "calendar end must be after start"
    return None


async def _calendar_integration(session: AsyncSession) -> Integration | None:
    return (
        await session.execute(
            select(Integration)
            .where(Integration.adapter == "calendar", Integration.status == "active")
            .order_by(Integration.created_at.asc())
            .limit(1)
        )
    ).scalars().first()


def _has_write_scope(integration: Integration) -> bool:
    scopes = {str(s) for s in (integration.scopes or [])}
    config = integration.config or {}
    extra = {str(s) for s in (config.get("scopes") or [])}
    return bool(
        scopes & {GOOGLE_WRITE_SCOPE, "calendar:write", "calendar.events", "calendar:act"}
        or extra & {GOOGLE_WRITE_SCOPE, "calendar:write", "calendar.events", "calendar:act"}
        or config.get("write") is True
    )


async def _duplicate_event_id(
    session: AsyncSession,
    *,
    title: str,
    start: str,
    integration: Integration | None = None,
) -> str | None:
    wanted_title = title
    wanted_start = start
    rows = list(
        (
            await session.execute(
                select(LiveEvent)
                .where(LiveEvent.event_type == "calendar.event.updated")
                .order_by(LiveEvent.occurred_at.desc())
                .limit(80)
            )
        ).scalars().all()
    )
    for row in rows:
        payload = row.payload or {}
        summary = str(payload.get("summary") or payload.get("title") or "")
        start_at = payload.get("start") or payload.get("starts_at")
        event_id = payload.get("id") or payload.get("event_id")
        if event_id and is_near_duplicate(
            title=wanted_title, start=wanted_start, other_title=summary, other_start=start_at
        ):
            return str(event_id)
    events: list[dict] = []
    if integration is not None:
        stored = (integration.config or {}).get("events")
        if isinstance(stored, list):
            events.extend(item for item in stored if isinstance(item, dict))
    for existing in events:
        event_id = existing.get("id") or existing.get("event_id")
        if event_id and is_near_duplicate(
            title=wanted_title,
            start=wanted_start,
            other_title=str(existing.get("summary") or existing.get("title") or ""),
            other_start=existing.get("start") or existing.get("starts_at"),
        ):
            return str(event_id)
    return None


async def calendar_add(
    session: AsyncSession,
    *,
    title: str,
    start: str,
    end: str,
    location: str | None = None,
    confirm: bool = False,
    actor: str = "master",
    idempotency_key: str | None = None,
) -> dict:
    input_error = _calendar_input_error(title=title, start=start, end=end)
    if input_error:
        return {
            "ok": False,
            "error": "invalid_calendar_request",
            "spoken": input_error,
        }
    now = utcnow()
    key = idempotency_key or fingerprint("calendar_add", title, start, end, location or "")
    replayed = await prior_result(session, name="calendar_add", key=key)
    if replayed is not None:
        return replayed
    duplicate_id = await _duplicate_event_id(
        session, title=title, start=start, integration=None
    )
    if duplicate_id:
        evidence = evidence_base(
            source="calendar",
            accepted=True,
            observed=True,
            now=now,
            event_id=duplicate_id,
            duplicate=True,
        )
        result = {
            "ok": True,
            "event_id": duplicate_id,
            "duplicate": True,
            "spoken": f"{title} is already on the calendar.",
            "evidence": {"id": duplicate_id, **evidence},
        }
        await record_actuator(
            session, name="calendar_add", actor=actor, key=key, result=result, target=title
        )
        return result
    if autonomy_mode() != "full" and not confirm:
        return {
            "ok": False,
            "error": "confirm_required",
            "spoken": f"Confirm to add {title} to the calendar.",
        }
    integration = await _calendar_integration(session)
    if integration is None:
        return {
            "ok": False,
            "error": "not_connected",
            "degraded": True,
            "spoken": "Calendar adapter is not installed.",
        }
    if not _has_write_scope(integration):
        return {
            "ok": False,
            "error": "write_scope_required",
            "spoken": "I need a calendar write re-consent before I can create events.",
        }
    duplicate_id = await _duplicate_event_id(
        session, title=title, start=start, integration=integration
    )
    if duplicate_id:
        evidence = evidence_base(
            source="calendar",
            accepted=True,
            observed=True,
            now=now,
            event_id=duplicate_id,
            duplicate=True,
        )
        result = {
            "ok": True,
            "event_id": duplicate_id,
            "duplicate": True,
            "spoken": f"{title} is already on the calendar.",
            "evidence": {"id": duplicate_id, **evidence},
        }
        await record_actuator(
            session, name="calendar_add", actor=actor, key=key, result=result, target=title
        )
        return result
    from app.integrations import service as integration_service
    from app.integrations.adapters import registry

    adapter = registry.get("calendar")
    if adapter is None:
        return {"ok": False, "error": "needs_setup", "spoken": "Calendar adapter is unavailable."}
    try:
        payload = await with_timeout(
            integration_service.execute_action_after_policy(
                session,
                integration.id,
                "calendar.create_event",
                {
                    "summary": title,
                    "title": title,
                    "start": start,
                    "end": end,
                    "location": location,
                    "idempotency_key": key,
                },
                actor=actor,
            ),
            seconds=DEFAULT_TIMEOUT_SECONDS,
            spoken="Calendar write timed out. I will not claim the event was created.",
        )
    except Exception as exc:  # noqa: BLE001
        await log_access(
            session,
            actor=actor,
            action="calendar_add",
            endpoint="tool:calendar_add",
            resource_type="calendar",
            resource_ids=[],
            details={"ok": False, "error": str(exc)},
        )
        return {"ok": False, "error": type(exc).__name__, "spoken": str(exc)}
    if isinstance(payload, dict) and payload.get("error") in {"timeout", "cancelled"}:
        return payload
    action_result = payload.result if hasattr(payload, "result") else payload
    payload_dict: dict = action_result if isinstance(action_result, dict) else {}
    if str((integration.config or {}).get("provider") or "local").lower() == "local":
        # ``execute_action`` gives adapters an isolated config copy. Persist
        # the local double's event ledger here so a later request can detect a
        # near-duplicate even when its idempotency key differs.
        event_id = payload_dict.get("id") or payload_dict.get("event_id")
        if event_id and not payload_dict.get("duplicate"):
            stored = dict(integration.config or {})
            events = [item for item in (stored.get("events") or []) if isinstance(item, dict)]
            if not any(str(item.get("id") or "") == str(event_id) for item in events):
                events.append(
                    {
                        "id": str(event_id),
                        "summary": title,
                        "start": start,
                        "end": end,
                        "location": location,
                    }
                )
                stored["events"] = events
                integration.config = stored
                await session.flush()
    if payload_dict.get("duplicate") and (payload_dict.get("id") or payload_dict.get("event_id")):
        event_id = str(payload_dict.get("id") or payload_dict.get("event_id"))
        evidence = evidence_base(
            source=str(payload_dict.get("mode") or "calendar"),
            accepted=True,
            observed=True,
            now=now,
            event_id=event_id,
            duplicate=True,
        )
        result = {
            "ok": True,
            "event_id": event_id,
            "duplicate": True,
            "spoken": f"{title} is already on the calendar.",
            "evidence": {"id": event_id, **evidence},
        }
        await record_actuator(
            session, name="calendar_add", actor=actor, key=key, result=result, target=title
        )
        return result
    evidence_value = payload_dict.get("evidence")
    evidence_payload: dict = evidence_value if isinstance(evidence_value, dict) else payload_dict
    event_id = (
        evidence_payload.get("id")
        or evidence_payload.get("event_id")
        or payload_dict.get("id")
        or payload_dict.get("event_id")
    )
    await log_access(
        session,
        actor=actor,
        action="calendar_add",
        endpoint="tool:calendar_add",
        resource_type="calendar",
        resource_ids=[str(event_id)] if event_id else [],
        details={"ok": bool(event_id), "title": title},
    )
    if not event_id:
        return {
            "ok": False,
            "error": "missing_event_id",
            "spoken": "The calendar did not return an event id. I will not claim it was created.",
        }
    evidence = evidence_base(
        source=str(payload_dict.get("mode") or "calendar"),
        accepted=True,
        observed=True,
        now=now,
        event_id=str(event_id),
    )
    result = {
        "ok": True,
        "event_id": str(event_id),
        "spoken": f"Added {title} to the calendar.",
        "evidence": {"id": str(event_id), **evidence},
    }
    await record_actuator(
        session, name="calendar_add", actor=actor, key=key, result=result, target=title
    )
    return result


def ticket_search_url(query: str) -> str:
    return TICKET_SEARCH + quote_plus(query)


async def ticket_hold(
    session: AsyncSession,
    *,
    query: str,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    price: str | None = None,
    actor: str = "master",
) -> dict:
    url = ticket_search_url(query)
    hold_title = title or f"Ticket hold: {query}"
    calendar = None
    if start and end:
        # A ticket hold may add a calendar entry, but that nested write must
        # re-enter the canonical tool/policy path instead of calling the
        # calendar adapter directly from this convenience helper.
        from app.ev.tools import dispatch

        calendar_call = await dispatch(
            session,
            "calendar_add",
            {
                "title": hold_title,
                "start": start,
                "end": end,
                "location": url,
                "confirm": True,
            },
            actor=actor,
            allow_sensitive=True,
            channel="action",
            request_id=f"ticket-hold:{fingerprint('ticket_hold', query, start, end)}",
        )
        calendar = calendar_call.result if isinstance(calendar_call.result, dict) else {
            "ok": calendar_call.ok,
            "error": calendar_call.error,
        }
        if not calendar_call.ok or not calendar.get("ok", True):
            calendar = {"ok": False, "hold": "search_only", "error": calendar.get("error")}
    await log_access(
        session,
        actor=actor,
        action="ticket_hold",
        endpoint="tool:ticket_hold",
        resource_type="ticket",
        resource_ids=[],
        details={"query": query, "url": url, "price": price},
    )
    return {
        "ok": True,
        "url": url,
        "price": price,
        "draft": True,
        "purchased": False,
        "calendar": calendar,
        "spoken": f"I drafted a ticket search for {query}. I did not buy anything.",
    }


async def ticket_buy(
    session: AsyncSession,
    *,
    query: str,
    confirm: bool = False,
    payment_token: str | None = None,
    actor: str = "master",
) -> dict:
    await log_access(
        session,
        actor=actor,
        action="ticket_buy",
        endpoint="tool:ticket_buy",
        resource_type="ticket",
        resource_ids=[],
        details={
            "query": query,
            "confirm": bool(confirm),
            "has_payment_token": bool(payment_token),
            "purchased": False,
        },
    )
    if not confirm or not payment_token:
        hold = await ticket_hold(session, query=query, actor=actor)
        hold["ok"] = False
        hold["error"] = "confirm_and_payment_required"
        hold["purchased"] = False
        hold["spoken"] = (
            "I will not buy tickets without an explicit confirm and a payment token. "
            "Here is a search instead."
        )
        return hold
    # No vendor in this slice even with confirm+token.
    hold = await ticket_hold(session, query=query, actor=actor)
    hold["purchased"] = False
    hold["error"] = "vendor_unavailable"
    hold["spoken"] = (
        "Ticket purchase is not connected. I opened a search and will not charge anything."
    )
    hold["ok"] = False
    return hold
