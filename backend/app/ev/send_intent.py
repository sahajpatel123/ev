"""Turn a send/text/email ask into who + body + channel.

The same job has many wordings. This is a small grammar of *acts* (tell,
let-know, send-to, email), not a catalog of owner sentences. Spark can fill
gaps; the fallback never invents a recipient or a body.

Multi-token recipients are handled in two ways: an explicit body lead
("... to John Smith saying running late") bounds the name, and otherwise a
captured extra token is only kept while it is capitalized or a name particle,
so "text mom running late" stays recipient=mom, body=running late. Channel
words come from the registry (``app.ev.messaging.channels``), never from a
catalog of ifs, and a channel is reported only when the owner named it.

MAC HUB: inquiry about mail/messages ("did I get any email from X") is NOT
a send. Do not treat this file as iPhone UI routing.
"""

from __future__ import annotations

import re
from typing import Any

from app.ev.messaging.channels import (
    CHANNELS,
    detect_channel,
    normalize_channel,
)

SEND_BODY_MAX = 500

__all__ = [
    "SEND_BODY_MAX",
    "channel_from_text",
    "incomplete_send",
    "incomplete_send_recipient",
    "looks_like_message_body",
    "maybe_send_utterance",
    "normalize_channel",
    "parse_send_intent",
    "prompt_for_send_body",
]

