"""WhatsApp sends through the native Mac app via background Accessibility.

Second transport behind headless Web (CDP): used when Web is unlinked and the
native app is installed. The helper launches headless when needed, confirms
the send in-thread, and restores focus and hidden state. The AX permission is
an explicit owner grant; without it calls fail with setup instructions.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

_STATUS_TTL_SECONDS = 10.0
_status_cache: tuple[float, dict[str, Any]] | None = None


@dataclass(frozen=True)
class AXStatus:
    running: bool = False
    installed: bool = False
    hidden: bool = False
    active: bool = False
    ax_trusted: bool = False
    chat_count: int = 0
    composer_available: bool = False
    reachable: bool = False
    reason: str = ""


def _quarters(value: Any) -> bool:
    return value is True


async def ax_status(
    *, helper_path: str | None = None, refresh: bool = False,
) -> AXStatus:
    """Probe the native WhatsApp AX transport (short-TTL cached)."""

    global _status_cache
    now = time.monotonic()
    if not refresh and _status_cache is not None:
        stamped, cached = _status_cache
        if now - stamped < _STATUS_TTL_SECONDS:
            return AXStatus(**cached)
    from app.integrations.life_helper import run_life_helper

    try:
        result = await run_life_helper(
            "whatsapp.ax_status", {}, helper_path=helper_path,
        )
    except Exception as exc:  # noqa: BLE001 - probe answers, never raises
        return AXStatus(reason=f"helper_unavailable:{type(exc).__name__}")
    data = result.data or {}
    installed = _quarters(data.get("installed"))
    trusted = _quarters(data.get("accessibility_trusted"))
    if not installed:
        reason = "app_not_installed"
    elif not trusted:
        reason = "accessibility_not_granted"
    else:
        reason = ""
    status = AXStatus(
        running=_quarters(data.get("running")),
        installed=installed,
        hidden=_quarters(data.get("hidden")),
        active=_quarters(data.get("active")),
        ax_trusted=trusted,
        chat_count=int(data.get("chat_count") or 0),
        composer_available=_quarters(data.get("composer_available")),
        reachable=installed and trusted,
        reason=reason,
    )
    _status_cache = (now, {
        "running": status.running, "installed": status.installed,
        "hidden": status.hidden, "active": status.active,
        "ax_trusted": status.ax_trusted, "chat_count": status.chat_count,
        "composer_available": status.composer_available,
        "reachable": status.reachable, "reason": status.reason,
    })
    return status


def match_ax_chat(
    chats: list[dict[str, Any]], query: str,
) -> dict[str, Any] | None:
    """Whole-token chat match, or {"ambiguous": [names]} / None.

    Substring hits never send: "John" must not resolve "Johnson".
    """

    want = (query or "").strip().casefold()
    if not want:
        return None
    rows = [row for row in chats or [] if isinstance(row, dict)]
    exact = [row for row in rows if str(row.get("name") or "").strip().casefold() == want]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return {"ambiguous": [str(row.get("name") or "") for row in exact]}
    want_tokens = set(re.findall(r"[0-9a-z]+", want))
    hits = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        have = set(re.findall(r"[0-9a-z]+", name.casefold()))
        if want_tokens and want_tokens <= have:
            hits.append(row)
    if len(hits) == 1:
        return hits[0]
    if hits:
        return {"ambiguous": [str(row.get("name") or "") for row in hits]}
    return None


async def ax_chats(
    *, helper_path: str | None = None, limit: int = 80,
) -> list[dict[str, Any]]:
    """Chat list for whole-token resolution. Empty on any failure."""

    from app.integrations.life_helper import run_life_helper

    try:
        result = await run_life_helper(
            "whatsapp.ax_chats", {"limit": limit}, helper_path=helper_path,
        )
    except Exception:  # noqa: BLE001 - resolution answers, never raises
        return []
    chats = (result.data or {}).get("chats")
    return [row for row in chats if isinstance(row, dict)] if isinstance(chats, list) else []


async def ax_send(
    *, to: str, text: str, helper_path: str | None = None,
) -> dict[str, Any]:
    """Send via the native app. Normalized receipt, never raises."""

    from app.integrations.life_helper import (
        LifeHelperError,
        LifePermissionDeniedError,
        run_life_helper,
    )

    try:
        result = await run_life_helper(
            "whatsapp.ax_send", {"to": to, "text": text},
            helper_path=helper_path,
        )
    except LifePermissionDeniedError as exc:
        return {
            "ok": False, "sent": False, "to": to, "channel": "whatsapp",
            "error": "ax_permission_denied", "retry_safe": True,
            "spoken": (
                "WhatsApp on this Mac needs an Accessibility grant first: open System "
                "Settings → Privacy & Security → Accessibility and enable EVLifeHelper. "
                f"{exc}"
            ),
        }
    except LifeHelperError as exc:
        detail = dict(exc.data or {}) if isinstance(exc.data, dict) else {}
        code = str(detail.get("error") or exc.error_code or "failed")
        if code == "ambiguous_chat":
            return {
                "ok": False, "sent": False, "to": to, "channel": "whatsapp",
                "error": "ax_ambiguous_chat", "retry_safe": True,
                "candidates": [str(name) for name in detail.get("candidates") or []],
            }
        if code == "chat_not_found":
            return {
                "ok": False, "sent": False, "to": to, "channel": "whatsapp",
                "error": "ax_chat_not_found", "retry_safe": True,
            }
        if code == "send_not_confirmed":
            # The composer was pressed and cleared without confirmation: the
            # message may have sent. Never auto-retry; the owner checks first.
            return {
                "ok": False, "sent": False, "to": to, "channel": "whatsapp",
                "error": "ax_send_unconfirmed", "retry_safe": False,
                "spoken": (
                    "I couldn't confirm that WhatsApp send — check the chat before "
                    "retrying so it doesn't go out twice."
                ),
            }
        return {
            "ok": False, "sent": False, "to": to, "channel": "whatsapp",
            "error": "ax_compose_failed", "retry_safe": True,
            "detail": code,
        }
    except Exception as exc:  # noqa: BLE001 - transport answers, never raises
        return {
            "ok": False, "sent": False, "to": to, "channel": "whatsapp",
            "error": "ax_unavailable", "retry_safe": True,
            "detail": type(exc).__name__,
        }
    data = result.data or {}
    return {
        "ok": True, "sent": True, "to": str(data.get("to") or to),
        "channel": "whatsapp",
        "verified_in_thread": bool(data.get("verified_in_thread", False)),
        "chat_verified": bool(data.get("chat_verified", False)),
        "focus_stolen": bool(data.get("focus_stolen", False)),
        "focus_restored": bool(data.get("focus_restored", False)),
        "hidden_restored": bool(data.get("hidden_restored", False)),
        "retry_safe": False,
        "spoken": f"Sent WhatsApp to {data.get('to') or to}.",
    }
