"""Owner-facing wording for a failed outbound action.

A failed physical action has exactly one job left: tell the owner what
happened and what would fix it. Raw exception class names, ``degraded``
flags, and bare "I couldn't do that" lines all fail that job, so every
surface that speaks a tool result routes the failure through here.

Pure string mapping — nothing here sends, and nothing here invents success.
"""

from __future__ import annotations

from typing import Any

# Which macOS permission a transport failure is actually about. The helper's
# own message is identical for every command, so the app is named from the
# channel instead of being left as "the requested permission".
_PERMISSION_APP = {
    "whatsapp": "WhatsApp",
    "messages": "Messages",
    "imessage": "Messages",
    "sms": "Messages",
    "mail": "Mail",
}

_PERMISSION_SIGNS = (
    "permission denied",
    "permissiondenied",
    "not authorized to send apple events",
    "-1743",
    "automation",
    "operation not permitted",
    "full disk access",
)

_MISSING_HELPER_SIGNS = (
    "helper_unavailable",
    "evlifehelper",
    "no such file",
    "not installed",
    "helper is not available",
)

_WEB_SIGNS = (
    "whatsapp_web_unavailable",
    "no_authenticated_tab",
    "whatsapp_web_not_linked",
    "osascript_failed",
    "chrome_not_running",
    "session_logged_out",
)

# Failures whose own wording is already specific and actionable.
_PASSTHROUGH = (
    "chat_not_found",
    "ambiguous_recipient",
    "compose_box_blocked",
    "empty_message",
    "whatsapp_web_tab_stale",
    "approved_route_unavailable",
    "approved_channel_changed",
    "no_contact",
    "no_whatsapp_chat",
    "no_email",
    "no_call_number",
    "unknown_channel",
)


def _blob(payload: Any) -> str:
    if isinstance(payload, dict):
        parts = [
            str(payload.get("spoken") or ""),
            str(payload.get("next_step") or ""),
            str(payload.get("error") or ""),
            str(payload.get("reason") or ""),
            str(payload.get("diagnosis") or ""),
        ]
        return " ".join(parts).lower()
    return str(payload or "").lower()


def _detail(payload: Any) -> str:
    if isinstance(payload, dict):
        value = (
            payload.get("next_step")
            or payload.get("reason")
            or payload.get("error")
            or ""
        )
        return str(value).strip()
    return str(payload or "").strip()


def spoken_failure(payload: Any, *, channel: str | None = None) -> str:
    """One honest, actionable sentence for a failed action.

    ``payload`` is the tool result (or a raw error string).
    """

    if isinstance(payload, dict):
        already = str(payload.get("spoken") or "").strip()
        if already and already.lower() not in {"not connected", "not_connected"}:
            return already
    blob = _blob(payload)
    if not blob.strip():
        return "I couldn't finish that."
    if "javascript_apple_events" in blob:
        # Chrome's own (non-TCC) gate: osascript JS is refused until the
        # owner allows it in Chrome's Developer menu.
        return (
            "Chrome is blocking background control. In Chrome open View \u2192 "
            "Developer \u2192 Allow JavaScript from Apple Events, then ask me again."
        )
    app = _PERMISSION_APP.get(str(channel or "").strip().lower(), "the app I was driving")
    if any(sign in blob for sign in _PERMISSION_SIGNS):
        return (
            f"macOS hasn't given me permission to control {app}, so nothing was sent. "
            f"Open System Settings → Privacy & Security → Automation, switch on {app}, "
            "then ask me again."
        )
    if any(sign in blob for sign in _MISSING_HELPER_SIGNS):
        return (
            "The Mac helper I use for Messages, Mail and WhatsApp isn't available, "
            "so nothing was sent. Install or start EVLifeHelper, then ask me again."
        )
    if any(sign in blob for sign in _WEB_SIGNS):
        return (
            "WhatsApp Web isn't open and signed in in Chrome, so nothing was sent. "
            "Open WhatsApp Web in Chrome, then ask me again."
        )
    if any(sign in blob for sign in _PASSTHROUGH):
        detail = _detail(payload)
        return detail or "I couldn't send that."
    detail = _detail(payload)
    if detail:
        detail = detail.split(":", 1)[-1].strip() if "Error" in detail else detail
        return f"I couldn't send that. {detail}"
    return "I couldn't send that."