_REL = (
    r"Mom|Dad|Mother|Father|Mama|Papa|Mum|Mummy|Mommy|Daddy|"
    r"Ammi|Maa|Baba|Papa"
)
# Complete sends keep a single name token so "text mom hi" stays a body.
# Incomplete sends may take extra tokens ("customer care") because the
# utterance ends there.
_NAME_WORD = r"[A-Za-z][\w.'-]*"
_TO_TAIL_STOP = (
    r"(?:that|saying|to\s+say|about|the|an?\b|on|"
    r"i'm|i’m|i\s+am|\bi\b|we(?:'re|\s+are)?|"
    r"please|just|ok|okay|hi|hello|hey)"
)
_EXTRA_NAME_WORD = rf"(?!{_TO_TAIL_STOP}){_NAME_WORD}"
_NAME = rf"(?:{_REL}|{_NAME_WORD})"
_NAME_LONG = rf"(?:{_REL}|{_NAME_WORD}(?:\s+{_EXTRA_NAME_WORD}){{0,4}})"
_TO = rf"(?P<to>(?:my\s+|the\s+)?{_NAME})"
_TO_LONG = rf"(?P<to>(?:my\s+|the\s+)?{_NAME_LONG})"
_CHANNEL_WORDS = sorted(
    {
        alias
        for spec in CHANNELS
        for alias in (*spec.explicit_aliases, *spec.context_aliases)
    },
    key=len,
    reverse=True,
)
_CHANNEL_TAIL = r"(?:" + "|".join(re.escape(word) for word in _CHANNEL_WORDS) + r")"
_CHANNEL_LEAD = r"(?:whatsapp|telegram|signal|discord|slack|messenger|instagram|imessage)"
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
        "a",
        "an",
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
        # Channel/kind nouns are never a recipient
        # ("send whatsapp message to Mum" must not send to "message").
        "message",
        "messages",
        "whatsapp",
        "imessage",
        "sms",
        "rcs",
        "mail",
        "email",
        "e-mail",
        "text",
        "txt",
        "chat",
    }
)
_NAME_PARTICLES = frozenset(
    {"de", "del", "della", "van", "von", "bin", "al", "da", "dos", "ben", "ibn", "mac", "mc", "st"}
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
    rf"\breply\s+(?:to\s+)?{_TO_LONG}"
    rf"(?:\s+on\s+(?:{_CHANNEL_TAIL}))?"
    rf"\s+{_BODY}$",
    re.IGNORECASE,
)
_TELL = re.compile(
    rf"\b(?:tell|text|txt|message|msg|sms|imessage|i-message|whatsapp|ping|"
    rf"shoot|dm|{_CHANNEL_LEAD})\s+"
    rf"(?:to\s+)?{_TO_LONG}\s+{_BODY}$",
    re.IGNORECASE,
)
_TELL_DELIM = re.compile(
    rf"\b(?:tell|text|txt|message|msg|sms|imessage|i-message|whatsapp|ping|"
    rf"shoot|dm|{_CHANNEL_LEAD})\s+"
    rf"(?:to\s+)?{_TO_LONG}\s+"
    rf"(?:saying|that|to\s+say|telling|:|-)\s+(?P<text>.+)$",
    re.IGNORECASE,
)
_LET_KNOW = re.compile(
    rf"\blet\s+{_TO_LONG}\s+know(?:\s+that)?\s+(?P<text>.+)$",
    re.IGNORECASE,
)
_SEND_TO = re.compile(
    rf"\bsend\s+(?:a\s+|an\s+)?"
    rf"(?:(?P<channel>{_CHANNEL_TAIL})\s+)?"
    rf"(?:text|message|sms|note|msg|dm|whatsapp|imessage|i-message|telegram|"
    rf"mail|e-?mail)\s+to\s+"
    rf"{_TO_LONG}\s+{_BODY}$",
    re.IGNORECASE,
)
_SEND_DELIM = re.compile(
    rf"\b(?:send|shoot|fire\s+off|drop)\s+(?:a\s+|an\s+)?"
    rf"(?:(?P<channel>{_CHANNEL_TAIL})\s+)?"
    rf"(?:text|message|sms|note|msg|dm|whatsapp|imessage|i-message|telegram|"
    rf"mail|e-?mail)\s+to\s+{_TO_LONG}\s+"
    rf"(?:saying|that|to\s+say|telling|:|-)\s+(?P<text>.+)$",
    re.IGNORECASE,
)
_SEND_ON_CHANNEL = re.compile(
    rf"\bsend\s+{_TO_LONG}\s+"
    rf"(?:that\s+|saying\s+|to say\s+|[:\-]\s*)?"
    rf"(?P<text>.+?)"
    rf"\s+on\s+(?P<channel>{_CHANNEL_TAIL})\s*$",
    re.IGNORECASE,
)
_WHATSAPP_MSG = re.compile(
    rf"\b(?:whatsapp|telegram|signal)\s+(?:a\s+|an\s+)?(?:message|text|msg|dm)\s+to\s+{_TO_LONG}\s+{_BODY}$",
    re.IGNORECASE,
)
_SEND_A = re.compile(
    rf"\b(?:send|shoot|fire\s+off|drop)\s+{_TO_LONG}\s+"
    rf"a\s+(?:text|message|sms|note|msg|dm|whatsapp|telegram|mail|e-?mail)\s+"
    rf"{_BODY}$",
    re.IGNORECASE,
)
_EMAIL = re.compile(
    rf"\b(?:e-?mail|mail)\s+(?:to\s+)?{_TO_LONG}\s+{_BODY}$",
    re.IGNORECASE,
)
_MAIL = re.compile(r"\b(?:e-?mails?|gmail)\b", re.IGNORECASE)
# Incomplete send: send verb + recipient, body missing ("email mom", "text mom",
# "reply to mom"). WhatsApp is deliberately absent: bare "whatsapp mom" keeps
# reading that aisle (channel-first phrasing, working behavior).
_INCOMPLETE_LEAD = (
    r"(?:please\s+|can\s+you\s+|could\s+you\s+|will\s+you\s+|would\s+you\s+|"
    r"i\s+(?:need|want)\s+you\s+to\s+|i(?:'d| would)\s+like\s+you\s+to\s+)?"
)
_INCOMPLETE_ON_CHANNEL = rf"(?:\s+on\s+{_CHANNEL_TAIL})?"
_INCOMPLETE_VERB = re.compile(
    rf"^\s*{_INCOMPLETE_LEAD}"
    rf"(?:text|message|sms|imessage|i-message|ping|tell|e-?mail|mail|shoot|dm)\s+"
    rf"(?:to\s+)?{_TO_LONG}{_INCOMPLETE_ON_CHANNEL}\s*$",
    re.IGNORECASE,
)
_INCOMPLETE_SEND_A = re.compile(
    rf"^\s*{_INCOMPLETE_LEAD}(?:send|shoot)\s+{_TO_LONG}\s+"
    rf"a\s+(?:text|message|sms|note|whatsapp|telegram|mail|e-?mail){_INCOMPLETE_ON_CHANNEL}\s*$",
    re.IGNORECASE,
)
_INCOMPLETE_SEND_TO = re.compile(
    rf"^\s*{_INCOMPLETE_LEAD}send\s+(?:a\s+|an\s+)?"
    rf"(?:(?P<channel>{_CHANNEL_TAIL})\s+)?"
    rf"(?:text|message|sms|note|whatsapp|telegram|imessage|i-message|mail|e-?mail)\s+"
    rf"to\s+{_TO_LONG}{_INCOMPLETE_ON_CHANNEL}\s*$",
    re.IGNORECASE,
)
_INCOMPLETE_REPLY = re.compile(
    rf"^\s*{_INCOMPLETE_LEAD}reply\s+(?:to\s+)?{_TO_LONG}{_INCOMPLETE_ON_CHANNEL}\s*$",
    re.IGNORECASE,
)
_INCOMPLETE_LET_KNOW = re.compile(
    rf"^\s*{_INCOMPLETE_LEAD}let\s+{_TO_LONG}\s+know{_INCOMPLETE_ON_CHANNEL}\s*$",
    re.IGNORECASE,
)
# "send WhatsApp to Ada" / "send a WhatsApp to Ada" — channel is the kind.
_INCOMPLETE_SEND_CHANNEL = re.compile(
    rf"^\s*{_INCOMPLETE_LEAD}send\s+(?:a\s+|an\s+)?"
    rf"(?P<channel>{_CHANNEL_TAIL})\s+to\s+{_TO_LONG}\s*$",
    re.IGNORECASE,
)
_INCOMPLETE_WHATSAPP_MSG = re.compile(
    rf"^\s*{_INCOMPLETE_LEAD}(?:whatsapp|telegram|signal)\s+(?:a\s+|an\s+)?"
    rf"(?:message|text|msg|dm)\s+to\s+{_TO_LONG}\s*$",
    re.IGNORECASE,
)
# Grammar missed the tight patterns but the owner still named a send.
# File routing ("send this file to Documents") is not a message.
_LOOSE_SEND = re.compile(
    rf"\b(?:send|text|txt|e-?mail|mail|message|msg|imessage|i-message|reply|tell|"
    rf"shoot|dm|ping|{_CHANNEL_LEAD})\b"
    rf"(?P<mid>.{{0,80}}?)"
    rf"\bto\s+{_TO_LONG}"
    rf"(?:\s+{_BODY})?\s*$",
    re.IGNORECASE,
)


