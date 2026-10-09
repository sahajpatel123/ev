"""Human-approval gate for WhatsApp Web autosend.

The owner keeps WhatsApp Web open; with approval Evie can send through it in
the background. Without approval she asks one question and waits — no send,
no compose, no focus theft. The ticket is an ``ApprovedAction`` row, so the
HUD/API approval path and the spoken "yes" path resume the exact same parked
arguments, and expiry/tamper checks stay in one place.
"""

from __future__ import annotations

import contextlib
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.confirm import args_fingerprint, binding_fingerprint, pol_meta, tool_arguments
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
    actor: str | None = None,
    device_id=None,
    live_session_id: str | None = None,
) -> ApprovedAction | None:
    """Newest executed ticket for this exact message that sent nothing.

    A missing delivery receipt is not proof of an unsent message. Only a
    pre-click failure is eligible; attempted/uncertain sends never reuse the
    approval automatically. Recipient, text, actor and surface remain bound.
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
        if actor is not None and str(row.requested_by or "") != actor:
            continue
        bound_device = str(meta.get("device_id") or "")
        bound_session = str(meta.get("live_session_id") or "")
        if bound_device != str(device_id or "") or bound_session != str(live_session_id or ""):
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
        if outcome.get("sent") or outcome.get("send_attempted") or outcome.get("retry_safe") is False:
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
    # A row accepted into the WhatsApp thread without an ack yet is not a
    # "sent" claim, but it is not a failure either: never invite a resend.
    accepted = bool(result.get("accepted_by_client")) and not sent
    spoken = str(
        result.get("spoken")
        or result.get("next_step")
        or ("Sent it." if sent else spoken_failure(result, channel=result.get("channel")))
    )
    return {
        "ok": bool(sent or accepted),
        "sent": sent,
        "accepted_by_client": bool(result.get("accepted_by_client")),
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


# --------------------------------------------------------------------------- #
# Mac control tickets: per-action voice approval for phone-driven ui_action.
#
# Same ApprovedAction ledger as sends (same TTL/tamper/supersede rules, a
# separate kind) so the two families never answer each other's tickets. Unlike
# sends, approval does NOT resume through execute_action/dispatch: dispatch's
# computer arm cannot see the approval factor and would park again. Instead
# approve_control executes directly via handle_computer_tool with the ticket
# id, and the handler only honors tickets it can consume as approved+fresh.
# --------------------------------------------------------------------------- #

CONTROL_KIND = "computer_control_approval"


def question_for_control(action: ApprovedAction) -> str:
    meta = pol_meta(action.payload)
    effect = str(meta.get("display") or meta.get("effect") or "that Mac action").strip()
    return f"Should I do this on your Mac: {effect}?"


async def _pending_control_rows(session: AsyncSession, *, limit: int = 12) -> list[ApprovedAction]:
    result = await session.execute(
        select(ApprovedAction)
        .where(
            ApprovedAction.status == "pending",
            ApprovedAction.action_type == "ui_action",
        )
        .order_by(ApprovedAction.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def park_control(
    session: AsyncSession,
    *,
    tool: str,
    arguments: dict[str, Any] | None,
    display: str,
    actor: str = "voice",
    device_id=None,
    live_session_id: str | None = None,
) -> ApprovedAction:
    """Park one Mac control step for human approval. Never acts."""

    if tool != "ui_action":
        raise ValueError(f"only ui_action is parkable for phone control, not {tool!r}")
    args = dict(arguments or {})
    clock = utcnow()
    expires_at = clock + timedelta(seconds=APPROVAL_TTL_SECONDS)
    for row in await _pending_control_rows(session):
        meta = pol_meta(row.payload)
        if meta.get("kind") != CONTROL_KIND or _expired(row):
            continue
        if (
            str(meta.get("display") or "") == display
            and str(meta.get("live_session_id") or "") == str(live_session_id or "")
            and str(meta.get("device_id") or "") == str(device_id or "")
            and meta.get("args_fingerprint") == args_fingerprint({**args, "_pol": {}})
        ):
            refreshed = dict(meta)
            refreshed["expires_at"] = expires_at.isoformat()
            refreshed["issued_at"] = clock.isoformat()
            row.payload = {**row.payload, "_pol": refreshed}
            row.title = f"Mac control: {display}"[:256]
            row.updated_at = clock
            await session.flush()
            return row
    payload = dict(args)
    payload["_pol"] = {
        "kind": CONTROL_KIND,
        "name": "ui_action",
        "display": display,
        "effect": display,
        "risk_class": "R2",
        "ttl_seconds": APPROVAL_TTL_SECONDS,
        "expires_at": expires_at.isoformat(),
        "issued_at": clock.isoformat(),
        "live_session_id": str(live_session_id) if live_session_id else None,
        "device_id": str(device_id) if device_id else None,
        "resume_on_approve": False,
        "args_fingerprint": args_fingerprint({**args, "_pol": {}}),
    }
    # Supersede older pending control tickets so one spoken "yes" is never
    # ambiguous (mirrors send supersession; kinds stay independent).
    for row in await _pending_control_rows(session):
        meta = pol_meta(row.payload)
        if meta.get("kind") != CONTROL_KIND:
            continue
        same_session = live_session_id and str(meta.get("live_session_id") or "") == str(live_session_id)
        same_device = device_id and str(meta.get("device_id") or "") == str(device_id)
        if same_session or same_device or (not meta.get("live_session_id") and not meta.get("device_id")):
            row.status = "denied"
            row.denied_at = utcnow()
            row.denied_reason = "superseded"
            row.updated_at = utcnow()
    action = ApprovedAction(
        action_type="ui_action",
        title=f"Mac control: {display}"[:256],
        payload=payload,
        requires_approval=True,
        status="pending",
        requested_by=actor,
        device_id=_device_uuid(device_id),
    )
    session.add(action)
    await session.flush()
    return action


async def latest_pending_control(
    session: AsyncSession,
    *,
    device_id=None,
    live_session_id: str | None = None,
) -> ApprovedAction | None:
    """Newest unexpired control ticket for this context, or None."""

    rows = await _pending_control_rows(session)
    fallback: ApprovedAction | None = None
    for row in rows:
        meta = pol_meta(row.payload)
        if meta.get("kind") != CONTROL_KIND:
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


async def approve_control(
    session: AsyncSession,
    action: ApprovedAction,
    *,
    actor: str = "voice",
) -> dict[str, Any]:
    """Approve a control ticket and execute it against the attached Mac."""

    from app.services.runtime import decide_action

    meta = pol_meta(action.payload)
    expected = str(meta.get("args_fingerprint") or "")
    if expected and expected != args_fingerprint({**tool_arguments(action.payload), "_pol": {}}):
        await cancel_control(session, action, actor=actor, reason="confirmation_target_mismatch")
        return {
            "ok": False,
            "executed": False,
            "spoken": "That confirmation no longer matches what I prepared, so I didn't touch your Mac.",
        }
    from app.ev.computer import mac_observe_live

    if mac_observe_live() is None:
        await cancel_control(session, action, actor=actor, reason="mac_not_connected")
        return {
            "ok": False,
            "executed": False,
            "error_code": "MAC_NOT_CONNECTED",
            "spoken": "Your Mac disconnected, so I didn't do it. Open EV.app Talk and ask again.",
        }
    await decide_action(session, action.id, actor=actor, decision="approve")
    result = await execute_control_ticket(session, action, actor=actor)
    spoken = str(result.get("spoken") or "").strip() or (
        "Done." if result.get("executed") else "I couldn't do that on your Mac."
    )
    return {
        "ok": bool(result.get("executed")),
        "executed": bool(result.get("executed")),
        "spoken": spoken,
        "action_id": str(action.id),
        "result": result,
    }


async def execute_control_ticket(
    session: AsyncSession,
    action: ApprovedAction,
    *,
    actor: str = "voice",
) -> dict[str, Any]:
    """Run an approved control ticket through the Mac live session."""

    from app.ev.computer import handle_computer_tool, mac_observe_live

    mac_live = mac_observe_live()
    if mac_live is None:
        return {"ok": False, "executed": False, "error_code": "MAC_NOT_CONNECTED"}
    action.status = "executed"
    action.executed_at = utcnow()
    action.updated_at = utcnow()
    await session.flush()
    result = await handle_computer_tool(
        session,
        "ui_action",
        tool_arguments(action.payload),
        actor=actor,
        live_session_id=mac_live.session_id,
        device_id=None,
        approved_action_id=action.id,
    )
    action.result = result if isinstance(result, dict) else {"result": result}
    await session.flush()
    return action.result if isinstance(action.result, dict) else {}


async def cancel_control(
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
        with contextlib.suppress(Exception):
            await session.refresh(action)
        if action.status != "pending":
            return {
                "ok": True,
                "cancelled": False,
                "executed": action.status == "executed",
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
        "executed": False,
        "spoken": "Cancelled — I didn't touch your Mac.",
    }


async def consume_control_ticket(
    session: AsyncSession,
    ticket_id: Any,
    arguments: dict[str, Any] | None,
) -> ApprovedAction | None:
    """Trust-check a control ticket presented for execution.

    The computer handler honors ONLY tickets that are still approved+fresh:
    right kind, right verb, decided approve (execute marks executed before
    the call lands), unexpired, and fingerprint-identical to the arguments
    about to run. Anything else returns None and the Mac is not touched.
    """

    try:
        row = await session.get(ApprovedAction, ticket_id)
    except Exception:  # noqa: BLE001 - a bad id is a refusal, not a crash
        return None
    if row is None:
        return None
    meta = pol_meta(row.payload)
    if meta.get("kind") != CONTROL_KIND:
        return None
    if row.action_type != "ui_action":
        return None
    if row.status not in {"approved", "executed"}:
        return None
    if _expired(row):
        return None
    expected = str(meta.get("args_fingerprint") or "")
    if expected and expected != args_fingerprint({**(arguments or {}), "_pol": {}}):
        return None
    return row


async def handle_parked_approval(
    session: AsyncSession,
    text: str,
    *,
    actor: str = "voice",
    device_id=None,
    live_session_id: str | None = None,
) -> dict[str, Any] | None:
    """Answer the newest pending ticket across kinds (send or control).

    One spoken yes/no must resolve exactly one ticket: when both families
    have a pending ticket, the NEWER one wins (ties go to control, the more
    dangerous act to leave hanging) and the older stays pending for its own
    answer. Returns None when the text is not an answer or nothing pends.
    """

    affirmative = is_send_approval_affirmative(text)
    negative = is_negative(text)
    if not affirmative and not negative:
        return None
    send = await latest_pending(session, device_id=device_id, live_session_id=live_session_id)
    control = await latest_pending_control(session, device_id=device_id, live_session_id=live_session_id)
    if send is None and control is None:
        # No contextual ticket in either family: preserve the send family's
        # cross-surface stray adoption (asked on the Mac, answered on the
        # phone). Control tickets never adopt strays — a Mac click must be
        # answered from the context that parked it.
        adopted = await handle_send_approval(
            session, text, actor=actor, device_id=device_id, live_session_id=live_session_id
        )
        if adopted is not None:
            adopted.setdefault("approval_family", "send")
        return adopted
    picked_control = False
    if send is not None and control is not None:
        send_at = send.created_at or send.updated_at
        control_at = control.created_at or control.updated_at
        picked_control = control_at >= send_at
    elif control is not None:
        picked_control = True
    if picked_control:
        assert control is not None
        if negative:
            result = await cancel_control(session, control, actor=actor)
        else:
            result = await approve_control(session, control, actor=actor)
        result["approval_family"] = "control"
        return result
    assert send is not None
    if negative:
        result = await cancel_pending(session, send, actor=actor)
    else:
        result = await approve_pending(session, send, actor=actor)
    result["approval_family"] = "send"
    return result


# --------------------------------------------------------------------------- #
# Graph tier-D tickets: the same ApprovedAction ledger for any semantic tool.
#
# A supervisor-worker tier-D node parks here instead of executing. Approval
# resumes through decide_action -> execute_action -> the single tool dispatch,
# so every family (sends, calls, computer effects, file ops) shares one
# executor, one fingerprint check, and one receipt shape. Unlike parked sends,
# graph tickets never supersede each other: parallel tier-D nodes in one wave
# are independent, and the answer door targets each by action_id.
# --------------------------------------------------------------------------- #

GRAPH_ACTION_KIND = "graph_action_approval"


def graph_ticket_call(
    tool: str, arguments: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]] | None:
    """Translate a semantic worker tool into the dispatch call a ticket runs.

    Approved tickets execute through ``runtime.dispatch(action_type)``, which
    only knows dispatch names — a semantic name (``life.send``) would fail at
    execution. ``None`` means untranslatable: the caller blocks honestly.
    """

    from app.cognitive.capabilities import OP_ROUTED_DISPATCH

    args = dict(arguments or {})
    if tool == "life.send":
        to = str(args.get("to") or args.get("name") or "").strip()
        body = str(args.get("text") or args.get("body") or "")
        if not to or not body.strip():
            return None
        channel = str(args.get("channel") or "").strip().lower()
        if channel in {"mail", "email"}:
            out: dict[str, Any] = {"to": to, "body": body}
            if args.get("subject") is not None:
                out["subject"] = str(args["subject"])
            return "send_mail", out
        out = {"to": to, "text": body}
        if channel:
            out["channel"] = channel
        return "send_message", out
    if tool == "phone.call":
        op = str(args.get("op") or "").strip().lower()
        contact = str(args.get("contact") or "").strip()
        if op in {"call", "facetime"} and contact:
            return "place_call", {
                "destination": contact,
                "kind": "facetime" if op == "facetime" else "tel",
            }
        message = str(args.get("message") or "").strip()
        if op == "message" and contact and message:
            return "send_message", {"to": contact, "text": message}
        return None
    if tool == "computer.perform_effect":
        effect = str(args.get("effect") or "").strip()
        return ("computer", {"goal": effect}) if effect else None
    if tool == "digital.act":
        service = str(args.get("service") or "").strip().lower()
        operation = str(args.get("operation") or "").strip().lower()
        sub = dict(args.get("args") or {})
        if service == "whatsapp" and operation in {"send", "reply"}:
            to = str(sub.get("to") or sub.get("name") or sub.get("chat") or "").strip()
            body = str(sub.get("text") or sub.get("body") or "")
            if to and body.strip():
                return "send_message", {"to": to, "text": body, "channel": "whatsapp"}
        return None
    if tool == "calculate":
        expression = str(args.get("expression") or "").strip()
        return ("calculate", {"expression": expression}) if expression else None
    if tool == "brief.me":
        out = {"topic": str(args["topic"])} if args.get("topic") else {}
        return "brief_me", out
    allowed = OP_ROUTED_DISPATCH.get(tool)
    if allowed is not None:
        op = str(args.get("op") or "").strip()
        if op not in allowed:
            return None
        raw = args.get("args")
        op_args = dict(raw) if isinstance(raw, dict) else {
            key: value for key, value in args.items() if key not in {"op", "args"}
        }
        return op, op_args
    return None


async def _pending_graph_rows(session: AsyncSession, *, limit: int = 50) -> list[ApprovedAction]:
    result = await session.execute(
        select(ApprovedAction)
        .where(ApprovedAction.status == "pending")
        .order_by(ApprovedAction.created_at.desc())
        .limit(limit)
    )
    return [
        row for row in result.scalars().all()
        if pol_meta(row.payload).get("kind") == GRAPH_ACTION_KIND
    ]


async def park_graph_action(
    session: AsyncSession,
    *,
    node_id: str,
    label: str,
    tool: str,
    arguments: dict[str, Any],
    question: str,
    actor: str = "graph",
    device_id=None,
    live_session_id: str | None = None,
    route: Any | None = None,
    address: str = "",
) -> ApprovedAction:
    """Park one tier-D node for human approval. Never executes anything.

    Idempotent on (tool + argument fingerprint + context): a retried node
    refreshes its ticket instead of stacking questions. Distinct tickets
    coexist — each is resumed by action_id, never by "the pending one".
    """

    from app.ev.policy import canonical_target

    clock = utcnow()
    expires_at = clock + timedelta(seconds=APPROVAL_TTL_SECONDS)
    args = {key: value for key, value in dict(arguments or {}).items() if key != "_pol"}
    fingerprint = args_fingerprint(args)
    bound_route = route.as_payload() if route is not None and hasattr(route, "as_payload") else route
    for row in await _pending_graph_rows(session):
        meta = pol_meta(row.payload)
        if _expired(row):
            continue
        if (
            row.action_type == tool
            and str(meta.get("args_fingerprint") or "") == fingerprint
            and str(meta.get("live_session_id") or "") == str(live_session_id or "")
            and str(meta.get("device_id") or "") == str(device_id or "")
        ):
            refreshed = dict(meta)
            refreshed["expires_at"] = expires_at.isoformat()
            refreshed["issued_at"] = clock.isoformat()
            refreshed["question"] = question
            row.payload = {**row.payload, "_pol": refreshed}
            row.title = f"Approve {tool}: {label}"[:256]
            row.updated_at = clock
            await session.flush()
            return row
    payload = dict(args)
    payload["_pol"] = {
        "kind": GRAPH_ACTION_KIND,
        "name": tool,
        "target": canonical_target(tool, args) or tool,
        "question": question,
        "node_id": node_id,
        "label": label,
        "route": bound_route,
        "address": address,
        "binding_fingerprint": binding_fingerprint(bound_route, address),
        "risk_class": "R2",
        "ttl_seconds": APPROVAL_TTL_SECONDS,
        "expires_at": expires_at.isoformat(),
        "issued_at": clock.isoformat(),
        "source": "graph",
        "live_session_id": str(live_session_id) if live_session_id else None,
        "device_id": str(device_id) if device_id else None,
        "independent": False,
        "resume_on_approve": True,
        "args_fingerprint": fingerprint,
    }
    action = ApprovedAction(
        action_type=tool,
        title=f"Approve {tool}: {label}"[:256],
        payload=payload,
        requires_approval=True,
        status="pending",
        requested_by=actor,
        device_id=_device_uuid(device_id),
    )
    session.add(action)
    await session.flush()
    return action


def question_for_action(action: ApprovedAction) -> str:
    """Owner-facing question for any parked ticket (send or graph)."""

    meta = pol_meta(action.payload)
    if meta.get("kind") == GRAPH_ACTION_KIND:
        stored = str(meta.get("question") or "").strip()
        if stored:
            return stored
        return f"Should I proceed with {action.action_type}?"
    if meta.get("kind") == CONTROL_KIND:
        return question_for_control(action)
    return question_for(action)


def delivery_receipt(tool: str, result: dict[str, Any] | None) -> dict[str, Any]:
    """Canonical cross-channel receipt from a tool execution result.

    The whatsapp_web.send shape (verified_in_thread / message_id /
    delivery_confirmed) is the reference; every other family maps its own
    proof fields onto the same keys so supervisors and speakers judge one
    shape. Unknown tools keep their raw keys under evidence, never dropped.
    """

    body = dict(result or {})
    verified = body.get("verified_in_thread")
    if verified is None:
        verified = body.get("verified")
    if verified is None:
        verified = body.get("completed_verified")
    if verified is None:
        verified = body.get("opened")
    if verified is None:
        verified = body.get("accepted_by_client")
    evidence_keys = (
        "message_id", "delivery_confirmed", "send_attempted", "focus_theft",
        "accepted_by_client", "provider", "channel", "evidence", "counts",
        "payload_keys", "artifacts",
    )
    return {
        "tool": tool,
        "sent": bool(body.get("sent")),
        "verified": None if verified is None else bool(verified),
        "message_id": body.get("message_id"),
        "channel": body.get("channel"),
        "provider": body.get("provider"),
        "retry_safe": bool(body.get("retry_safe", not body.get("sent", False))),
        "evidence": {key: body[key] for key in evidence_keys if key in body},
    }


async def approve_graph_action(
    session: AsyncSession,
    action: ApprovedAction,
    *,
    actor: str = "graph",
) -> dict[str, Any]:
    """Approve a parked graph ticket and execute it through the runtime.

    Total: expiry, tamper, and races return honest failures, never raise.
    """

    from app.services.runtime import decide_action

    meta = pol_meta(action.payload)
    if meta.get("kind") != GRAPH_ACTION_KIND:
        return {"ok": False, "spoken": "That ticket is not a graph approval."}
    expected = str(meta.get("args_fingerprint") or "")
    if expected and expected != args_fingerprint(action.payload):
        with contextlib.suppress(Exception):
            await decide_action(session, action.id, actor=actor, decision="deny",
                                reason="confirmation_target_mismatch")
        return {
            "ok": False,
            "spoken": "That confirmation no longer matches what I prepared, so I didn't run it.",
        }
    stored_binding = str(meta.get("binding_fingerprint") or "")
    if stored_binding and stored_binding != binding_fingerprint(
        meta.get("route"), str(meta.get("address") or "")
    ):
        with contextlib.suppress(Exception):
            await decide_action(session, action.id, actor=actor, decision="deny",
                                reason="confirmation_target_mismatch")
        return {
            "ok": False,
            "spoken": "The transport or recipient on that confirmation changed, so I didn't run it.",
        }
    try:
        decided = await decide_action(session, action.id, actor=actor, decision="approve")
    except (KeyError, ValueError) as exc:
        name = type(exc).__name__
        if "expired" in str(exc).lower():
            return {"ok": False, "spoken": "That confirmation expired, so I didn't run it."}
        return {"ok": False, "spoken": f"That approval couldn't be used ({name})."}
    result = decided.result if isinstance(decided.result, dict) else {}
    receipt = delivery_receipt(action.action_type, result)
    result = {**result, "delivery_receipt": receipt}
    decided.result = result
    await session.flush()
    spoken = str(result.get("spoken") or "").strip()
    if not spoken:
        spoken = "Done." if receipt["sent"] or result.get("ok") else "I ran it, but I couldn't verify the effect."
    return {
        "ok": bool(receipt["sent"] or result.get("ok")),
        "sent": receipt["sent"],
        "spoken": spoken,
        "action_id": str(action.id),
        "delivery_receipt": receipt,
        "result": result,
    }
