"""WhatsApp recipient resolution and sends through Evie's headless CDP workspace.

Never drives the owner's ordinary browser tab or falls back to foreground
Desktop Accessibility. Pairing is an explicit owner setup operation.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from app.ev.messaging.recipients import score_person_name

WA_URL = "web.whatsapp.com"
STATUS_CACHE_SECONDS = 3.0
SEARCH_SETTLE_SECONDS = 0.7
SEARCH_POLL_ATTEMPTS = 6
WEB_AVAILABLE_SECONDS = 30.0

_status_cache: tuple[float, bool] | None = None


def _under_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _digits(value: str | None) -> str:
    return re.sub(r"\D+", "", value or "")


def _looks_like_number(value: str) -> bool:
    digits = _digits(value)
    return len(digits) >= 7 and not re.search(r"[A-Za-z]", value or "")


async def web_available(*, refresh: bool = False) -> bool:
    """Only Evie's authenticated headless workspace is eligible."""
    if _under_pytest():
        return False
    from app.config import settings
    from app.ev.messaging import whatsapp_cdp

    if not getattr(settings, "digital_ops_enabled", True):
        return False
    try:
        # A wedged debugger holds ensure_ready's restart/poll loop past a
        # minute. Every caller (policy, park, resolve, routing) already
        # treats False as "background unavailable", so expiry answers that
        # instead of holding the tool call. A cold launch keeps warming in
        # the background; the retry then finds it.
        async with asyncio.timeout(WEB_AVAILABLE_SECONDS):
            state, _diagnosis = await whatsapp_cdp.ensure_ready(
                reveal_workspace=False
            )
    except TimeoutError:
        return False
    return state == "linked"


async def _clear_search() -> None:
    # Every CDP transaction clears its own query under the workspace lock.
    return None


async def _search_rows(query: str) -> list[dict[str, Any]]:
    if _under_pytest():
        return []
    from app.ev.messaging import whatsapp_cdp

    outcome = await whatsapp_cdp.search_chats(query, limit=80)
    rows = outcome.get("chats") if outcome.get("ok") else []
    return [row for row in (rows or []) if isinstance(row, dict)]


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
    return f"{row.get('name') or ''} {row.get('phone') or ''} {row.get('chat_ref') or ''}"


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

    # An exact chat name beats punctuation/emoji variants: "Mansi" chooses the
    # chat literally named Mansi over "Mansi…!!", "Mansi(D)", etc. Only a lone
    # exact match wins; several exact rows stay ambiguous.
    exact = [
        row
        for row in candidates
        if str(row.get("name") or "").strip().casefold() == query.casefold()
    ]
    if exact:
        # The same chat can render twice (chat + contact search results); only
        # genuinely distinct refs with the same name stay ambiguous.
        refs = {
            str(row.get("chat_ref") or row.get("name") or "").strip().casefold()
            for row in exact
        }
        if len(exact) == 1 or len(refs) == 1:
            row = exact[0]
            name = str(row.get("name") or query)
            return {
                "status": "unique",
                "display": name,
                "chat_ref": str(row.get("chat_ref") or name),
                "row": row,
            }
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
    """Background only. Never fall back to the owner-visible Desktop app."""
    from app.ev.messaging import whatsapp_cdp

    return await whatsapp_cdp.send(to, text)