_ON_CHANNEL_TAIL = re.compile(rf"\s+on\s+{_CHANNEL_TAIL}\s*$", re.IGNORECASE)
_ON_CHANNEL_LEAD = re.compile(
    rf"^on\s+{_CHANNEL_TAIL}\s+(?=(?:saying|that|to\s+say)\b)\s*",
    re.IGNORECASE,
)
_ON_BODY_LEAD = re.compile(r"^(?:saying|that|to\s+say)\s+", re.IGNORECASE)


def _clean_body(body: str) -> str:
    """Strip a trailing/leading 'on <channel>' and a body lead-in."""

    text = _ON_CHANNEL_TAIL.sub("", body or "").strip()
    text = _ON_CHANNEL_LEAD.sub("", text).strip()
    text = _ON_BODY_LEAD.sub("", text).strip()
    return text


_KIND_ONLY_BODY = re.compile(
    r"^(?:a\s+|an\s+)?(?:text|message|sms|note|whatsapp|imessage|i-message|"
    r"telegram|mail|e-?mail|msg|dm)\s*$",
    re.IGNORECASE,
)
_SHORT_BODY_LEAD = re.compile(
    r"^(?:hi|hello|hey|yo|ok|okay|yes|yeah|yep|no|nope|thanks|thank you|"
    r"please|just|i'm|i’m|i\s+am|we(?:'re)?)\b",
    re.IGNORECASE,
)
_COMMAND_LEAD_TOKEN = re.compile(
    r"^(?:hey|hi|hello|ok|okay|please|evie|just)[,!]?\s+",
    re.IGNORECASE,
)
_COMMAND_LEAD_ASK = re.compile(
    r"^(?:can you|could you|will you|would you|"
    r"i need you to|i want you to|i(?:'d| would) like you to)\s+",
    re.IGNORECASE,
)


def _utterance_core(raw: str) -> str:
    """Strip vocatives / please so the send act must lead the turn."""

    text = (raw or "").strip()
    for _ in range(6):
        nxt = _COMMAND_LEAD_TOKEN.sub("", text, count=1)
        nxt = _COMMAND_LEAD_ASK.sub("", nxt, count=1)
        nxt = nxt.strip()
        if nxt == text:
            break
        text = nxt
    return text


