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

from app.ev.confirm import args_fingerprint, binding_fingerprint, pol_meta
from app.ev.messaging.routing import RouteBinding
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
        # Romanised Hindi / Indian-English confirmations on voice. Bare "ha"
        # is deliberately absent: it is also English laughter, and "ha ha"
        # must never approve a parked send.
        "haan",
        "han",
        "haa",
        "hann",
        "theek",
        "thik",
        "sahi",
        "bilkul",
        "jaroor",
        "zaroor",
        "yah",
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
    # Connectives and pronouns: "yes, go ahead and send it to him" is a
    # confirmation, and refusing it parks the send forever.
    "and",
    "then",
    "if",
    "you",
    "want",
    "would",
    "like",
    "to",
    "him",
    "her",
    "them",
    "that",
    "this",
    "one",
    # Romanised Hindi affirmations (the owner mixes languages on voice).
    "haan",
    "han",
    "haa",
    "ha",
    "hann",
    "theek",
    "thik",
    "sahi",
    "bilkul",
    "jaroor",
    "zaroor",
    "bhej",
    "bhejo",
    "bhejdo",
    "kar",
    "karo",
    "kardo",
    "de",
    "dedo",
    "do",
    "ji",
}
_NEGATE_FIRST = frozenset(
    {
        "no",
        "nope",
        "nah",
        "cancel",
        "stop",
        "don't",
        "dont",
        "never",
        "wait",
        "hold",
        # Negative replies that must also read as "do not send this".
        "not",
        "nevermind",
        "later",
        "leave",
        "forget",
    }
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
    "yet",
    "just",
    "actually",
    "and",
    "then",
    "if",
    "for",
    "get",
    "this",
    "one",
    "thanks",
    "thank",
    "you",
    "no",
    "later",
    "off",
    "nevermind",
}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z']+", (text or "").lower())


def is_affirmative_head(token: str) -> bool:
    """True when a single word can open a confirmation."""

    return str(token or "").strip().lower() in _AFFIRM_FIRST


_QUESTION_AUX = frozenset(
    {"do", "does", "did", "would", "could", "can", "should", "will", "are", "is", "was", "were"}
)
#: "do it" is an imperative; "do you …" is a question. Only real subjects count.
_QUESTION_SUBJECT = frozenset({"you", "i", "we", "they", "he", "she"})
#: A trailing question mark is never consent.
_QUESTION_TAIL = re.compile(r"\?\s*$")


def is_affirmative(text: str) -> bool:
    """A short, unambiguous yes — never a sentence that just starts with yes."""

    raw = str(text or "")
    if _QUESTION_TAIL.search(raw):
        return False
    tokens = _words(raw)
    if not tokens or len(tokens) > 8:
        return False
    if tokens[0] not in _AFFIRM_FIRST:
        return False
    # "do you want to send it" opens with an affirmative word and is a
    # QUESTION, not consent. Treating it as one sent a parked message the
    # owner never approved. "do it" / "do it now" stay affirmative.
    if (
        tokens[0] in _QUESTION_AUX
        and len(tokens) > 1
        and tokens[1] in _QUESTION_SUBJECT
    ):
        return False
    return all(token in _AFFIRM_WORDS for token in tokens)


def is_negative(text: str) -> bool:
    tokens = _words(text)
    if not tokens or len(tokens) > 8:
        return False
    if tokens[0] not in _NEGATE_FIRST:
        return False
    return all(token in _NEGATE_WORDS for token in tokens)


#: Consent that names the act. A parked message is a physical send: a bare
#: backchannel ("ok", "sure", "please") is not authorization for it.
_SEND_CONSENT = frozenset(
    {"yes", "yeah", "yep", "yup", "confirm", "confirmed", "send", "affirmative", "ya", "do", "go"}
)
#: A bare backchannel never authorizes a send; these words do.
_SEND_CONSENT_SINGLE = frozenset(
    {"yes", "yeah", "yep", "yup", "confirm", "confirmed", "send", "affirmative", "ya"}
)


def is_send_approval_affirmative(text: str) -> bool:
    """Affirmative consent for one parked send — explicit yes/send/confirm.

    "ok", "sure", "please", and "go" alone do not approve a message; "ok send
    it", "yes please", "do it", and "confirm" do.
    """

    if not is_affirmative(text):
        return False
    tokens = _words(text)
    if len(tokens) == 1:
        return tokens[0] in _SEND_CONSENT_SINGLE
    return any(token in _SEND_CONSENT for token in tokens)


