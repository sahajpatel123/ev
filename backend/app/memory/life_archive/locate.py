"""Typed short-path locator for life-archive events.

The archive stays in Postgres. This module never injects it into a realtime
prompt. A question maps to one shelf; that shelf is scanned with a tight
limit; the model receives at most ``MAX_HITS`` text-only evidence rows.

``never_send_to_model`` is always excluded. File paths are never returned.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import String, cast, extract, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.paths import atomic_write_json, ensure_tree
from app.models import Event
from app.utils.text import simple_tokens, utcnow

SOURCE = "life_archive"
MAX_HITS = 8
SCAN_CAP = 48

SHELF_TYPES: dict[str, tuple[str, ...]] = {
    "contacts": ("life.contact",),
    "people": ("life.person",),
    "familiarity": ("life.person", "life.owner.voice"),
    "chats": ("life.chat.thread", "life.chat.excerpt"),
    "mail": ("life.mail.envelope",),
    "photos": ("life.photo.index",),
    "notes": ("life.note",),
    "tasks": ("life.task",),
    "calendar": ("life.calendar.event", "life.task"),
    "bookmarks": ("life.bookmark",),
    "owner": ("life.owner.voice",),
    "health": ("life.health.metric",),
}

AISLE_HEADERS = {
    "contacts": "Address-book names only. Open when they ask who is in their contacts.",
    "people": "People Evie knows from WhatsApp and life — one card each.",
    "familiarity": "Close people and how the owner writes. Open when they ask if you know them.",
    "chats": "WhatsApp thread summaries, not full logs.",
    "mail": "Mail subjects only. Open for inbox/email questions.",
    "photos": "Photo pointers by filename, album, or year. Not the pixels.",
    "notes": "Keep, Drive, and notebook files. Titles first.",
    "tasks": "Imported reminders and tasks.",
    "calendar": "Old exported calendar events, not live Calendar.app.",
    "bookmarks": "Saved links and reading lists.",
    "owner": "How the owner writes in WhatsApp. Open only for voice/style questions.",
    "health": "Recorded health snapshots. Open for health history, not live vitals.",
}

_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "do",
        "find",
        "for",
        "from",
        "have",
        "hello",
        "hey",
        "hi",
        "how",
        "hows",
        "i",
        "in",
        "is",
        "just",
        "me",
        "my",
        "of",
        "on",
        "show",
        "the",
        "to",
        "was",
        "were",
        "did",
        "does",
        "what",
        "when",
        "who",
        "with",
        "you",
        "your",
    }
)

_SHELF_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mail", ("email", "e-mail", "gmail", "inbox", "mailbox", "mail subject")),
    ("photos", ("photo", "photos", "picture", "pictures", "album", "screenshot", "selfie")),
    ("contacts", ("contact", "contacts", "address book", "phone book", "vcard")),
    ("calendar", ("imported calendar", "old calendar", "calendar export", "icloud calendar")),
    ("tasks", ("todo", "to-do", "task", "tasks", "reminder", "reminders")),
    ("notes", ("notebooklm", "keep note", "my notes", "note titled", "drive file", "icloud drive", "notes")),
    ("bookmarks", ("bookmark", "bookmarks", "saved list", "reading list")),
    ("chats", ("whatsapp chat", "whatsapp with", "whatsapp thread", "texted", "messaged", "my chats", "chats with", "conversations with", "conversation with")),
    ("people", ("who do i know", "people i know", "my family", "my friends", "who do i talk to")),
    ("owner", ("how do i talk", "how i text", "how i write", "what am i like", "my personality")),
    ("health", ("health record", "health history", "health snapshot", "old health")),
)

_FAMILIARITY = re.compile(
    r"\b("
    r"do you know me|know me well|what do you know about me|"
    r"what do you remember about me|"
    r"do you remember me|remember who i am|"
    r"do you know who i am|"
    r"tell me about me|"
    r"who am i(?: to you)?(?=\?|[.]|$)|"
    r"my (?:life|history|past|story)\b|"
    r"how well do you know me|"
    r"you know me|we've known"
    r")",
    re.IGNORECASE,
)
_CURRENT_TALK = re.compile(
    r"\b(this conversation|our conversation|what did we talk|what were we talking|"
    r"what have we (?:been )?talking|have a conversation|"
    r"let'?s (?:have a )?conversation|conversation with (?:me|you|us))\b",
    re.IGNORECASE,
)
_CONVERSATION_AISLE = re.compile(
    r"\b("
    r"conversations? with|"
    r"(?:my|what|which|those|these|about) conversations?|"
    r"conversations? (?:have i|i(?:'ve| have)|with)|"
    r"my (?:chats|conversations)|"
    r"whatsapp|"
    r"who (?:have i|do i) (?:been )?(?:talking|texting)|"
    r"who do i talk to|"
    r"chats? with|"
    r"people i (?:talk|text|chat)|"
    r"talk(?:ed|ing)? (?:about|to|with).{0,48}people|"
    r"talked (?:to|with) (?:people|everyone|somebody|someone|anybody)"
    r")\b",
    re.IGNORECASE,
)
_WHO_I_TALK = re.compile(
    r"\b(who (?:have i|do i) (?:been )?(?:talking|texting)|who do i talk to|"
    r"people i (?:talk|text|chat))\b",
    re.IGNORECASE,
)
_PERSON_ASK = re.compile(
    r"\b(do i know|who is|who's|who was|named|called)\b",
    re.IGNORECASE,
)
_SEND_NOW = re.compile(
    r"^\s*(?:(?:hey|ok|okay|evie|e v)\s+)*"
    r"(?:text|txt|message|msg|ping|sms|call|ring|dial)\s+\S+"
    r"|"
    r"^\s*(?:(?:hey|ok|okay|evie|e v)\s+)*"
    r"(?:remind me|set a reminder|start a timer)\b",
    re.IGNORECASE,
)
_ACT_NOW = re.compile(
    r"\b("
    r"send (?:a )?(?:text|message|note|sms|whatsapp)|"
    r"send \S+ a (?:text|message|note|sms|whatsapp)|"
    r"open (?:up )?(?:the )?whatsapp|"
    r"message \S+"
    r")\b",
    re.IGNORECASE,
)
_PROPER_NAME = re.compile(r"\b([A-Z][a-z]{1,30}(?:\s+[A-Z][a-z]{1,30}){0,3})\b")
_QUESTION_START = frozenset(
    {
        "what",
        "who",
        "how",
        "when",
        "where",
        "why",
        "which",
        "did",
        "does",
        "do",
        "is",
        "are",
        "can",
        "could",
        "would",
        "should",
        "will",
        "was",
        "were",
        "have",
        "has",
        "had",
        "please",
        "tell",
        "show",
        "find",
        "get",
    }
)
_YEAR = re.compile(r"\b(20\d{2})\b")
# Spoken address + "can you" must not become archive search tokens.
# "Hey Eve, can you tell me about my chats" is the same aisle as the short ask.
_LIVE_PREFIX = re.compile(
    r"^\s*(?:(?:hey|hi|hello|ok|okay|so)[,!\s]+)*"
    r"(?:eve|evie|e\s*v)[,!\s]+"
    r"(?:(?:can|could|would)\s+you\s+|please\s+)?",
    re.IGNORECASE,
)
_POLITE_ASK = re.compile(
    r"^\s*(?:(?:can|could|would)\s+you\s+|please\s+)",
    re.IGNORECASE,
)
_CHAT_WITH_PERSON = re.compile(
    r"\b(whatsapp|texted|messaged|chatted|said|says|say|told|tell|asked|asking|spoke|speaking|talking|talk)\b",
    re.IGNORECASE,
)
# "what did I talk about with Mansi" is a WhatsApp quote, not Evie-history
# and not a camera keep. "what did we talk about" stays current-talk.
# "what were we talking about with Ada" is that chat, not this live turn.
_CHAT_WITH_OTHER = re.compile(
    r"\b(?:"
    r"(?:talk(?:ed|ing)?|chat(?:ted|ting)?|text(?:ed|ing)?|whatsapp(?:ed)?|spoke)"
    r"(?:\s+about)?"
    r"\s+(?:to|with)\s+(?:my\s+)?([A-Za-z][A-Za-z.'-]{1,30})"
    r"|"
    r"(?:last|recent)\s+(?:chat|talk|message|text|whatsapp)\s+"
    r"(?:i\s+(?:had|did)\s+)?with\s+(?:my\s+)?([A-Za-z][A-Za-z.'-]{1,30})"
    r"|"
    r"chat i (?:had|did) with\s+(?:my\s+)?([A-Za-z][A-Za-z.'-]{1,30})"
    r"|"
    r"(?:catch me up|bring me up to speed|update me)\s+"
    r"(?:on|with|about)\s+(?:my\s+)?([A-Za-z][A-Za-z.'-]{1,30})"
    r"|"
    r"(?:latest|recent news|any word)\s+from\s+(?:my\s+)?([A-Za-z][A-Za-z.'-]{1,30})"
    r")\b",
    re.IGNORECASE,
)
_SELF_ADDRESSEE = frozenset(
    {
        "i",
        "me",
        "we",
        "us",
        "you",
        "u",
        "her",
        "him",
        "them",
        "myself",
        "yourself",
        "evie",
        "eve",
    }
)
# "what did X …" is a quote request even when ASR drops tell/say.
_PERSON_CONTENT = re.compile(
    r"\b(what(?:'d| did| has| have)|last (?:text|message|chat)|"
    r"show .{0,24}(?:messages|texts|chats|whatsapp))\b",
    re.IGNORECASE,
)
_QUOTE_FROM_PERSON = re.compile(
    r"\b(?:what(?:'d| did| has)|last (?:text|message|chat) (?:from|with))\s+"
    r"(?:my\s+)?([A-Za-z][A-Za-z.'-]{1,30})\b",
    re.IGNORECASE,
)
# I/we/you asking Evie about their own past — not "what did Maya tell me"
# and not a current "who is Maya" / "what do you know about me".
_OWNER_HISTORY_RE = re.compile(
    r"\b(?:what|which|where|why|when)\b.{0,48}\b(?:did|have|has|were|was|'d)\s+"
    r"(?:i|we|you)\b",
    re.IGNORECASE,
)
_OWNER_CHAT_CHANNEL = re.compile(
    r"\b(?:texted|messaged|whatsapp(?:ed)?|chatted|sms)\b",
    re.IGNORECASE,
)
# Spoken kinship must open people/chats even when the people-aisle cache is empty
# or ASR picks mommy instead of mummy. Groups are OR-matched, never AND-ed.
_KINSHIP_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"mummy", "mommy", "mom", "mum", "mother", "maa", "ammi"}),
    frozenset({"daddy", "dad", "papa", "pappa", "father", "baba"}),
)
_WEAK_TOKENS = frozenset(
    {
        "about",
        "any",
        "anything",
        "everything",
        "got",
        "had",
        "have",
        "been",
        "history",
        "know",
        "known",
        "last",
        "life",
        "list",
        "mine",
        "old",
        "own",
        "past",
        "please",
        "received",
        "recent",
        "remember",
        "remembered",
        "some",
        "something",
        "story",
        "say",
        "says",
        "said",
        "ask",
        "asked",
        "asking",
        "spoke",
        "speaking",
        "talk",
        "talked",
        "talking",
        "tell",
        "told",
        "was",
        "were",
        "did",
        "does",
        "how",
        "hows",
        "just",
    }
)


def strip_owner_ask(query: str) -> str:
    """Drop 'Hey Eve, can you…' so vocative tokens are not search keys."""

    text = (query or "").strip()
    if not text:
        return ""
    stripped = _LIVE_PREFIX.sub("", text, count=1).strip()
    stripped = _POLITE_ASK.sub("", stripped, count=1).strip()
    return stripped or text


def classify_shelf(query: str, *, people: list[str] | tuple[str, ...] | None = None) -> str | None:
    """Map a question to one archive drawer. None means do not open the archive."""
    query = strip_owner_ask(query)
    blob = (query or "").strip().lower()
    if not blob:
        return None
    from app.memory.visual import is_visual_recall_query

    # Her own captures are camera.observation, not the Photos takeout drawer.
    if is_visual_recall_query(query):
        return None
    if _SEND_NOW.search(query or ""):
        return None
    if _ACT_NOW.search(query or ""):
        return None
    if _FAMILIARITY.search(blob):
        return "familiarity"
    if is_chat_with_other_person(query or ""):
        return "chats"
    if _CURRENT_TALK.search(blob):
        return None
    if _CONVERSATION_AISLE.search(blob):
        return "people" if _WHO_I_TALK.search(blob) else "chats"
    # Mail before generic "message" so chat recall stays on the conversation shelf.
    if re.search(r"\b(e-?mail|gmail|inbox|mailbox)\b", blob) or re.search(
        r"\bmail(s|ed|ing)?\b", blob
    ):
        if "gmail" in blob or "email" in blob or "e-mail" in blob or "inbox" in blob:
            return "mail"
        if re.search(r"\b(mail subject|my mail|an email|the email)\b", blob):
            return "mail"
    for shelf, hints in _SHELF_HINTS:
        if any(hint in blob for hint in hints):
            return shelf
    if "calendar" in blob and re.search(
        r"\b(old|imported|takeout|icloud|was|were|used to|export|past)\b", blob
    ):
        return "calendar"
    if is_owner_history_query(query or ""):
        # Owner-memory questions stay off the WhatsApp drawer even when a
        # contact name is an English word in the utterance ("before", "will").
        if _refers_to_known_person(blob, people) and not _OWNER_CHAT_CHANNEL.search(blob):
            if _PERSON_ASK.search(blob) or re.search(r"\b(?:know|how's|how is)\b", blob):
                return "people"
        if _PERSON_ASK.search(blob) and _spoken_proper_name(query or ""):
            return "contacts"
        return None
    wants_quotes = bool(_CHAT_WITH_PERSON.search(blob) or _PERSON_CONTENT.search(blob))
    if _refers_to_known_person(blob, people):
        return "chats" if wants_quotes else "people"
    if _quote_from_other_person(query or ""):
        return "chats"
    if _PERSON_ASK.search(blob) and _spoken_proper_name(query or ""):
        return "contacts"
    return None


def _quote_from_other_person(query: str) -> bool:
    """True when they asked what someone else said — not what they told Evie."""
    match = _QUOTE_FROM_PERSON.search(query or "")
    if not match:
        return False
    speaker = re.sub(r"[^a-z]+", "", match.group(1).lower())
    return speaker not in {
        "i",
        "we",
        "you",
        "u",
        "it",
        "that",
        "this",
        "they",
        "he",
        "she",
    }


_GENERIC_CHAT_NAMES = frozenset(
    {
        "anybody",
        "anyone",
        "everybody",
        "everyone",
        "others",
        "people",
        "person",
        "somebody",
        "someone",
        "them",
    }
)


def _chat_person_query_token(query: str) -> str:
    """Spoken other-person name from a chat ask, or empty."""

    text = query or ""
    match = _CHAT_WITH_OTHER.search(text)
    name = ""
    if match:
        name = next((group for group in match.groups() if group), "")
    elif _quote_from_other_person(text):
        quoted = _QUOTE_FROM_PERSON.search(text)
        name = quoted.group(1) if quoted else ""
    token = re.sub(r"[^a-z]+", "", (name or "").lower())
    if len(token) < 2 or token in _SELF_ADDRESSEE or token in _STOP:
        return ""
    if token in _GENERIC_CHAT_NAMES:
        return ""
    return token


def is_chat_with_other_person(query: str) -> bool:
    """True when they asked about a WhatsApp/chat with someone else, not Evie."""

    text = query or ""
    if _quote_from_other_person(text):
        return True
    return bool(_chat_person_query_token(text))


def is_owner_history_query(query: str) -> bool:
    """True when they asked Evie about their own past with her, not a WhatsApp quote."""

    text = (query or "").strip()
    if not text:
        return False
    if _quote_from_other_person(text):
        return False
    if is_chat_with_other_person(text):
        return False
    if _FAMILIARITY.search(text):
        return False
    if _CONVERSATION_AISLE.search(text) or _CURRENT_TALK.search(text):
        return False
    if _OWNER_CHAT_CHANNEL.search(text):
        return False
    # "who is Maya" is a person card, not "what did I prefer before".
    if _PERSON_ASK.search(text) and _spoken_proper_name(text) and not _OWNER_HISTORY_RE.search(text):
        return False
    if re.search(r"\bdo you remember what i\b", text, re.IGNORECASE):
        return True
    if _OWNER_HISTORY_RE.search(text):
        return True
    from app.memory.state import classify_temporal_query

    mode = classify_temporal_query(text).mode
    if mode in {"solved", "leave_off", "still_open", "changes"}:
        return True
    # Bare "before" is too wide (any sentence). Only explicit original/used-to.
    return mode == "historical" and bool(
        re.search(
            r"\b(originally|at first|when we first|what used to|back then|at the time)\b",
            text,
            re.IGNORECASE,
        )
    )


def life_shelf_for_memory_search(query: str, shelf: str | None) -> str | None:
    """Chats/people drawers must not suppress owner-memory questions."""

    if not shelf:
        return None
    text = query or ""
    if _quote_from_other_person(text):
        return shelf
    if is_chat_with_other_person(text):
        return shelf
    if _CONVERSATION_AISLE.search(text) and not _CURRENT_TALK.search(text):
        return shelf
    if is_owner_history_query(text) and shelf in {"chats", "people", "contacts"}:
        return None
    return shelf


def _spoken_proper_name(query: str) -> bool:
    """True when the utterance names a person, not just a capitalized question word."""
    for match in _PROPER_NAME.finditer(query or ""):
        token = match.group(1).split()[0].lower()
        if token not in _QUESTION_START:
            return True
    return False


def token_variants(token: str) -> frozenset[str]:
    """Kinship synonyms for one spoken token. Unknown words stay a singleton."""
    key = re.sub(r"[^a-z0-9]+", "", (token or "").strip().lower())
    if not key:
        return frozenset()
    for group in _KINSHIP_GROUPS:
        if key in group:
            return group
    return frozenset({key})


def name_lookup_keys(name: str) -> list[str]:
    """Lookup spellings for a person query, including kinship aliases."""
    raw = str(name or "").strip()
    if not raw:
        return []
    keys: list[str] = []
    seen: set[str] = set()
    for candidate in (raw, raw.lower(), *sorted(token_variants(raw))):
        token = str(candidate or "").strip()
        key = token.lower()
        if len(token) < 2 or key in seen:
            continue
        seen.add(key)
        keys.append(token)
    return keys


def _kinship_mentioned(blob: str, tokens: set[str] | None = None) -> bool:
    words = tokens if tokens is not None else simple_tokens(blob)
    cleaned = {re.sub(r"[^a-z0-9]+", "", word) for word in words}
    for group in _KINSHIP_GROUPS:
        for word in group:
            if word in words or word in cleaned:
                return True
            if re.search(rf"\b{re.escape(word)}\b", blob):
                return True
    return False


def _variant_in_blob(variant: str, blob: str) -> bool:
    if len(variant) <= 3:
        return bool(re.search(rf"\b{re.escape(variant)}\b", blob))
    return variant in blob


def _refers_to_known_person(
    blob: str, people: list[str] | tuple[str, ...] | None
) -> bool:
    """True for kinship terms or a name on the people aisle — cache optional."""
    tokens = simple_tokens(blob)
    if _kinship_mentioned(blob, tokens):
        return True
    return _named_person(blob, people, tokens=tokens)


def _named_person(
    blob: str,
    people: list[str] | tuple[str, ...] | None,
    *,
    tokens: set[str] | None = None,
) -> bool:
    if not people:
        return False
    words = tokens if tokens is not None else simple_tokens(blob)
    cleaned = {re.sub(r"[^a-z0-9]+", "", word) for word in words}
    for name in people:
        key = str(name or "").strip().lower()
        if len(key) < 3 or key in _STOP:
            continue
        variants = token_variants(key) | {key}
        first = re.sub(r"[^a-z0-9]+", "", key.split()[0])
        if len(first) >= 3:
            variants = variants | token_variants(first) | {first}
        for variant in variants:
            if len(variant) < 3 or variant in _STOP:
                continue
            if variant in words or variant in cleaned:
                return True
            if re.search(rf"\b{re.escape(variant)}\b", blob):
                return True
    return False


async def resolve_shelf(session: AsyncSession, query: str) -> str | None:
    """Classify using the tiny people-aisle sign, not the whole event table."""
    names = await people_aisle_names(session)
    return classify_shelf(query, people=names)


_PEOPLE_CACHE: tuple[float, tuple[str, ...]] | None = None


async def people_aisle_names(session: AsyncSession) -> tuple[str, ...]:
    """Names on the people aisle sign. Small on purpose — not the 2k vCard list."""
    global _PEOPLE_CACHE
    import time as _time

    now = _time.monotonic()
    if _PEOPLE_CACHE is not None and now - _PEOPLE_CACHE[0] < 30:
        return _PEOPLE_CACHE[1]
    rows = (
        await session.execute(
            select(Event.content).where(
                Event.source == SOURCE,
                Event.event_type == "life.person",
                Event.tombstoned_at.is_(None),
                Event.privacy_level != "never_send_to_model",
            ).limit(200)
        )
    ).scalars().all()
    names: list[str] = []
    seen: set[str] = set()
    for content in rows:
        payload = content if isinstance(content, dict) else {}
        for raw in (payload.get("name"), payload.get("title"), *(payload.get("aliases") or [])):
            token = str(raw or "").strip()
            key = token.lower()
            if len(token) < 3 or key in seen or key in _STOP:
                continue
            seen.add(key)
            names.append(token)
    packed = tuple(names)
    _PEOPLE_CACHE = (now, packed)
    return packed


def reset_people_cache() -> None:
    global _PEOPLE_CACHE
    _PEOPLE_CACHE = None


def _person_is_close(payload: dict[str, Any]) -> bool:
    tokens = {
        str(payload.get("name") or "").strip().lower(),
        *(str(alias).strip().lower() for alias in (payload.get("aliases") or [])),
    }
    tokens.discard("")
    kinship = set().union(*_KINSHIP_GROUPS)
    if tokens & kinship:
        return True
    return str(payload.get("relation") or "").strip().lower() in {"parent", "family"}


async def familiarity_sign(session: AsyncSession) -> str:
    """Tiny people + owner-voice sign for session bootstrap. Not chat logs."""
    people_events = list(
        (
            await session.execute(
                select(Event)
                .where(
                    Event.source == SOURCE,
                    Event.event_type == "life.person",
                    Event.tombstoned_at.is_(None),
                    Event.privacy_level != "never_send_to_model",
                )
                .order_by(Event.occurred_at.desc())
                .limit(48)
            )
        ).scalars().all()
    )
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for event in people_events:
        payload = event.content if isinstance(event.content, dict) else {}
        name = str(payload.get("name") or "").strip()
        relation = str(payload.get("relation") or "").strip()
        key = name.lower()
        if len(name) < 2 or key in seen:
            continue
        seen.add(key)
        label = f"{name} ({relation})" if relation else name
        ranked.append((0 if _person_is_close(payload) else 1, label))
    ranked.sort(key=lambda item: item[0])
    names = [label for _rank, label in ranked[:8]]
    voice_events = list(
        (
            await session.execute(
                select(Event)
                .where(
                    Event.source == SOURCE,
                    Event.event_type == "life.owner.voice",
                    Event.tombstoned_at.is_(None),
                    Event.privacy_level != "never_send_to_model",
                )
                .order_by(Event.occurred_at.desc())
                .limit(3)
            )
        ).scalars().all()
    )
    voices: list[str] = []
    for event in voice_events:
        text = _safe_text(event)
        if text:
            voices.append(text.split(".")[0][:120].rstrip())
    parts: list[str] = []
    if names:
        parts.append("Close people already known: " + "; ".join(names) + ".")
    if voices:
        parts.append("How they write: " + "; ".join(voices) + ".")
    if names or voices:
        parts.append(
            "Shared life shelves already exist. This is not a first meeting; "
            "that history is not new to you."
        )
    return " ".join(parts)


def locate_tokens(query: str) -> list[str]:
    tokens = []
    for token in simple_tokens(strip_owner_ask(query)):
        token = re.sub(r"'s$", "", token).replace("'", "")
        if token not in _STOP and len(token) >= 3 and token not in SHELF_TYPES:
            tokens.append(token)
    # Keep distinctive content words; drop the shelf label itself.
    shelf_words = {
        "contact",
        "contacts",
        "email",
        "gmail",
        "mail",
        "photo",
        "photos",
        "picture",
        "pictures",
        "album",
        "calendar",
        "schedule",
        "task",
        "tasks",
        "todo",
        "note",
        "notes",
        "bookmark",
        "bookmarks",
        "whatsapp",
        "person",
        "people",
        "thread",
        "chat",
        "chats",
        "conversation",
        "conversations",
        "familiarity",
        "icloud",
        "drive",
        "google",
        "apple",
        "takeout",
        "health",
        "snapshot",
        "vitals",
    }
    return [token for token in tokens if token not in shelf_words][:8]


async def locate_archive(
    session: AsyncSession,
    query: str,
    *,
    shelf: str | None = None,
    k: int = MAX_HITS,
) -> list[dict[str, Any]]:
    """Return a tiny evidence pack from one shelf. Empty if no shelf or no hits."""
    chosen = shelf or classify_shelf(query, people=await people_aisle_names(session))
    if chosen is None or chosen not in SHELF_TYPES:
        return []
    types = SHELF_TYPES[chosen]
    tokens = locate_tokens(query)
    distinctive = [token for token in tokens if token not in _WEAK_TOKENS]
    if chosen == "familiarity":
        # The aisle is the answer. Do not AND "remember"/"know" against cards.
        distinctive = []
    elif chosen in {"chats", "people"}:
        distinctive = [
            token
            for token in distinctive
            if token
            not in {
                "anyone",
                "anybody",
                "been",
                "chat",
                "chats",
                "conversation",
                "conversations",
                "different",
                "everybody",
                "had",
                "everyone",
                "family",
                "friends",
                "message",
                "messages",
                "others",
                "people",
                "person",
                "someone",
                "somebody",
                "text",
                "texts",
                "time",
                "times",
                "various",
                "whatsapp",
            }
        ]
    person_token = _chat_person_query_token(query)
    if chosen == "chats" and person_token:
        # "last time" must not AND with the name — cards do not say "time".
        distinctive = [person_token]
    if chosen == "chats" and not distinctive:
        types = ("life.chat.thread",)
    limit = max(1, min(k, MAX_HITS))
    stmt = (
        select(Event)
        .where(
            Event.source == SOURCE,
            Event.event_type.in_(types),
            Event.tombstoned_at.is_(None),
            Event.privacy_level != "never_send_to_model",
        )
        .order_by(Event.occurred_at.desc())
        .limit(SCAN_CAP if distinctive else limit)
    )
    year = _year_from_query(query)
    if chosen == "photos" and year is not None:
        stmt = stmt.where(extract("year", Event.occurred_at) == year)
    match = _content_match(distinctive)
    if match is not None:
        stmt = stmt.where(match)
    rows = list((await session.execute(stmt)).scalars().all())
    scored: list[tuple[float, Event, str]] = []
    for event in rows:
        text = _safe_text(event)
        if not text:
            continue
        score = _score(distinctive, text, event)
        if distinctive and score <= 0:
            continue
        scored.append((score, event, text))
    if distinctive:
        named_chat = bool(person_token) and chosen == "chats"
        scored.sort(
            key=lambda item: (
                item[0],
                1 if named_chat and item[1].event_type == "life.chat.excerpt" else 0,
                (item[1].occurred_at or utcnow()).timestamp(),
            ),
            reverse=True,
        )
    else:
        scored.sort(key=lambda item: item[1].occurred_at or utcnow(), reverse=True)
    hits: list[dict[str, Any]] = []
    if not (distinctive and not scored):
        for score, event, text in scored[:limit]:
            hits.append(
                {
                    "id": str(event.id),
                    "source": SOURCE,
                    "when": event.occurred_at.isoformat() if event.occurred_at else None,
                    "text": text[:400],
                    "kind": "life",
                    "memory_type": event.event_type,
                    "confidence": "archive_locator",
                    "score": round(max(score, 0.15), 4),
                    "provenance": [str(event.id)],
                    "shelf": chosen,
                }
            )
    from app.memory.live_life import locate_live_life, merge_life_hits

    live_hits = await locate_live_life(
        session, query, shelf=chosen, tokens=distinctive, k=limit
    )
    merged = merge_life_hits(live_hits, hits, limit=limit)
    if distinctive and not merged:
        return []
    return merged


async def rebuild_locator(session: AsyncSession) -> dict[str, Any]:
    """One pass over archive events: write shelf counts. Not model context."""
    from sqlalchemy import func

    rows = (
        await session.execute(
            select(Event.event_type, func.count())
            .where(
                Event.source == SOURCE,
                Event.tombstoned_at.is_(None),
            )
            .group_by(Event.event_type)
        )
    ).all()
    by_type = {str(event_type): int(count) for event_type, count in rows}
    shelves = {
        shelf: int(sum(by_type.get(event_type, 0) for event_type in types))
        for shelf, types in SHELF_TYPES.items()
    }
    reset_people_cache()
    names = await people_aisle_names(session)
    photo_years = await _photo_year_counts(session)
    payload = {
        "schema_version": 1,
        "updated_at": utcnow().isoformat(),
        "source": SOURCE,
        "max_hits": MAX_HITS,
        "by_type": by_type,
        "shelves": shelves,
        "photo_years": photo_years,
        "total": sum(by_type.values()),
        "aisles": [
            {
                "id": shelf,
                "header": AISLE_HEADERS.get(shelf, shelf),
                "count": int(count),
            }
            for shelf, count in shelves.items()
        ],
        "people": [{"name": name, "header": f"Person aisle: {name}."} for name in names],
    }
    root = ensure_tree()
    atomic_write_json(root / "catalog" / "locator" / "shelves.json", payload)
    atomic_write_json(
        root / "catalog" / "locator" / "aisles.json",
        {
            "schema_version": 1,
            "updated_at": payload["updated_at"],
            "max_hits": MAX_HITS,
            "aisles": payload["aisles"],
            "people": payload["people"],
            "photo_years": payload["photo_years"],
        },
    )
    return payload


def _safe_text(event: Event) -> str:
    """Model-facing text only. Never paths, phones, or raw blobs."""
    content = event.content or {}
    text = str(content.get("text") or "").strip()
    return text


def _score(tokens: list[str], text: str, event: Event) -> float:
    if not tokens:
        return 0.15
    lowered = text.lower()
    extra = " ".join(
        str((event.content or {}).get(key) or "")
        for key in ("title", "album", "summary", "name", "subject", "aliases")
    ).lower()
    blob = f"{lowered} {extra}"
    hits = sum(1 for token in tokens if _token_hits(token, blob))
    if hits == 0:
        return 0.0
    if any(len(token) >= 5 for token in tokens) and hits < len(tokens):
        return 0.0
    return hits / len(tokens)


def _token_hits(token: str, blob: str) -> bool:
    """A query token hits if it or a kinship synonym appears in the card."""
    return any(_variant_in_blob(variant, blob) for variant in token_variants(token) if variant)


def _year_from_query(query: str) -> int | None:
    match = _YEAR.search(query or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _sql_match_tokens(tokens: list[str]) -> list[str]:
    """SQL tokens: prefer longer kinship synonyms so `mum` does not match `maximum`."""
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens[:6]:
        group = [variant for variant in token_variants(token) if variant]
        long = [variant for variant in group if len(variant) >= 4]
        chosen = long or [token]
        for variant in chosen:
            safe = re.sub(r"[^a-z0-9]+", "", variant.lower())
            if len(safe) < 3 or safe in seen or safe in _STOP:
                continue
            seen.add(safe)
            out.append(safe)
    return out[:8]


def _content_match(tokens: list[str]):
    """SQL short-path: only rows whose JSON blob mentions a distinctive token."""
    clauses = []
    for safe in _sql_match_tokens(tokens):
        clauses.append(cast(Event.content, String).ilike(f"%{safe}%"))
    if not clauses:
        return None
    return or_(*clauses)


async def _photo_year_counts(session: AsyncSession) -> dict[str, int]:
    from sqlalchemy import func

    rows = (
        await session.execute(
            select(extract("year", Event.occurred_at), func.count())
            .where(
                Event.source == SOURCE,
                Event.event_type == "life.photo.index",
                Event.tombstoned_at.is_(None),
                Event.occurred_at.is_not(None),
            )
            .group_by(extract("year", Event.occurred_at))
        )
    ).all()
    counts: dict[str, int] = {}
    for year, count in rows:
        if year is None:
            continue
        counts[str(int(year))] = int(count)
    return dict(sorted(counts.items()))