def _recipient_overflow(to: str) -> tuple[str, str]:
    """Split a captured recipient into (name, likely body overflow).

    A captured extra token is only a name while it is capitalized or a known
    particle; a lowercase word starts the body. "mom running late" keeps
    recipient "mom"; "John Smith" keeps both; "john smith" keeps "john" and
    the explicit-delimiter grammar handles the all-lowercase case.
    """

    tokens = (to or "").split()
    if len(tokens) <= 1:
        return (to or "").strip(), ""
    keep = [tokens[0]]
    overflow: list[str] = []
    for token in tokens[1:]:
        # Contractions are never name parts ("text John I'll be late" keeps
        # recipient "John"): without this, "I'll" is Capitalized and would
        # be absorbed into the name, mangling both recipient and body.
        contracted = "'" in token or "\u2019" in token
        if (
            overflow
            or contracted
            or not (token[:1].isupper() or token.lower() in _NAME_PARTICLES)
        ):
            overflow.append(token)
        else:
            keep.append(token)
    return " ".join(keep), " ".join(overflow)


_BODY_LEAD_PREFIX = re.compile(rf"^(?:{_TO_TAIL_STOP})\b|^[:\-,]", re.IGNORECASE)
_BOUNDED_PATTERNS = frozenset({_SEND_DELIM, _TELL_DELIM, _LET_KNOW, _SEND_A})


def _capture_is_bounded(after: str) -> bool:
    """True when the captured name already stops at a body lead.

    "text john smith i'll be late" captured "john smith" because the next
    token is a body lead; the lowercase surname must not be trimmed away.
    "text mom running late" captured "mom running" with body "late"; there
    the lowercase extra is a body word and must be handed back.
    """

    rest = (after or "").lstrip()
    return not rest or bool(_BODY_LEAD_PREFIX.match(rest))


def _file_not_message(raw: str, mid: str) -> bool:
    """True when this 'send … to' is a file job, not a person/chat."""

    hay = f"{raw} {mid}"
    if re.search(
        r"\b(?:whatsapp|imessage|i-message|sms|rcs|e-?mail|gmail|telegram|signal)\b",
        raw,
        re.IGNORECASE,
    ):
        return False
    if re.search(r"\b(?:text|message|msg)\s+to\b", raw, re.IGNORECASE):
        return False
    return bool(re.search(r"\b(?:files?|pdfs?|folders?|documents?)\b", hay, re.IGNORECASE))


def _loose_send_act(text: str | None) -> dict[str, Any] | None:
    """Who (+ body) when the tight grammar missed a still-clear send act."""

    raw = (text or "").strip()
    if not raw or _INQUIRY.search(raw):
        return None
    match = _LOOSE_SEND.search(raw)
    if not match:
        return None
    to_raw = (match.group("to") or "").strip()
    after = raw[match.end("to") :]
    if _capture_is_bounded(after):
        to, overflow = to_raw, ""
    else:
        to, overflow = _recipient_overflow(to_raw)
    if not _valid_recipient(to):
        return None
    mid = match.group("mid") or ""
    if _file_not_message(raw, mid):
        return None
    body = (match.groupdict().get("text") or "").strip()
    body = _clean_body(body)
    if overflow:
        body = f"{overflow} {body}".strip()
    if body and _body_is_name_tail(body):
        to = f"{to} {body}".strip()
        body = ""
    if not _valid_recipient(to):
        return None
    if not body or _KIND_ONLY_BODY.match(body) or len(body) < 2:
        body = ""
    payload: dict[str, Any] = {"to": to}
    if body:
        payload["text"] = body[:SEND_BODY_MAX]
    channel = detect_channel(raw)
    if channel:
        payload["channel"] = channel
    return payload


def _valid_recipient(to: str) -> bool:
    who = (to or "").strip()
    if not who or who.lower() in _SKIP_TO:
        return False
    first = who.split()[0].lower()
    return first not in _SKIP_TO


def _body_is_name_tail(body: str) -> bool:
    """True when a one-word leftover is the rest of the name, not a message."""

    raw = (body or "").strip()
    if not raw or " " in raw:
        return False
    if _SHORT_BODY_LEAD.match(raw) or _KIND_ONLY_BODY.match(raw):
        return False
    if re.search(r"[.!?:,0-9]", raw):
        return False
    return bool(re.fullmatch(_NAME_WORD, raw)) and raw.lower() not in _SKIP_TO


