"""Generalized outbound channel registry.

One place that knows what a channel *is*: its canonical id, the words the
owner uses for it, whether it can be addressed 1:1 or by group, whether it
delivers without a tap, and which helper command (if any) carries it.

Adding a channel is data, not a new ``if`` branch. Unregistered channels are
reported honestly as "not connected" instead of silently falling back to a
different channel. Nothing here sends; routing and execution live in
``app.ev.messaging.routing``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ChannelKind = Literal["text", "mail", "voice"]
AddressKind = Literal["phone", "handle", "email"]


@dataclass(frozen=True)
class ChannelSpec:
    """One addressable channel the owner can send through."""

    id: str
    label: str
    kind: ChannelKind
    explicit_aliases: tuple[str, ...] = ()
    context_aliases: tuple[str, ...] = ()
    helper_command: str | None = None
    confirmation: str | None = None
    address: AddressKind = "handle"
    autosend: bool = True
    groups: bool = False
    media: bool = False
    service: str | None = None
    note: str = ""

    @property
    def wired(self) -> bool:
        return self.helper_command is not None


# Registered channels. `explicit_aliases` may match anywhere in an utterance;
# `context_aliases` only match after a preposition ("on messages", "via text"),
# so the verb "message" never turns into a channel by itself.
CHANNELS: tuple[ChannelSpec, ...] = (
    ChannelSpec(
        id="messages",
        label="Messages",
        kind="text",
        explicit_aliases=("apple messages",),
        context_aliases=("messages", "message", "imessage", "imessages"),
        helper_command="messages.send",
        confirmation="sent",
        address="handle",
        service="auto",
        groups=True,
    ),
    ChannelSpec(
        id="imessage",
        label="iMessage",
        kind="text",
        explicit_aliases=("imessage", "i-message", "i message", "imessages"),
        helper_command="messages.send",
        confirmation="sent",
        address="handle",
        service="imessage",
    ),
    ChannelSpec(
        id="sms",
        label="SMS",
        kind="text",
        explicit_aliases=("sms", "text message", "green bubble"),
        context_aliases=("text", "texts"),
        helper_command="messages.send",
        confirmation="sent",
        address="phone",
        service="sms",
        note="RCS is not addressable from this Mac; SMS is the transport.",
    ),
    ChannelSpec(
        id="whatsapp",
        label="WhatsApp",
        kind="text",
        explicit_aliases=("whatsapp", "whats app", "what's app"),
        context_aliases=("wa",),
        helper_command="whatsapp.send",
        confirmation="opened",
        address="phone",
        autosend=False,
        note="WhatsApp compose opens with the text ready; the owner taps send.",
    ),
    ChannelSpec(
        id="telegram",
        label="Telegram",
        kind="text",
        explicit_aliases=("telegram",),
        context_aliases=("tg",),
        address="handle",
    ),
    ChannelSpec(
        id="signal",
        label="Signal",
        kind="text",
        explicit_aliases=("signal",),
        address="phone",
    ),
    ChannelSpec(
        id="slack",
        label="Slack",
        kind="text",
        explicit_aliases=("slack",),
        address="handle",
    ),
    ChannelSpec(
        id="discord",
        label="Discord",
        kind="text",
        explicit_aliases=("discord",),
        address="handle",
    ),
    ChannelSpec(
        id="messenger",
        label="Messenger",
        kind="text",
        explicit_aliases=("facebook messenger", "fb messenger", "messenger"),
        address="handle",
    ),
    ChannelSpec(
        id="instagram",
        label="Instagram",
        kind="text",
        explicit_aliases=("instagram", "insta", "ig dm"),
        address="handle",
    ),
    ChannelSpec(
        id="linkedin",
        label="LinkedIn",
        kind="text",
        explicit_aliases=("linkedin",),
        address="handle",
    ),
    ChannelSpec(
        id="twitter",
        label="X",
        kind="text",
        explicit_aliases=("twitter dm", "x dm", "dm on x", "twitter"),
        address="handle",
    ),
    ChannelSpec(
        id="bluesky",
        label="Bluesky",
        kind="text",
        explicit_aliases=("bluesky",),
        address="handle",
    ),
    ChannelSpec(
        id="teams",
        label="Teams",
        kind="text",
        explicit_aliases=("microsoft teams", "ms teams", "teams"),
        address="handle",
    ),
    ChannelSpec(
        id="google_chat",
        label="Google Chat",
        kind="text",
        explicit_aliases=("google chat", "gchat", "google hangouts"),
        address="handle",
    ),
    ChannelSpec(
        id="wechat",
        label="WeChat",
        kind="text",
        explicit_aliases=("wechat", "we chat"),
        address="handle",
    ),
    ChannelSpec(
        id="line",
        label="LINE",
        kind="text",
        explicit_aliases=("line app",),
        address="handle",
    ),
    ChannelSpec(
        id="viber",
        label="Viber",
        kind="text",
        explicit_aliases=("viber",),
        address="phone",
    ),
    ChannelSpec(
        id="kakaotalk",
        label="KakaoTalk",
        kind="text",
        explicit_aliases=("kakaotalk", "kakao talk"),
        address="handle",
    ),
    ChannelSpec(
        id="snapchat",
        label="Snapchat",
        kind="text",
        explicit_aliases=("snapchat", "snap"),
        address="handle",
    ),
    ChannelSpec(
        id="matrix",
        label="Matrix",
        kind="text",
        explicit_aliases=("matrix",),
        address="handle",
    ),
    ChannelSpec(
        id="irc",
        label="IRC",
        kind="text",
        explicit_aliases=("irc",),
        address="handle",
    ),
    ChannelSpec(
        id="mail",
        label="Mail",
        kind="mail",
        explicit_aliases=("email", "e-mail", "gmail", "mail"),
        helper_command="mail.send",
        confirmation="sent",
        address="email",
        media=True,
    ),
)

_BY_ID: dict[str, ChannelSpec] = {spec.id: spec for spec in CHANNELS}


def _alias_index() -> tuple[tuple[str, str, bool], ...]:
    """(alias, channel_id, needs_preposition) longest alias first."""

    rows: list[tuple[str, str, bool]] = []
    for spec in CHANNELS:
        rows.extend((alias, spec.id, False) for alias in spec.explicit_aliases)
        rows.extend((alias, spec.id, True) for alias in spec.context_aliases)
    rows.sort(key=lambda row: len(row[0]), reverse=True)
    return tuple(rows)


_ALIASES = _alias_index()
_ALIAS_RE = re.compile(
    r"(?<![\w])(?P<alias>"
    + "|".join(re.escape(alias) for alias, _, _ in _ALIASES)
    + r")(?![\w])",
    re.IGNORECASE,
)
_PREP_RE = re.compile(
    r"\b(?:on|via|through|using|over|by)\s+(?:the\s+)?"
    r"(?P<channel>[a-z][a-z0-9 +_'’.-]{1,32})",
    re.IGNORECASE,
)
_BODY_LEAD_RE = re.compile(
    r"\b(?:saying|that|about|to\s+say|telling|regarding|re)\b|:",
    re.IGNORECASE,
)


def _command_head(text: str) -> str:
    """Text before the body starts, so body words never pick a channel."""

    match = _BODY_LEAD_RE.search(text or "")
    return (text or "")[: match.start()] if match else (text or "")


def channel_spec(name: str | None) -> ChannelSpec | None:
    """Resolve a channel id or alias to its spec. Unknown -> None."""

    raw = (name or "").strip().lower()
    if not raw:
        return None
    candidates = (raw, raw.replace("_", " "), raw.replace("-", " "))
    for candidate in candidates:
        if candidate in _BY_ID:
            return _BY_ID[candidate]
        for spec in CHANNELS:
            if candidate == spec.label.lower():
                return spec
            if candidate in spec.explicit_aliases or candidate in spec.context_aliases:
                return spec
    return None


def normalize_channel(name: str | None) -> str | None:
    """Canonical channel id for a name/alias, or None when unknown/empty."""

    spec = channel_spec(name)
    return spec.id if spec else None


def _alias_hit(alias: str) -> tuple[str, bool] | None:
    for candidate, channel_id, needs_prep in _ALIASES:
        if candidate == alias:
            return channel_id, needs_prep
    return None


def _preposition_channel(text: str) -> tuple[str, str] | None:
    """(channel_id, matched_token) for an explicit 'on <channel>' phrase."""

    match = _PREP_RE.search(text or "")
    if not match:
        return None
    phrase = (match.group("channel") or "").strip()
    if not phrase:
        return None
    spec = channel_spec(phrase)
    if spec is not None:
        return spec.id, phrase
    first = phrase.split()[0].strip(".,;:!?")
    spec = channel_spec(first)
    if spec is not None:
        return spec.id, first
    return None


def detect_channel(text: str | None) -> str | None:
    """Canonical channel explicitly named in the utterance, or None.

    Prepositional phrasing wins ("send it on WhatsApp" beats a stray word),
    then the earliest explicit alias. Context aliases only count after a
    preposition so "send a message" stays channel-neutral.
    """

    raw = text or ""
    if not raw:
        return None
    prepositional = _preposition_channel(raw)
    if prepositional is not None:
        return prepositional[0]
    head = _command_head(raw)
    best: tuple[int, str] | None = None
    for match in _ALIAS_RE.finditer(head):
        hit = _alias_hit(match.group("alias").lower())
        if hit is None:
            continue
        channel_id, needs_prep = hit
        if needs_prep:
            continue
        if best is None or match.start() < best[0]:
            best = (match.start(), channel_id)
    return best[1] if best else None


def unknown_channel(text: str | None) -> str | None:
    """A platform word in 'on X' that is not registered, else None.

    Only a single alphabetic token right after a preposition qualifies, and
    time/ordinary English words are rejected, so "send it on tuesday" never
    becomes a channel.
    """

    match = _PREP_RE.search(text or "")
    if not match:
        return None
    phrase = (match.group("channel") or "").strip()
    token = phrase.split()[0].strip(".,;:!?").lower() if phrase else ""
    if not token or not re.fullmatch(r"[a-z][a-z0-9+_.'’-]{2,24}", token):
        return None
    if token in _TIME_WORDS or token in _NON_CHANNEL_WORDS:
        return None
    if channel_spec(token) is not None:
        return None
    return token


_NON_CHANNEL_WORDS = frozenset(
    {
        "the",
        "this",
        "that",
        "these",
        "those",
        "my",
        "our",
        "your",
        "his",
        "her",
        "their",
        "way",
        "road",
        "train",
        "plane",
        "flight",
        "deck",
        "bike",
        "car",
        "bus",
        "phone",
        "speaker",
        "screen",
        "call",
        "team",
        "project",
        "list",
        "board",
        "group",
        "chat",
    }
)


_TIME_WORDS = frozenset(
    {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "today",
        "tomorrow",
        "tonight",
        "morning",
        "afternoon",
        "evening",
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "holiday",
        "vacation",
        "second",
        "minute",
        "hour",
        "day",
        "week",
        "month",
        "year",
        # Bare time/date words that follow "on" in ordinary bodies ("send it
        # on time", "ping me on the date") must never become channels.
        "time",
        "date",
        "weekend",
        "noon",
        "midnight",
    }
)


def channel_from_text(text: str | None) -> str | None:
    """Back-compat shim: canonical channel mentioned in a send utterance."""

    return detect_channel(text)


def channel_label(channel: str | None) -> str:
    spec = channel_spec(channel)
    return spec.label if spec else (channel or "message")


def channel_aliases(channel: str | None) -> tuple[str, ...]:
    spec = channel_spec(channel)
    if spec is None:
        return ()
    return tuple(spec.explicit_aliases) + tuple(spec.context_aliases)


def spoken_channel(channel: str | None) -> str:
    """Human wording for a channel in a spoken sentence."""

    label = channel_label(channel)
    if channel in {"messages", "imessage", "sms"}:
        return "Messages"
    if channel == "mail":
        return "email"
    return label
