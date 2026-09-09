"""Turn a send/text/email ask into who + body + channel.

The same job has many wordings. This is a small grammar of *acts* (tell,
let-know, send-to, email), not a catalog of owner sentences. Spark can fill
gaps; the fallback never invents a recipient or a body.

MAC HUB: inquiry about mail/messages ("did I get any email from X") is NOT
a send. Do not treat this file as iPhone UI routing.
"""

from __future__ import annotations

import re
from typing import Any

_REL = r"Mom|Dad|Mother|Father|Mama|Papa|Mum|Mommy|Daddy"
_NAME = rf"{_REL}|[A-Z][\w.'-]+"
_TO = rf"(?:my\s+)?(?P<to>{_NAME})"
_CHANNEL_TAIL = r"(?:whatsapp|imessage|i-message|sms|rcs|mail|e-?mail|gmail)"
# A body that is ONLY a channel tail ("reply to mom on whatsapp", no words
# after) is a missing body, not a message whose text is "on whatsapp".
_BODY = (
    r"(?:that\s+|saying\s+|to say\s+|[:\-]\s*)?"
    rf"(?P<text>(?!on\s+{_CHANNEL_TAIL}\s*$).+)"
)
_SKIP_TO = frozenset(
    {
        "me",
        "us",
        "you",
        "it",
        "this",
        "that",
        "them",
        "her",
        "him",
        "someone",
        "from",
        "about",
        # Body lead-ins the chat-verb pattern would otherwise eat as a name
        # ("reply ... saying ok" must not send to "Saying").
        "say",
        "saying",
        "tell",
        "telling",
        # Prepositions are never a recipient ("whatsapp with mummy" is a
        # read, not a send to "With"; "reply to mom" must not send to "To").
        "with",
        "for",
        "on",
        "at",
        "in",
        "and",
        "or",
        "to",
        # File words are never a recipient ("the whatsapp backup file" must
        # not send "file" to "backup").
        "file",
        "files",
        "backup",
        "backups",
        "note",
        "notes",
    }
)
# Questions / lookups are not send acts. "email from Alex" is a mailbox
# ask; "email Ada the deck is ready" is a send.
_INQUIRY = re.compile(
    r"^\s*(?:hey \w+,?\s*)?(?:please\s+)?(?:can you\s+|could you\s+)?"
    r"(?:tell me\s+|do you know\s+)?"
    r"(?:what|which|who|did|do|have|has|is|are|any|check)\b",
    re.IGNORECASE,
)

_REPLY = re.compile(
    rf"\breply\s+(?:to\s+)?{_TO}"
    rf"(?:\s+on\s+(?:whatsapp|imessage|i-message|sms|mail|e-?mail))?"
    rf"\s+{_BODY}$",
    re.IGNORECASE,
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
# Incomplete send: send verb + recipient, body missing ("email mom", "text mom",
# "reply to mom"). WhatsApp is deliberately absent: bare "whatsapp mom" keeps
# reading that aisle (channel-first phrasing, working behavior).
_INCOMPLETE_LEAD = r"(?:please\s+|can\s+you\s+|could\s+you\s+|will\s+you\s+|would\s+you\s+)?"
_INCOMPLETE_ON_CHANNEL = rf"(?:\s+on\s+{_CHANNEL_TAIL})?"
_INCOMPLETE_VERB = re.compile(
    rf"^\s*{_INCOMPLETE_LEAD}"
    rf"(?:text|message|sms|imessage|i-message|ping|tell|e-?mail|mail)\s+"
    rf"(?:to\s+)?{_TO}{_INCOMPLETE_ON_CHANNEL}\s*$",
    re.IGNORECASE,
)
_INCOMPLETE_SEND_A = re.compile(
    rf"^\s*{_INCOMPLETE_LEAD}send\s+{_TO}\s+"
    rf"a\s+(?:text|message|sms|note|whatsapp|mail|e-?mail){_INCOMPLETE_ON_CHANNEL}\s*$",
    re.IGNORECASE,
)
_INCOMPLETE_SEND_TO = re.compile(
    rf"^\s*{_INCOMPLETE_LEAD}send\s+(?:a\s+|an\s+)?"
    rf"(?:text|message|sms|note|whatsapp|mail|e-?mail)\s+to\s+{_TO}{_INCOMPLETE_ON_CHANNEL}\s*$",
    re.IGNORECASE,
)
_INCOMPLETE_REPLY = re.compile(
    rf"^\s*{_INCOMPLETE_LEAD}reply\s+(?:to\s+)?{_TO}{_INCOMPLETE_ON_CHANNEL}\s*$",
    re.IGNORECASE,
)
_INCOMPLETE_LET_KNOW = re.compile(
    rf"^\s*{_INCOMPLETE_LEAD}let\s+{_TO}\s+know{_INCOMPLETE_ON_CHANNEL}\s*$",
    re.IGNORECASE,
)


def parse_send_intent(text: str | None) -> dict[str, Any] | None:
    """Return {to, text, channel?} or None. Never guesses a missing body."""

    raw = (text or "").strip()
    if not raw:
        return None
    if _INQUIRY.search(raw):
        return None
    for pattern in (_LET_KNOW, _SEND_TO, _SEND_A, _EMAIL, _REPLY, _TELL):
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
    return parse_send_intent(raw) is not None


def incomplete_send_recipient(text: str | None) -> str:
    """Recipient of a send-shaped ask missing its body, or "".

    "email mom" / "message mom" / "reply to mom" must prompt for the body —
    never read mail, never memory-search. Complete sends and inquiries
    return "" here.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    if parse_send_intent(raw) is not None:
        return ""
    if _INQUIRY.search(raw):
        return ""
    for pattern in (
        _INCOMPLETE_LET_KNOW,
        _INCOMPLETE_SEND_TO,
        _INCOMPLETE_SEND_A,
        _INCOMPLETE_REPLY,
        _INCOMPLETE_VERB,
    ):
        match = pattern.search(raw)
        if not match:
            continue
        to = (match.group("to") or "").strip()
        if not to or to.lower() in _SKIP_TO:
            continue
        return to
    return ""