_PATTERNS = (
    _LET_KNOW,
    _SEND_DELIM,
    _SEND_TO,
    _SEND_ON_CHANNEL,
    _WHATSAPP_MSG,
    _SEND_A,
    _EMAIL,
    _REPLY,
    _TELL_DELIM,
    _TELL,
)


def parse_send_intent(text: str | None) -> dict[str, Any] | None:
    """Return {to, text, channel?} or None. Never guesses a missing body."""

    raw = (text or "").strip()
    if not raw:
        return None
    if _INQUIRY.search(raw):
        return None
    core = _utterance_core(raw)
    if not core or _INQUIRY.search(core):
        return None
    for pattern in _PATTERNS:
        match = pattern.match(core)
        if not match:
            continue
        to_raw = (match.group("to") or "").strip()
        after = core[match.end("to") :]
        if pattern in _BOUNDED_PATTERNS or _capture_is_bounded(after):
            to, overflow = to_raw, ""
        else:
            to, overflow = _recipient_overflow(to_raw)
        body = (match.group("text") or "").strip()
        body = _clean_body(body)
        if overflow:
            body = f"{overflow} {body}".strip()
        if not to or not body:
            continue
        if not _valid_recipient(to):
            continue
        if _KIND_ONLY_BODY.match(body):
            continue
        if len(body) < 2:
            continue
        if _body_is_name_tail(body):
            continue
        payload: dict[str, Any] = {"to": to, "text": body[:SEND_BODY_MAX]}
        channel = detect_channel(raw)
        named = match.groupdict().get("channel")
        if named and not channel:
            channel = detect_channel(named) or named.strip().lower()
        if channel:
            payload["channel"] = channel
        return payload
    loose = _loose_send_act(core)
    if loose and loose.get("text"):
        return {k: v for k, v in loose.items() if k in {"to", "text", "channel"}}
    return None


def channel_from_text(text: str | None) -> str | None:
    """Canonical channel explicitly named in text (registry-backed)."""

    return detect_channel(text)


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
    core = _utterance_core(raw)
    hay = core or raw
    if parse_send_intent(hay) is not None:
        return ""
    if _INQUIRY.search(hay):
        return ""
    for pattern in (
        _INCOMPLETE_LET_KNOW,
        _INCOMPLETE_SEND_CHANNEL,
        _INCOMPLETE_WHATSAPP_MSG,
        _INCOMPLETE_SEND_TO,
        _INCOMPLETE_SEND_A,
        _INCOMPLETE_REPLY,
        _INCOMPLETE_VERB,
    ):
        match = pattern.search(hay)
        if not match:
            continue
        to, overflow = _recipient_overflow((match.group("to") or "").strip())
        if overflow:
            to = f"{to} {overflow}".strip()
        if not _valid_recipient(to):
            continue
        return to
    loose = _loose_send_act(hay)
    if loose and not loose.get("text") and _valid_recipient(str(loose.get("to") or "")):
        return str(loose.get("to") or "")
    return ""


def incomplete_send(text: str | None) -> dict[str, Any] | None:
    """Who + channel of a send-shaped ask missing its body, or None."""

    who = incomplete_send_recipient(text)
    if not who:
        return None
    payload: dict[str, Any] = {"to": who}
    channel = detect_channel(text)
    if channel:
        payload["channel"] = channel
    return payload


def looks_like_message_body(text: str | None) -> bool:
    """True when this utterance can fill a waiting send body.

    Questions, new send acts, and other jobs are not a body. A short
    statement after Evie asked 'what should I say?' is.
    """

    raw = (text or "").strip()
    if not raw or len(raw) > 500:
        return False
    if _INQUIRY.search(raw):
        return False
    if parse_send_intent(raw) is not None:
        return False
    return not bool(incomplete_send_recipient(raw))


def prompt_for_send_body(*, to: str, channel: str | None = None) -> str:
    who = (to or "").strip() or "them"
    wanted = (channel or "").strip().lower()
    if wanted in {"whatsapp", "wa"}:
        return f"What should I say to {who} on WhatsApp?"
    if wanted == "mail":
        return f"What should I email {who}?"
    if wanted in {"messages", "imessage", "sms"}:
        return f"What should I text {who}?"
    return f"What should I say to {who}?"
