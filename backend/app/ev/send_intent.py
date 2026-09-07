"""Turn a send/text/email ask into who + body + channel.

The same job has many wordings. This is a small grammar of *acts* (tell,
let-know, send-to, email), not a catalog of owner sentences. Spark can fill
gaps; the fallback never invents a recipient or a body.
"""

from __future__ import annotations

import re
from typing import Any

_REL = r"Mom|Dad|Mother|Father|Mama|Papa|Mum|Mommy|Daddy"
_NAME = rf"{_REL}|[A-Z][\w.'-]+"
_TO = rf"(?:my\s+)?(?P<to>{_NAME})"
_BODY = r"(?:that\s+|saying\s+|to say\s+|[:\-]\s*)?(?P<text>.+)"
_SKIP_TO = frozenset(
    {"me", "us", "you", "it", "this", "that", "them", "her", "him", "someone"}
)

_TELL = re.compile(
    rf"\b(?:tell|text|txt|message|msg|sms|imessage|i-message|whatsapp|ping)\s+"
    rf"(?:to\s+)?{_TO}\s+{_BODY}$",
    re.IGNORECASE,
)
_LET_KNOW = re.compile(
    rf"\blet\s+{_TO}\s+know(?:\s+that)?\s+(?P<text>.+)$",
    re.IGNORECASE,
)
_SEND_TO = re.compile(
    rf"\bsend\s+(?:a\s+|an\s+)?(?:text|message|sms|note|whatsapp|mail|e-?mail)\s+to\s+"
    rf"{_TO}\s+{_BODY}$",
    re.IGNORECASE,
)
_SEND_A = re.compile(
    rf"\bsend\s+{_TO}\s+a\s+(?:text|message|sms|note|whatsapp|mail|e-?mail)\s+"
    rf"{_BODY}$",
    re.IGNORECASE,
)
_EMAIL = re.compile(
    rf"\b(?:e-?mail|mail)\s+(?:to\s+)?{_TO}\s+{_BODY}$",
    re.IGNORECASE,
)
_WHATSAPP = re.compile(r"\bwhatsapp\b", re.IGNORECASE)
_MAIL = re.compile(r"\b(?:e-?mails?|gmail)\b", re.IGNORECASE)
_IMESSAGE = re.compile(r"\b(?:imessage|i-message|sms|rcs)\b", re.IGNORECASE)


def parse_send_intent(text: str | None) -> dict[str, Any] | None:
    """Return {to, text, channel?} or None. Never guesses a missing body."""

    raw = (text or "").strip()
    if not raw:
        return None
    for pattern in (_LET_KNOW, _SEND_TO, _SEND_A, _EMAIL, _TELL):
        match = pattern.search(raw)
        if not match:
            continue
        to = (match.group("to") or "").strip()
        body = (match.group("text") or "").strip()
        if not to or not body:
            continue
        if to.lower() in _SKIP_TO:
            continue
        if len(body) < 2:
            continue
        payload: dict[str, Any] = {"to": to, "text": body[:500]}
        channel = channel_from_text(raw)
        if channel:
            payload["channel"] = channel
        return payload
    return None


def channel_from_text(text: str | None) -> str | None:
    raw = text or ""
    if _WHATSAPP.search(raw):
        return "whatsapp"
    if _MAIL.search(raw) and not _TELL.search(raw) and not _IMESSAGE.search(raw):
        if re.search(r"\b(?:e-?mail|mail)\b", raw, re.IGNORECASE):
            return "mail"
    if _IMESSAGE.search(raw):
        return "messages"
    if re.search(r"\b(?:e-?mail|mail)\s+", raw, re.IGNORECASE):
        return "mail"
    return None


def maybe_send_utterance(text: str | None) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(
        _TELL.search(raw)
        or _LET_KNOW.search(raw)
        or _SEND_TO.search(raw)
        or _SEND_A.search(raw)
        or _EMAIL.search(raw)
    )
