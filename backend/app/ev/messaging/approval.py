"""Human-approval gate for WhatsApp Web autosend.

The owner keeps WhatsApp Web open; with approval Evie can send through it in
the background. Without approval she asks one question and waits — no send,
no compose, no focus theft. The ticket is an ``ApprovedAction`` row, so the
HUD/API approval path and the spoken "yes" path resume the exact same parked
arguments, and expiry/tamper checks stay in one place.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.confirm import args_fingerprint, pol_meta
from app.models import ApprovedAction
from app.utils.text import utcnow

APPROVAL_KIND = "send_approval"
APPROVAL_TTL_SECONDS = 300

_AFFIRM_FIRST = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yup",
        "sure",
        "ok",
        "okay",
        "confirm",
        "confirmed",
        "send",
        "go",
        "do",
        "please",
        "sounds",
        "alright",
        "affirmative",
        "correct",
    }
)
_AFFIRM_WORDS = _AFFIRM_FIRST | {
    "it",
    "now",
    "ahead",
    "for",
    "the",
    "message",
    "whatsapp",
    "good",
    "okey",
    "ya",
}
_NEGATE_FIRST = frozenset(
    {"no", "nope", "nah", "cancel", "stop", "don't", "dont", "never", "wait", "hold"}
)
_NEGATE_WORDS = _NEGATE_FIRST | {
    "it",
    "now",
    "not",
    "leave",
    "skip",
    "forget",
    "please",
    "on",
    "that",
    "the",
    "message",
    "send",
    "mind",
}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", (text or "").lower())


def is_affirmative(text: str) -> bool:
    """A short, unambiguous yes — never a sentence that just starts with yes."""

    tokens = _words(text)
    if not tokens or len(tokens) > 6:
        return False
    if tokens[0] not in _AFFIRM_FIRST:
        return False
    return all(token in _AFFIRM_WORDS for token in tokens)


def is_negative(text: str) -> bool:
    tokens = _words(text)
    if not tokens or len(tokens) > 6:
        return False
    if tokens[0] not in _NEGATE_FIRST:
        return False
    return all(token in _NEGATE_WORDS for token in tokens)


def question_for(action: ApprovedAction) -> str:
    meta = pol_meta(action.payload)
    body = str(meta.get("text") or "").strip()
    display = str(meta.get("display") or meta.get("target") or "them").strip()
    return f'Should I send "{body}" to {display} on WhatsApp?'


def pending_payload(action: ApprovedAction) -> dict[str, Any]:
    meta = pol_meta(action.payload)
    return {
        "action_id": str(action.id),
        "to": str(meta.get("target") or ""),
        "display": str(meta.get("display") or ""),
        "text": str(meta.get("text") or ""),
        "channel": str(meta.get("channel") or "whatsapp"),
        "expires_at": str(meta.get("expires_at") or ""),
        "spoken": question_for(action),
    }


async def park_send(
    session: AsyncSession,
    *,
    to: str,
    text: str,
    display: str,
    channel: str = "whatsapp",
    actor: str = "voice",
    device_id=None,
    live_session_id: str | None = None,
    source: str = "chat",
) -> ApprovedAction:
    """Park one send for human approval. Never sends anything."""

    clock = utcnow()
    expires_at = clock + timedelta(seconds=APPROVAL_TTL_SECONDS)
    for row in await _pending_rows(session):
        meta = pol_meta(row.payload)
        if meta.get("kind") != APPROVAL_KIND or _expired(row):
            continue
        if (
            str(meta.get("target") or "") == to
            and str(meta.get("text") or "") == text
            and str(meta.get("channel") or channel) == channel
        ):
            # Idempotent: a repeated identical ask refreshes the same ticket
            # instead of stacking questions.
            refreshed = dict(meta)
            refreshed["expires_at"] = expires_at.isoformat()
            refreshed["issued_at"] = clock.isoformat()
            refreshed["display"] = display
            row.payload = {**row.payload, "_pol": refreshed}
            row.title = f"Send WhatsApp to {display}"[:256]
            row.updated_at = clock
            await session.flush()
            return row
    args = {"to": to, "text": text, "channel": channel, "confirm": True}
    payload = dict(args)
    payload["_pol"] = {
        "kind": APPROVAL_KIND,
        "name": "send_message",
        "target": to,
        "display": display,
        "text": text,
        "channel": channel,
        "risk_class": "R2",
        "ttl_seconds": APPROVAL_TTL_SECONDS,
        "expires_at": expires_at.isoformat(),
        "issued_at": clock.isoformat(),
        "source": source,
        "live_session_id": str(live_session_id) if live_session_id else None,
        "device_id": str(device_id) if device_id else None,
        "independent": False,
        "resume_on_approve": True,
        "args_fingerprint": args_fingerprint(args),
    }
    # Supersede older pending sends so one spoken "yes" is never ambiguous.
    await _supersede_pending(session, actor=actor, device_id=device_id, live_session_id=live_session_id)
    action = ApprovedAction(
        action_type="send_message",
        title=f"Send WhatsApp to {display}"[:256],
        payload=payload,
        requires_approval=True,
        status="pending",
        requested_by=actor,
        device_id=_device_uuid(device_id),
    )
    session.add(action)
    await session.flush()
    return action


async def _supersede_pending(
    session: AsyncSession,
    *,
    actor: str,
    device_id=None,
    live_session_id: str | None,
) -> None:
    rows = await _pending_rows(session)
    for row in rows:
        meta = pol_meta(row.payload)
        if meta.get("kind") != APPROVAL_KIND:
            continue
        same_session = live_session_id and str(meta.get("live_session_id") or "") == str(live_session_id)
        same_device = device_id and str(meta.get("device_id") or "") == str(device_id)
        if same_session or same_device or (not meta.get("live_session_id") and not meta.get("device_id")):
            row.status = "denied"
            row.denied_at = utcnow()
            row.denied_reason = "superseded"
            row.updated_at = utcnow()


def _device_uuid(value):
    from uuid import UUID

    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


async def _pending_rows(session: AsyncSession, *, limit: int = 12) -> list[ApprovedAction]:
    result = await session.execute(
        select(ApprovedAction)
        .where(
            ApprovedAction.status == "pending",
            ApprovedAction.action_type == "send_message",
        )
        .order_by(ApprovedAction.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


def _expired(action: ApprovedAction) -> bool:
    from app.ev.confirm import confirmation_expired

    return confirmation_expired(action.payload)


async def latest_pending(
    session: AsyncSession,
    *,
    device_id=None,
    live_session_id: str | None = None,
) -> ApprovedAction | None:
    """Newest unexpired send approval for this context, or None."""

    rows = await _pending_rows(session)
    fallback: ApprovedAction | None = None
    for row in rows:
        meta = pol_meta(row.payload)
        if meta.get("kind") != APPROVAL_KIND:
            continue
        if _expired(row):
            row.status = "denied"
            row.denied_at = utcnow()
            row.denied_reason = "confirmation_expired"
            row.updated_at = utcnow()
            continue
        bound_session = str(meta.get("live_session_id") or "")
        bound_device = str(meta.get("device_id") or "")
        if live_session_id and bound_session == str(live_session_id):
            return row
        if device_id and bound_device == str(device_id):
            return row
        if not bound_session and not bound_device and fallback is None:
            fallback = row
    return fallback


async def approve_pending(
    session: AsyncSession,
    action: ApprovedAction,
    *,
    actor: str = "voice",
) -> dict[str, Any]:
    """Resume the parked send through the normal dispatch (approval factor set)."""

    expected = str(pol_meta(action.payload).get("args_fingerprint") or "")
    if expected and expected != args_fingerprint(action.payload):
        await cancel_pending(session, action, actor=actor, reason="confirmation_target_mismatch")
        return {
            "ok": False,
            "sent": False,
            "spoken": "That confirmation no longer matches what I prepared, so I didn't send it.",
        }
    from app.services.runtime import decide_action

    await decide_action(session, action.id, actor=actor, decision="approve")
    result = action.result if isinstance(action.result, dict) else {}
    sent = bool(result.get("sent"))
    spoken = str(
        result.get("spoken")
        or result.get("next_step")
        or ("Sent it." if sent else "I couldn't send that.")
    )
    return {
        "ok": sent,
        "sent": sent,
        "spoken": spoken,
        "action_id": str(action.id),
        "result": result,
    }


async def cancel_pending(
    session: AsyncSession,
    action: ApprovedAction,
    *,
    actor: str = "voice",
    reason: str = "owner_cancelled",
) -> dict[str, Any]:
    from app.services.runtime import decide_action

    try:
        await decide_action(session, action.id, actor=actor, decision="deny", reason=reason)
    except (KeyError, ValueError):
        action.status = "denied"
        action.denied_at = utcnow()
        action.denied_reason = reason
        action.updated_at = utcnow()
        await session.flush()
    return {
        "ok": True,
        "cancelled": True,
        "sent": False,
        "spoken": "Cancelled — I didn't send it.",
    }


async def handle_send_approval(
    session: AsyncSession,
    text: str,
    *,
    actor: str = "voice",
    device_id=None,
    live_session_id: str | None = None,
) -> dict[str, Any] | None:
    """Approve or cancel a parked send on an affirmative/negative turn."""

    affirmative = is_affirmative(text)
    negative = is_negative(text)
    if not affirmative and not negative:
        return None
    action = await latest_pending(
        session,
        device_id=device_id,
        live_session_id=live_session_id,
    )
    if action is None:
        return None
    if negative:
        return await cancel_pending(session, action, actor=actor)
    return await approve_pending(session, action, actor=actor)
