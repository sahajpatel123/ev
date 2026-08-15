"""Calendar write (after re-consent) and ticket hold/search — never silent-buy."""

from __future__ import annotations

from urllib.parse import quote_plus

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.actions import autonomy_mode
from app.models import Integration
from app.services.access_log import log_access

GOOGLE_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
TICKET_SEARCH = "https://www.google.com/search?q="


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


async def calendar_add(
    session: AsyncSession,
    *,
    title: str,
    start: str,
    end: str,
    location: str | None = None,
    confirm: bool = False,
    actor: str = "master",
) -> dict:
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
            "error": "needs_setup",
            "spoken": "Calendar adapter is not installed.",
        }
    if not _has_write_scope(integration):
        return {
            "ok": False,
            "error": "write_scope_required",
            "spoken": "I need a calendar write re-consent before I can create events.",
        }
    from app.integrations.adapters import registry

    adapter = registry.get("calendar")
    if adapter is None:
        return {"ok": False, "error": "needs_setup", "spoken": "Calendar adapter is unavailable."}
    try:
        payload = await adapter.act(
            action="calendar.create_event",
            args={
                "summary": title,
                "title": title,
                "start": start,
                "end": end,
                "location": location,
            },
            token="",
            scopes=list(integration.scopes or []),
            config=dict(integration.config or {}),
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
    payload = payload or {}
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else payload
    event_id = evidence.get("id") or evidence.get("event_id") or payload.get("id") or payload.get("event_id")
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
    return {
        "ok": True,
        "event_id": str(event_id),
        "spoken": f"Added {title} to the calendar.",
        "evidence": {"id": str(event_id)},
    }


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
        calendar = await calendar_add(
            session,
            title=hold_title,
            start=start,
            end=end,
            location=url,
            confirm=True,
            actor=actor,
        )
        if not calendar.get("ok"):
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
