"""Shared per-lane error messages and delivery-evidence gate (Executor Plan §2.1–2.2).

Every messaging/desktop lane names its true failure (typed code + one honest
spoken line + one next step) instead of flattening everything into a generic
"connection" apology. Every send verb proves delivery with provider evidence
or fails with ``missing_delivery_evidence`` — compose/draft/opened-URL states
can never satisfy the gate.
"""

from __future__ import annotations

from typing import Any

LANE_DISPLAY = {
    "whatsapp": "WhatsApp",
    "imessage": "Messages",
    "mail": "Mail",
    "gmail": "Gmail",
    "telegram": "Telegram",
    "signal": "Signal",
    "slack": "Slack",
    "discord": "Discord",
    "teams": "Teams",
    "zoom": "Zoom",
    "messenger": "Messenger",
    "instagram": "Instagram",
}

# Plan §2.1 typed codes. Each maps to (spoken template, next step); templates
# take {lane} (display name) and {detail} (chat/contact/selector context).
LANE_ERROR_SPOKEN: dict[str, tuple[str, str]] = {
    "helper_missing": (
        "The Mac helper isn't installed, so Evie can't reach {lane} on this Mac.",
        "Install EVLifeHelper and set EV_LIFE_HELPER_PATH.",
    ),
    "permission_denied": (
        "Evie doesn't have the Mac permission needed for {lane}.",
        "Open System Settings → Privacy & Security and grant the requested permission.",
    ),
    "not_connected": (
        "Evie can't reach {lane} on the Mac right now.",
        "Open the app on the Mac and make sure you're signed in.",
    ),
    "session_logged_out": (
        "{lane} is showing its login screen on the Mac.",
        "Sign in again on the Mac, then ask once more.",
    ),
    "qr_expired": (
        "That {lane} login code expired before it was scanned.",
        "Ask for a fresh code and scan it promptly.",
    ),
    "companion_evicted": (
        "The Mac lost its linked-device slot for {lane}.",
        "Re-link the Mac from the phone app's Linked Devices screen.",
    ),
    "chat_not_found": (
        "I couldn't find a {lane} conversation named {detail}.",
        "Check the name and try again.",
    ),
    "ambiguous_recipient": (
        "More than one {lane} conversation matches {detail} — which one?",
        "Name the exact conversation.",
    ),
    "ax_unwalkable": (
        "The {lane} app on the Mac changed its layout, so Evie can't drive it right now.",
        "Nothing to do on your side; the automation needs re-mapping.",
    ),
    "checkpoint": (
        "{lane} is asking for an extra verification step on the Mac.",
        "Complete the check in the app yourself — Evie never solves logins.",
    ),
    "selector_drift": (
        "{lane} changed its screen layout, so Evie's saved buttons no longer match.",
        "Nothing to do on your side; the lane needs re-pinning.",
    ),
    "scope_missing": (
        "Evie isn't authorized for that {lane} workspace or chat.",
        "Approve the integration scope or pick an authorized chat.",
    ),
    "desktop_cache_unavailable": (
        "{lane} Desktop's message cache isn't readable on the Mac.",
        "Open the Desktop app once so it syncs, then ask again.",
    ),
    "imessage_account_missing": (
        "Messages on the Mac isn't signed in, so Evie couldn't send that.",
        "Sign in to Messages on the Mac and try again.",
    ),
    "sms_relay_off": (
        "SMS sending needs this Mac paired for text relay, and it isn't.",
        "On the iPhone: Settings → Messages → Text Message Forwarding → enable this Mac.",
    ),
    "mail_no_match": (
        "I couldn't find that email in Mail.",
        "Name the sender or the subject.",
    ),
    "mail_ambiguous_match": (
        "More than one email matches — which one?",
        "Name both the sender and the subject.",
    ),
}

