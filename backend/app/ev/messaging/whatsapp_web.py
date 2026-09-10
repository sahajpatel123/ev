"""WhatsApp Web provider: send through the tab the owner already has open.

The owner keeps WhatsApp Web signed in on the Home Station Chrome. This
module drives that existing tab through ``chrome_session.eval_in_tab`` —
JavaScript in the tab's own context, never ``activate``, never a new window,
so the working desktop is untouched and ``focus_theft`` stays 0.

Delivery is only claimed when the text appears in the thread. Recipients are
resolved by whole-token identity (never the first loose search hit), and an
ambiguous or missing chat refuses instead of guessing.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from app.ev.messaging.recipients import score_person_name

WA_URL = "web.whatsapp.com"
STATUS_CACHE_SECONDS = 3.0
SEARCH_SETTLE_SECONDS = 0.7
SEARCH_POLL_ATTEMPTS = 6

_status_cache: tuple[float, bool] | None = None


def _under_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _digits(value: str | None) -> str:
    return re.sub(r"\D+", "", value or "")


def _looks_like_number(value: str) -> bool:
    digits = _digits(value)
    return len(digits) >= 7 and not re.search(r"[A-Za-z]", value or "")


async def web_available(*, refresh: bool = False) -> bool:
    """True when WhatsApp Web is signed in in an open Chrome tab.

    Under pytest this is always False unless a test injects a backing, so a
    live owner session can never be driven by the suite.
    """

    global _status_cache
    if _under_pytest():
        return False
    from app.config import settings

    if not getattr(settings, "digital_ops_enabled", True):
        return False
    now = time.monotonic()
    if not refresh and _status_cache is not None:
        stamped, value = _status_cache
        if now - stamped < STATUS_CACHE_SECONDS:
            return value
    from app.digital.chrome_session import chrome_running

    if not await chrome_running():
        _status_cache = (now, False)
        return False
    try:
        from app.digital.adapters.whatsapp import ComputerWhatsAppBacking

        status = await ComputerWhatsAppBacking().status()
        value = bool(status.get("authenticated"))
    except Exception:
        value = False
    _status_cache = (now, value)
    return value


async def _clear_search() -> None:
    """Remove the typed query so the owner's sidebar is left as found."""

    try:
        from app.digital import whatsapp_js as wjs
        from app.digital.chrome_session import eval_in_tab, wrap_js

        await eval_in_tab(
            url_contains=WA_URL,
            javascript=wrap_js(wjs.type_search_js("")),
        )
    except Exception:
        pass


async def _search_rows(query: str) -> list[dict[str, Any]]:
    """Raw sidebar rows from the existing tab after a typed search.

    WhatsApp repaints asynchronously, so the sidebar is polled instead of
    read once — a slow render must not look like "person not on WhatsApp".
    A typed query is cleared before returning; the tab is never left filtered.
    """

    if _under_pytest():
        return []
    from app.digital import whatsapp_js as wjs
    from app.digital.chrome_session import eval_in_tab, wrap_js

    typed_ok = False
    if query:
        typed = await eval_in_tab(
            url_contains=WA_URL,
            javascript=wrap_js(wjs.type_search_js(query)),
        )
        typed_ok = bool(typed.get("ok"))
        if typed_ok:
            await asyncio.sleep(SEARCH_SETTLE_SECONDS)
    rows: list[Any] = []
    for _ in range(SEARCH_POLL_ATTEMPTS):
        result = await eval_in_tab(
            url_contains=WA_URL,
            javascript=wrap_js(wjs.search_chats_js("")),
        )
        raw_rows = result.get("chats")
        rows = raw_rows if isinstance(raw_rows, list) else []
        if rows:
            break
        await asyncio.sleep(SEARCH_SETTLE_SECONDS)
    if not rows and query:
        # Typing filters the pane; the JS filter may have raced the repaint.
        result = await eval_in_tab(
            url_contains=WA_URL,
            javascript=wrap_js(wjs.search_chats_js(query)),
        )
        raw_rows = result.get("chats")
        rows = raw_rows if isinstance(raw_rows, list) else []
    if typed_ok:
        await _clear_search()
    return [row for row in rows if isinstance(row, dict)]


def _daemon_peer(query: str) -> dict[str, Any] | None:
    """WhatsApp Desktop ChatStorage match — works with the Web tab closed.

    The desktop copy knows chats whether or not they are saved in Apple
    Contacts; only a whole-name match is trusted.
    """

    from app.ev.messaging.recipients import verify_peer

    try:
        from app.services.life_stream_daemon import (
            get_life_stream_daemon,
            life_stream_should_run,
        )

        if not life_stream_should_run():
            return None
        peer = get_life_stream_daemon().resolve_whatsapp_peer(query)
    except Exception:
        return None
    return peer if verify_peer(query, peer) else None


def _row_haystack(row: dict[str, Any]) -> str:
    return f"{row.get('name') or ''} {row.get('gist') or ''} {row.get('chat_ref') or ''}"


def score_chat(query: str, row: dict[str, Any]) -> float:
    """Identity score for one sidebar row. Digits match on a numeric ask."""

    name = str(row.get("name") or "").strip()
    if not name:
        return 0.0
    if _looks_like_number(query):
        wanted = _digits(query)
        hay = _digits(_row_haystack(row))
        if not wanted or not hay:
            return 0.0
        if hay.endswith(wanted) or wanted.endswith(hay):
            return 1.0 if len(hay) >= len(wanted) else 0.9
        if wanted in hay:
            return 0.8
        return 0.0
    return score_person_name(query, name)


