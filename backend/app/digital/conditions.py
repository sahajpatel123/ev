"""Presence OS communication conditions — additive evaluators, no Presence redesign."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PresenceCondition

DIGITAL_CONDITION_CLASSES = frozenset(
    {
        "EMAIL_RECEIVED_MATCH",
        "WHATSAPP_REPLY_MATCH",
        "DOCUMENT_RECEIVED",
        "PERSON_RESPONDED",
        "DEADLINE_APPROACHING",
        "CALENDAR_EVENT_CHANGED",
    }
)


async def evaluate_digital(
    session: AsyncSession, cond: PresenceCondition, context: dict[str, Any]
) -> bool:
    """Bounded to owner-declared watches in cond.payload. No inbox scrape."""
    cls = str(cond.cond_class or "")
    payload = dict(cond.payload or {})
    ctx = context or {}
    digital = ctx.get("digital") if isinstance(ctx.get("digital"), dict) else ctx
    if not isinstance(digital, dict):
        digital = {}
    # Allow top-level emails/messages from Presence resume context.
    if "emails" not in digital and ctx.get("emails"):
        digital = {**digital, "emails": ctx.get("emails")}
    if "messages" not in digital and ctx.get("messages"):
        digital = {**digital, "messages": ctx.get("messages")}
    if "gmail_events" not in digital and ctx.get("gmail_events"):
        digital = {**digital, "gmail_events": ctx.get("gmail_events")}

    if cls == "EMAIL_RECEIVED_MATCH":
        events = digital.get("gmail_events") or digital.get("messages") or digital.get("emails") or []
        return _match_messages(events, payload, channel="EMAIL")
    if cls == "WHATSAPP_REPLY_MATCH":
        events = digital.get("whatsapp_events") or digital.get("messages") or []
        return _match_messages(events, payload, channel="WHATSAPP")
    if cls == "DOCUMENT_RECEIVED":
        events = digital.get("messages") or digital.get("gmail_events") or digital.get("whatsapp_events") or []
        want = str(payload.get("filename") or payload.get("mime") or "pdf").lower()
        for ev in events:
            atts = ev.get("attachments") or []
            blob = f"{ev.get('subject') or ''} {ev.get('text') or ''}".lower()
            if want in blob:
                return True
            for a in atts:
                name = str(a.get("filename") or a.get("name") or "").lower()
                if want in name:
                    return True
        return bool(digital.get("document_received"))
    if cls == "PERSON_RESPONDED":
        person = str(payload.get("person") or "").lower()
        events = digital.get("messages") or []
        return any(person and person in str(e.get("from") or e.get("sender") or "").lower() for e in events)
    if cls == "DEADLINE_APPROACHING":
        return bool(digital.get("deadline_approaching") or payload.get("fired"))
    if cls == "CALENDAR_EVENT_CHANGED":
        return bool(digital.get("calendar_changed"))
    return False


def _match_messages(events: list[Any], payload: dict[str, Any], *, channel: str) -> bool:
    if not isinstance(events, list) or not events:
        return False
    person = str(payload.get("person") or payload.get("from") or "").lower()
    query = str(payload.get("query") or payload.get("about") or payload.get("q") or "").lower()
    thread = str(payload.get("thread_id") or payload.get("chat_ref") or "")
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if payload.get("channel") and str(ev.get("channel") or channel).upper() != str(payload.get("channel")).upper():
            continue
        blob = " ".join(
            str(ev.get(k) or "") for k in ("from", "sender", "subject", "text", "snippet", "name")
        ).lower()
        if person and person not in blob:
            continue
        if query and query not in blob:
            continue
        if thread and str(ev.get("thread_id") or ev.get("chat_ref") or "") != thread:
            continue
        return True
    return False
