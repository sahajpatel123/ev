"""Deterministic WhatsApp read/summarize fast path (background cache).

Reads come from ``app.ev.messaging.whatsapp_local`` (a read-only snapshot of
the linked WhatsApp desktop cache): instant, headless, zero focus theft, and
independent of the CDP DOM. Summaries are worded by MiMo from fetched text
only — never invented.

Sends are deliberately NOT handled here: they stay on the policy-gated digital
bus (``digital.act service='whatsapp'`` / ``life.send``), which parks an exact
recipient/message for owner approval. This module never bypasses approval.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger("ev.ev.whatsapp_flow")

_WA_RE = re.compile(r"\bwhats\s?app\b", re.IGNORECASE)
_SEND_RE = re.compile(r"\b(?:send|text|reply|write|tell|message)\b", re.IGNORECASE)
# Compose intent ("review ... and prepare a reply") is a send request even
# when it also contains read words. It must reach the approval-parked bus,
# so it can never be satisfied by the read/summarize fast path.
_COMPOSE_RE = re.compile(
    r"\b(?:prepare|draft|compose|suggest|respond|response)\b", re.IGNORECASE
)
_READ_RE = re.compile(
    r"\b(?:read|check|show|fetch|get|list|open|see|latest|unread|new)\b",
    re.IGNORECASE,
)
_SUMMARY_RE = re.compile(
    r"\b(?:summar(?:y|ise|ize)|digest|catch me up|brief|overview|what did i miss)\b",
    re.IGNORECASE,
)
_NAME_RE = re.compile(
    r"\b(?:from|with|to|about)\s+([^\s,?!]+(?:\s+[^\s,?!]+){0,3})"
)


def is_whatsapp_turn(text: str) -> bool:
    return bool(_WA_RE.search(text or ""))


def chat_name_from_text(text: str) -> str | None:
    match = _NAME_RE.search(text or "")
    if match is None:
        return None
    # Keep a leading dot (some WhatsApp names start with one) but drop a
    # sentence-final period and wrapping punctuation.
    name = match.group(1).strip(" ,'\"").rstrip(".")
    return name or None


async def _local_chats(limit: int = 12) -> list[dict[str, Any]]:
    from app.ev.messaging import whatsapp_local

    result = await whatsapp_local.search_chats("", limit=limit)
    return [row for row in (result.get("chats") or []) if isinstance(row, dict)]


def _plain_name(text: str) -> str:
    """Normalize for name matching: strip direction marks, unify spacing, casefold."""

    import unicodedata

    cleaned = "".join(
        ch for ch in str(text or "") if ch not in "\u200e\u200f\u2060\u200b\u200c\u200d"
    )
    return " ".join(unicodedata.normalize("NFC", cleaned).split()).casefold()


async def _resolve_name(name: str, chats: list[dict[str, Any]]) -> str | None:
    """Resolve a spoken name to a cache chat name (exact first, then containment)."""

    wanted = _plain_name(name)
    if not wanted:
        return None
    for chat in chats:
        if _plain_name(str(chat.get("name") or "")) == wanted:
            return str(chat.get("name"))
    for chat in chats:
        if wanted in _plain_name(str(chat.get("name") or "")):
            return str(chat.get("name"))
    return None


async def _local_read(name: str, *, limit: int = 5) -> list[dict[str, Any]]:
    from app.ev.messaging import whatsapp_local

    result = await whatsapp_local.read_recent(name, limit=limit)
    if result.get("error"):
        return []
    return [row for row in (result.get("messages") or []) if isinstance(row, dict)]


def _remote_ok() -> bool:
    """Remote wording is optional: without egress consent, stay deterministic."""

    try:
        from app.compliance.policy import remote_processing_allowed

        return bool(remote_processing_allowed("chat_egress"))
    except Exception:  # noqa: BLE001 - a broken policy check must not leak content
        return False


async def _word_read(fetch: str, fallback: str) -> str:
    """Let MiMo present fetched messages: long -> summarise, short -> quote.

    The length judgement is the model's, not a code threshold; the fetched text
    is the only permitted source. Message bodies never leave the machine when
    remote chat egress is not allowed: the deterministic fallback is spoken.
    """

    if not fetch.strip() or not _remote_ok():
        return fallback
    from app.contracts import ChatMessage
    from app.gateway.roles import chat_via_role

    try:
        result = await chat_via_role(
            [
                ChatMessage(
                    role="system",
                    content=(
                        "Present the owner's WhatsApp message(s) as a short spoken "
                        "reply. Decide naturally: if a message is long, summarise it "
                        "in one or two sentences and say what it is about; if it is "
                        "short, speak it as-is. Name the sender. Only use the supplied "
                        "text; never invent content. No markdown, no lists."
                    ),
                ),
                ChatMessage(role="user", content=fetch[:12000]),
            ],
            reasoning_effort="low",
        )
    except Exception:  # noqa: BLE001 - fall back to the raw fetch on model failure
        return fallback
    return (result.text or "").strip()[:2000] or fallback


def _fetch_text(name: str, messages: list[dict[str, Any]]) -> str:
    lines = [f"Chat: {name}"]
    for message in messages[-6:]:
        sender = "owner" if message.get("from_me") else name
        body = str(message.get("body") or message.get("text") or "").strip()
        if body:
            lines.append(f"{sender}: {body[:1200]}")
    return "\n".join(lines)


def _speak_rows(name: str, messages: list[dict[str, Any]], *, limit: int = 3) -> str:
    picked = [
        m for m in messages if str(m.get("body") or m.get("text") or "").strip()
    ][-limit:]
    if not picked:
        return f"I couldn't read messages from {name}."
    parts = []
    for message in picked:
        from_me = bool(message.get("from_me"))
        who = "You said" if from_me else "They said"
        body = str(message.get("body") or message.get("text") or "").strip()[:240]
        parts.append(f"{who}: {body}")
    return f"Latest in {name}. " + " ".join(parts)


def _speak_digest(chats: list[dict[str, Any]]) -> str:
    lines = []
    for chat in chats[:4]:
        name = str(chat.get("name") or "").strip()
        gist = str(chat.get("gist") or "").strip()
        if not name:
            continue
        lines.append(f"{name}: {gist[:160]}" if gist else name)
    if not lines:
        return "I couldn't read any WhatsApp chats right now."
    return "Latest on WhatsApp. " + " | ".join(lines)


async def _summary(chats: list[dict[str, Any]], named: str | None) -> str:
    selected = [
        c
        for c in chats
        if not named or _plain_name(named) in _plain_name(str(c.get("name") or ""))
    ]
    pack: list[str] = []
    for chat in selected[:4]:
        name = str(chat.get("name") or "").strip()
        if not name:
            continue
        messages = await _local_read(name, limit=5)
        if not messages:
            continue
        pack.append(f"Chat: {name}")
        for message in messages[-5:]:
            from_me = bool(message.get("from_me"))
            who = "owner" if from_me else "them"
            body = str(message.get("body") or message.get("text") or "").strip()[:300]
            pack.append(f"  [{who}] {body}")
        if len(pack) > 40:
            break
    if not pack:
        return "I couldn't read any WhatsApp chats right now."
    if not _remote_ok():
        return _speak_digest(selected or chats)
    from app.contracts import ChatMessage
    from app.gateway.roles import chat_via_role

    try:
        result = await chat_via_role(
            [
                ChatMessage(
                    role="system",
                    content=(
                        "Summarize the owner's WhatsApp for a spoken reply in 3 to 5 "
                        "short sentences. Only use the supplied messages; never invent "
                        "content. Say who needs a reply and what they asked. No markdown."
                    ),
                ),
                ChatMessage(role="user", content="\n".join(pack)[:12000]),
            ],
            reasoning_effort="low",
        )
    except Exception:  # noqa: BLE001 - deterministic digest on model failure
        return _speak_digest(selected or chats)
    return (result.text or "").strip()[:2000] or _speak_digest(selected or chats)


async def handle_whatsapp_turn(text: str) -> dict[str, Any] | None:
    """Read/summarize only. Sends return None so approval policy stays in charge."""

    raw = (text or "").strip()
    if not is_whatsapp_turn(raw):
        return None
    # Sends must go through the approval-parked bus, never this fast path.
    # A phrase that parses as a real send, or that only sounds send-ish without
    # any read/summarize word, is left to the policy-gated digital bus; a read
    # such as "tell me my latest WhatsApp messages" still takes the fast path.
    if _SEND_RE.search(raw) and not _SUMMARY_RE.search(raw):
        try:
            from app.ev.send_intent import parse_send_intent

            parsed_send = parse_send_intent(raw) is not None
        except Exception:  # noqa: BLE001 - parser failure must not turn sends into reads
            parsed_send = True
        if (
            parsed_send
            or _COMPOSE_RE.search(raw)
            or not (_READ_RE.search(raw) or _SUMMARY_RE.search(raw))
        ):
            return None
    if not (_SUMMARY_RE.search(raw) or _READ_RE.search(raw)):
        return None
    name = chat_name_from_text(raw)
    try:
        async with asyncio.timeout(20.0):
            chats = await _local_chats(limit=12)
            if _SUMMARY_RE.search(raw):
                spoken = await _summary(chats, name)
                return {"kind": "whatsapp_summary", "spoken": spoken}
            if name:
                # Query the cache by name directly so chats outside the recent
                # top-N still resolve.
                from app.ev.messaging import whatsapp_local

                matches = [
                    row
                    for row in (
                        (await whatsapp_local.search_chats(name, limit=8)).get("chats") or []
                    )
                    if isinstance(row, dict)
                ]
                resolved = await _resolve_name(name, matches) or await _resolve_name(name, chats)
                messages = await _local_read(resolved, limit=5) if resolved else []
                if not messages:
                    return {"kind": "whatsapp_read", "spoken": _speak_digest(chats)}
                spoken = await _word_read(
                    _fetch_text(resolved or name, messages),
                    _speak_rows(resolved or name, messages),
                )
                return {"kind": "whatsapp_read", "spoken": spoken}
            # No chat named: fetch the top two chats and let MiMo present them
            # (summarising any long message, quoting short ones).
            fetch_parts = []
            for chat in chats[:2]:
                chat_name = str(chat.get("name") or "").strip()
                if not chat_name:
                    continue
                messages = await _local_read(chat_name, limit=3)
                if messages:
                    fetch_parts.append(_fetch_text(chat_name, messages))
            if fetch_parts:
                spoken = await _word_read(
                    "\n\n".join(fetch_parts),
                    _speak_digest(chats),
                )
                return {"kind": "whatsapp_read", "spoken": spoken}
            return {"kind": "whatsapp_read", "spoken": _speak_digest(chats)}
    except TimeoutError:
        logger.warning("whatsapp read fast path timed out")
        return {"kind": "whatsapp", "spoken": "WhatsApp is taking too long to read right now."}
    except Exception:  # noqa: BLE001 - fall through to the normal bus/model path
        logger.debug("whatsapp fast path failed", exc_info=True)
        return None