# Typed markers raised by EVLifeHelper AppleScript guards (substring match —
# the helper embeds them in its failure detail).
_HELPER_MARKERS: dict[str, str] = {
    "EVIE_NO_IMESSAGE_SERVICE": "imessage_account_missing",
    "EVIE_NO_MESSAGE_SERVICE": "imessage_account_missing",
    "EVIE_NO_SMS_SERVICE": "sms_relay_off",
    "EVIE_MAIL_NO_MATCH": "mail_no_match",
    "EVIE_MAIL_AMBIGUOUS_MATCH": "mail_ambiguous_match",
}

# Backing-specific signals that normalize onto the typed codes above.
_SIGNAL_ALIASES: dict[str, str] = {
    "cdp_qr": "session_logged_out",
    "cdp_not_linked": "session_logged_out",
    "whatsapp_web_not_linked": "session_logged_out",
    "not_linked": "session_logged_out",
    "logged_out": "session_logged_out",
    "session_logged_out": "session_logged_out",
    "qr": "session_logged_out",
    "cdp_offline": "not_connected",
    "cdp_unavailable": "not_connected",
    "cdp_not_running": "not_connected",
    "chrome_not_running": "not_connected",
    "browser_offline": "not_connected",
    "cdp_chrome_not_running": "not_connected",
    "cdp_connect_failed": "not_connected",
    "dead_cdp_transport": "not_connected",
    "desktop_cache_unavailable": "desktop_cache_unavailable",
    "chat_not_found": "chat_not_found",
    "ambiguous_recipient": "ambiguous_recipient",
    "ax_unwalkable": "ax_unwalkable",
    "ui_changed": "ax_unwalkable",
    "checkpoint": "checkpoint",
    "meta_checkpoint": "checkpoint",
    "selector_drift": "selector_drift",
    "scope_missing": "scope_missing",
    "admin_block": "scope_missing",
    "helper_missing": "helper_missing",
    "permission_denied": "permission_denied",
    "companion_evicted": "companion_evicted",
    "cdp_not_linked_after_linked": "companion_evicted",
    "qr_expired": "qr_expired",
}

# Exact WhatsApp wordings (Phase 3 contract — existing tests pin these).
_WHATSAPP_LOGGED_OUT_MESSAGE = (
    "WhatsApp Web is not linked (showing the login screen). On the iPhone: "
    "WhatsApp → Settings → Linked Devices → Link a Device, then scan. "
    "Or open WhatsApp Desktop on the Mac so Evie can read its local cache."
)
_WHATSAPP_CACHE_MESSAGE = (
    "WhatsApp Desktop's message cache isn't readable on the Mac. Open "
    "WhatsApp Desktop once so it syncs, then ask again."
)
_WHATSAPP_TRANSPORT_MESSAGE = (
    "The Mac's background browser for WhatsApp isn't running, so Evie "
    "can't reach WhatsApp Web right now."
)


def normalize_lane_code(result: dict[str, Any] | None) -> str | None:
    """Map a backing result's diagnosis/error/reason onto a typed code."""

    result = result if isinstance(result, dict) else {}
    if result.get("qr"):
        return "session_logged_out"
    haystack = " ".join(
        str(result.get(key) or "") for key in ("diagnosis", "error", "reason")
    )
    for marker, code in _HELPER_MARKERS.items():
        if marker in haystack:
            return code
    for key in ("diagnosis", "error", "reason"):
        signal = str(result.get(key) or "").strip().lower()
        if signal in _SIGNAL_ALIASES:
            return _SIGNAL_ALIASES[signal]
        if signal in LANE_ERROR_SPOKEN:
            return signal
    return None