def question_for(action: ApprovedAction) -> str:
    meta = pol_meta(action.payload)
    body = str(meta.get("text") or "").strip()
    display = str(
        meta.get("display") or meta.get("to") or meta.get("target") or "them"
    ).strip()
    return f'Should I send "{body}" to {display} on WhatsApp?'


def pending_payload(action: ApprovedAction) -> dict[str, Any]:
    meta = pol_meta(action.payload)
    return {
        "action_id": str(action.id),
        "to": str(meta.get("display") or meta.get("to") or meta.get("target") or ""),
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
    route: RouteBinding | None = None,
    address: str = "",
) -> ApprovedAction:
    """Park one send for human approval. Never sends anything.

    ``route`` is the transport the question is asked about and ``address`` the
    resolved destination identity. Both are stored so execution can be held to
    exactly what the owner approved instead of re-deciding either.
    """

    clock = utcnow()
    expires_at = clock + timedelta(seconds=APPROVAL_TTL_SECONDS)
    bound_route = route.as_payload() if route is not None else None
    for row in await _pending_rows(session):
        meta = pol_meta(row.payload)
        if meta.get("kind") != APPROVAL_KIND or _expired(row):
            continue
        if (
            str(meta.get("to") or meta.get("target") or "") == to
            and str(meta.get("text") or "") == text
            and str(meta.get("channel") or channel) == channel
        ):
            # Idempotent: a repeated identical ask refreshes the same ticket
            # instead of stacking questions.
            refreshed = dict(meta)
            refreshed["expires_at"] = expires_at.isoformat()
            refreshed["issued_at"] = clock.isoformat()
            refreshed["display"] = display
            if bound_route is not None:
                refreshed["route"] = bound_route
            if address:
                refreshed["address"] = address
            row.payload = {**row.payload, "_pol": refreshed}
            row.title = f"Send WhatsApp to {display}"[:256]
            row.updated_at = clock
            await session.flush()
            return row
    args = {"to": to, "text": text, "channel": channel, "confirm": True}
    payload = dict(args)
    # The stored target is the policy-level identity of what was approved
    # (transport + recipient), so a confirmation cannot be replayed onto a
    # different transport. `to`/`display` stay human-readable for speech.
    from app.ev.policy import canonical_target

    payload["_pol"] = {
        "kind": APPROVAL_KIND,
        "name": "send_message",
        "target": canonical_target("send_message", {"to": to, "channel": channel}) or to,
        "to": to,
        "display": display,
        "text": text,
        "channel": channel,
        "route": bound_route,
        "address": address or to,
        "binding_fingerprint": binding_fingerprint(bound_route, address or to),
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


async def newest_pending(session: AsyncSession) -> ApprovedAction | None:
    """Newest live send ticket, whatever surface parked it.

    For *mentioning* a prepared send on the surface the owner is now using —
    never for approving one. A "yes" that did not match this surface's ticket
    must not be spent on a ticket from another surface, because that is how an
    unrelated agreement turns into a message nobody approved.
    """

    for row in await _pending_rows(session):
        meta = pol_meta(row.payload)
        if meta.get("kind") != APPROVAL_KIND:
            continue
        if _expired(row):
            continue
        return row
    return None


async def recent_failed_send(
    session: AsyncSession,
    *,
    to: str,
    text: str,
    channel: str = "whatsapp",
    limit: int = 8,
) -> ApprovedAction | None:
    """Newest executed ticket for this exact message that sent nothing.

    Only transport/preparation failures reach here. Reusing the owner's yes is
    safe because the earlier attempt is provably undelivered
    (``result.sent != true``) and the TTL has not expired; the same approval
    binding is replayed so execution cannot drift to another recipient.
    """

    if not to or not text:
        return None
    result = await session.execute(
        select(ApprovedAction)
        .where(
            ApprovedAction.action_type == "send_message",
            ApprovedAction.status == "executed",
        )
        .order_by(ApprovedAction.executed_at.desc())
        .limit(limit)
    )
    wanted = to.strip().casefold()
    for row in result.scalars().all():
        meta = pol_meta(row.payload)
        if meta.get("kind") != APPROVAL_KIND:
            continue
        if _expired(row):
            continue
        if str(meta.get("text") or "") != text:
            continue
        if str(meta.get("channel") or "whatsapp") != channel:
            continue
        names = {
            str(meta.get("to") or "").strip().casefold(),
            str(meta.get("display") or "").strip().casefold(),
            str(meta.get("address") or "").strip().casefold(),
        }
        if wanted not in names:
            continue
        outcome = row.result if isinstance(row.result, dict) else {}
        if outcome.get("sent"):
            continue
        return row
    return None


def adopt_pending(
    action: ApprovedAction,
    *,
    device_id=None,
    live_session_id: str | None = None,
) -> None:
    """Re-bind a ticket to the surface now speaking, so its next "yes" lands."""

    meta = pol_meta(action.payload)
    meta["live_session_id"] = str(live_session_id) if live_session_id else None
    meta["device_id"] = str(device_id) if device_id else None
    meta["issued_at"] = utcnow().isoformat()
    action.payload = {**action.payload, "_pol": meta}
    action.updated_at = utcnow()


async def approve_pending(
    session: AsyncSession,
    action: ApprovedAction,
    *,
    actor: str = "voice",
) -> dict[str, Any]:
    """Resume the parked send through the normal dispatch (approval factor set)."""

    meta = pol_meta(action.payload)
    expected = str(meta.get("args_fingerprint") or "")
    if expected and expected != args_fingerprint(action.payload):
        await cancel_pending(session, action, actor=actor, reason="confirmation_target_mismatch")
        return {
            "ok": False,
            "sent": False,
            "spoken": "That confirmation no longer matches what I prepared, so I didn't send it.",
        }
    stored_binding = str(meta.get("binding_fingerprint") or "")
    if stored_binding and stored_binding != binding_fingerprint(
        meta.get("route"), str(meta.get("address") or "")
    ):
        await cancel_pending(session, action, actor=actor, reason="confirmation_target_mismatch")
        return {
            "ok": False,
            "sent": False,
            "spoken": (
                "The transport or recipient on that confirmation changed after I "
                "prepared it, so I didn't send it."
            ),
        }
    from app.ev.messaging.failures import spoken_failure
    from app.services.runtime import decide_action

    await decide_action(session, action.id, actor=actor, decision="approve")
    result = action.result if isinstance(action.result, dict) else {}
    sent = bool(result.get("sent"))
    spoken = str(
        result.get("spoken")
        or result.get("next_step")
        or ("Sent it." if sent else spoken_failure(result, channel=result.get("channel")))
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
        # The ticket may already be executed/denied by a race: never rewrite a
        # terminal row or claim a delivered message was cancelled.
        import contextlib

        with contextlib.suppress(Exception):
            await session.refresh(action)
        if action.status != "pending":
            result = action.result if isinstance(action.result, dict) else {}
            if action.status == "executed" and bool(result.get("sent")):
                return {
                    "ok": True,
                    "cancelled": False,
                    "sent": True,
                    "spoken": "It had already been sent, so I didn't cancel it.",
                }
            return {
                "ok": True,
                "cancelled": False,
                "sent": False,
                "spoken": "That one was already decided, so I left it as it was.",
            }
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

    affirmative = is_send_approval_affirmative(text)
    negative = is_negative(text)
    if not affirmative and not negative:
        return None
    action = await latest_pending(
        session,
        device_id=device_id,
        live_session_id=live_session_id,
    )
    if action is None:
        # A send may be prepared for another surface (the owner asked on the
        # Mac and is now answering on the phone). Never spend this "yes" on a
        # ticket it did not answer: adopt the ticket into this context and ask
        # once more, naming exactly what would go out.
        stray = await newest_pending(session) if affirmative else None
        if stray is None:
            return None
        adopt_pending(stray, device_id=device_id, live_session_id=live_session_id)
        await session.flush()
        return {
            "ok": False,
            "sent": False,
            "pending_approval": True,
            "spoken": question_for(stray),
            "action_id": str(stray.id),
        }
    if negative:
        return await cancel_pending(session, action, actor=actor)
    return await approve_pending(session, action, actor=actor)