def _resolve_from_rows(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Unique-or-clarify scoring over sidebar rows."""

    scored: list[tuple[float, dict[str, Any]]] = []
    for row in candidates:
        score = score_chat(query, row)
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored:
        return {"status": "none", "display": query, "candidates": []}
    top_score, top = scored[0]
    if top_score < 0.8:
        return {"status": "none", "display": query, "candidates": []}
    tied = [row for score, row in scored if top_score - score < 0.1]
    if len(tied) > 1:
        return {
            "status": "ambiguous",
            "display": query,
            "candidates": [str(row.get("name") or "") for row in tied[:4]],
        }
    name = str(top.get("name") or query)
    return {
        "status": "unique",
        "display": name,
        "chat_ref": str(top.get("chat_ref") or name),
        "row": top,
    }


async def resolve(to: str, *, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Whole-name chat resolution from WhatsApp itself, never Apple Contacts.

    Order: the open Web tab's chat list, then the WhatsApp Desktop chat list
    (which also covers chats not saved in Apple Contacts). ``unique``,
    ``ambiguous``, ``none``, or ``desktop_only`` (chat exists on the desktop
    copy but the open tab did not render it — the caller can retry by phone).
    """

    query = (to or "").strip()
    if not query:
        return {"status": "none", "display": "", "candidates": []}
    if rows is None:
        candidates = await _search_rows(query) if await web_available() else []
    else:
        candidates = rows
    match = _resolve_from_rows(query, candidates)
    if match["status"] != "none" or rows is not None:
        return match
    peer = _daemon_peer(query)
    if not peer:
        return match
    handle = str(peer.get("handle") or "").strip()
    if handle and handle.lower() != query.lower():
        retry = _resolve_from_rows(query, await _search_rows(handle))
        if retry["status"] != "none":
            retry["peer"] = peer
            return retry
    return {
        "status": "desktop_only",
        "display": handle or query,
        "candidates": [],
        "peer": peer,
    }


async def send(to: str, text: str) -> dict[str, Any]:
    """Send through the open tab in the background. Never claims a tap-free send."""

    body = (text or "").strip()
    if not body:
        return {
            "ok": False,
            "sent": False,
            "channel": "whatsapp",
            "error": "empty_message",
            "spoken": "There's nothing to send.",
            "focus_theft": 0,
        }
    if not await web_available():
        return {
            "ok": False,
            "sent": False,
            "channel": "whatsapp",
            "error": "whatsapp_web_unavailable",
            "diagnosis": "no_authenticated_tab",
            "spoken": "WhatsApp Web isn't signed in on this Mac right now.",
            "focus_theft": 0,
        }
    match = await resolve(to)
    status = str(match.get("status") or "none")
    if status == "ambiguous":
        names = ", ".join(str(name) for name in match["candidates"] if name)
        return {
            "ok": False,
            "sent": False,
            "channel": "whatsapp",
            "error": "ambiguous_recipient",
            "candidates": match["candidates"],
            "spoken": f"I found more than one WhatsApp chat for {to}: {names}. Which one?",
            "focus_theft": 0,
        }
    if status not in {"unique", "desktop_only"}:
        return {
            "ok": False,
            "sent": False,
            "channel": "whatsapp",
            "error": "chat_not_found",
            "spoken": f"I couldn't find {to} on WhatsApp on this Mac.",
            "focus_theft": 0,
        }
    peer_raw = match.get("peer")
    peer: dict[str, Any] = peer_raw if isinstance(peer_raw, dict) else {}
    display = str(match.get("display") or to)
    chat_ref = str(match.get("chat_ref") or display)
    if status == "desktop_only":
        # The chat exists in WhatsApp Desktop but the open tab did not list
        # it by name; unsaved chats are addressable by phone.
        phone = str(peer.get("phone") or "").strip()
        if not phone:
            return {
                "ok": False,
                "sent": False,
                "channel": "whatsapp",
                "error": "whatsapp_web_tab_stale",
                "spoken": (
                    f"I see {display} in WhatsApp Desktop, but the open Web tab "
                    "didn't list that chat. Reload WhatsApp Web and try again."
                ),
                "focus_theft": 0,
            }
        chat_ref = phone

    from app.digital.adapters.whatsapp import ComputerWhatsAppBacking

    backing = ComputerWhatsAppBacking()
    try:
        opened = await backing.open_chat(chat_ref)
        chat_ref = str(opened.get("chat_ref") or chat_ref)
        sent = await backing.send(chat_ref, body)
        verified = bool(sent.get("verified_in_thread") or sent.get("sent"))
        if not verified:
            # Slow renders can miss the first read; poll before calling it a
            # failure — a duplicate retry is prevented by the backing's own
            # observe-before-send check.
            for _ in range(3):
                await asyncio.sleep(0.7)
                recent = await backing.read_recent(chat_ref, limit=5)
                if any(body in str(row.get("text") or "") for row in recent):
                    verified = True
                    break
    except Exception as exc:
        return {
            "ok": False,
            "sent": False,
            "channel": "whatsapp",
            "error": "whatsapp_web_send_failed",
            "diagnosis": type(exc).__name__,
            "spoken": f"I couldn't send that WhatsApp to {display}.",
            "focus_theft": 0,
        }
    display = str(sent.get("chat_ref") or display)
    return {
        "ok": verified,
        "sent": verified,
        "channel": "whatsapp",
        "to": display,
        "verified_in_thread": verified,
        "duplicate_prevented": bool(sent.get("duplicate_prevented")),
        "focus_theft": int(sent.get("focus_theft") or 0),
        "spoken": (
            f"Sent WhatsApp to {display}."
            if verified
            else f"I couldn't send that WhatsApp to {display}."
        ),
    }