def lane_error_message(
    lane: str,
    result: dict[str, Any] | None,
    *,
    verb: str = "read",
    chat: str = "",
) -> str:
    """One honest spoken line + next step for a lane failure.

    ``verb`` is ``read`` or ``send``; send failures name the unsent act so
    the owner never wonders whether anything went out.
    """

    lane_key = str(lane or "").strip().lower()
    display = LANE_DISPLAY.get(lane_key, lane_key or "that app")
    code = normalize_lane_code(result)
    detail = str(chat or "").strip()

    if lane_key == "whatsapp":
        if code == "session_logged_out":
            message = _WHATSAPP_LOGGED_OUT_MESSAGE
        elif code == "desktop_cache_unavailable":
            message = _WHATSAPP_CACHE_MESSAGE
        elif code == "not_connected":
            message = _WHATSAPP_TRANSPORT_MESSAGE
        elif code == "chat_not_found":
            message = f"I couldn't find a WhatsApp chat named {detail or 'that chat'}."
        elif code == "ambiguous_recipient":
            message = f"More than one WhatsApp chat matches {detail or 'that name'} — which one?"
        elif code in LANE_ERROR_SPOKEN:
            spoken, _next = LANE_ERROR_SPOKEN[code]
            rendered = spoken.format(lane=display, detail=detail or "that chat")
            message = f"{rendered} {_next}"
        else:
            fallback = (
                str((result or {}).get("reason") or (result or {}).get("error") or "")
                or "background_transport_failed"
            )
            message = f"WhatsApp background connection: {fallback}"
    elif code in LANE_ERROR_SPOKEN:
        spoken, _next = LANE_ERROR_SPOKEN[code]
        if code in {"chat_not_found", "ambiguous_recipient"} and not detail:
            detail = "that conversation"
        message = f"{spoken.format(lane=display, detail=detail)} {_next}"
    else:
        fallback = (
            str((result or {}).get("reason") or (result or {}).get("error") or "")
            or "unknown_lane_failure"
        )
        message = f"{display} on the Mac reported a problem ({fallback}); nothing was assumed."

    if verb == "send" and "nothing went out" not in message and "wasn't sent" not in message:
        message = f"{message} Nothing went out."
    return message


def assert_delivery_evidence(
    lane: str,
    verb: str,
    payload: dict[str, Any] | None,
    *,
    provider: str = "",
) -> dict[str, Any]:
    """Normalize send-verb delivery proof, or refuse with missing evidence.

    Accepts the helper envelope (``delivery.confirmed`` / ``data.sent``), the
    CDP shape (``sent`` / ``message_id`` / ``outbound_verified``), and the API
    shape (``ts`` / ``message_id`` / ``id``). ``dry_run``, compose-only, and
    ``ok:false`` payloads always raise — shared §2.2 gate for lanes that do
    not run through ``run_life_helper``.
    """

    from app.integrations.life_helper import LifeHelperError

    body_src: dict[str, Any] = payload if isinstance(payload, dict) else {}
    _data: Any = body_src.get("data")
    data: dict[str, Any] = _data if isinstance(_data, dict) else {}
    body = {**body_src, **data}
    if body.get("dry_run") is True:
        raise LifeHelperError(
            f"{lane}.{verb} returned dry_run without real delivery evidence; refusing to report success",
            error_code="missing_delivery_evidence",
        )
    delivery = body_src.get("delivery")
    confirmed = isinstance(delivery, dict) and delivery.get("confirmed") is True
    confirmed_by = (
        str(delivery.get("confirmed_by") or "") if isinstance(delivery, dict) else ""
    )
    if not confirmed:
        for field in ("sent", "outbound_verified", "delivered"):
            if body.get(field) is True:
                confirmed, confirmed_by = True, field
                break
    receipt_id = ""
    if not confirmed:
        for field in ("message_id", "ts", "id"):
            value = body.get(field)
            if value:
                confirmed, confirmed_by, receipt_id = True, field, str(value)
                break
    if not confirmed or body.get("ok") is False:
        raise LifeHelperError(
            f"{lane}.{verb} returned ok without real delivery evidence; refusing to report success",
            error_code="missing_delivery_evidence",
        )
    evidence: dict[str, Any] = {
        "provider": provider or str(body.get("provider") or "lane"),
        "confirmed_by": confirmed_by or "sent",
    }
    for key in ("to", "destination", "chat", "subject", "message_id", "ts", "id"):
        if body.get(key):
            evidence[key] = body[key]
    if receipt_id and "message_id" not in evidence and "ts" not in evidence and "id" not in evidence:
        evidence["receipt"] = receipt_id
    return {"confirmed": True, "evidence": evidence}
