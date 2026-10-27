"""Deterministic owner-message classification (spec §6, §4).

High precision by design: anything unclear counts as neutral. No ML, no
network, no randomness.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

POSITIVE = "positive"
NEUTRAL = "neutral"
NEGATIVE = "negative"

_LAUGH_TOKENS = frozenset(
    {"haha", "hahaha", "hahahaha", "lol", "lmao", "rofl", "hehe", "heh"}
)
_LAUGH_EMOJI = frozenset({"😂", "🤣", "😆", "😹"})

# Ambiguous emoji: only laughing when a laugh token, banter cue, or short
# positive word is present too — and never in a distress context.
_CONTEXT_EMOJI = frozenset({"💀", "😭"})
_DISTRESS_RES = (
    re.compile(r"\btired\b"), re.compile(r"\bexhausted\b"),
    re.compile(r"\bsad\b"), re.compile(r"\bcry(ing)?\b"),
    re.compile(r"\bstressed\b"), re.compile(r"\boverwhelm(ed|ing)?\b"),
    re.compile(r"\banxious\b"), re.compile(r"\bworried\b"),
    re.compile(r"\bupset\b"), re.compile(r"\bbad day\b"),
    re.compile(r"\bdepress(ed|ing|ion)?\b"), re.compile(r"\bhurt(s|ing)?\b"),
    re.compile(r"\bpain\b"), re.compile(r"\bgrie(f|ving)\b"),
    re.compile(r"\balone\b"), re.compile(r"\bcan'?t sleep\b"),
    re.compile(r"\binsomnia\b"),
)

# Banter-back: the owner plays along or teases back (positive).
_BANTER_BACK_RES = (
    re.compile(r"\bgood one\b"),
    re.compile(r"\bnice one\b"),
    re.compile(r"\bsays you\b"),
    re.compile(r"\bsays the\b"),
    re.compile(r"\boh please\b"),
    re.compile(r"\blook who'?s talking\b"),
    re.compile(r"\bnice try\b"),
    re.compile(r"\byou got me\b"),
    re.compile(r"\btouch[eé]\b"),
    re.compile(r"\bfight me\b"),
    re.compile(r"\bok that was (good|funny)\b"),
    re.compile(r"\bthat was (good|funny)\b"),
    re.compile(r"\bi'?m (dead|dying|gone)\b"),
    re.compile(r"\bbro\s*😭"),
    re.compile(r"\bkya baat\b(?!\s*kar)"),
    re.compile(r"\bkilled me\b"),
    re.compile(r"\bkilling me\b"),
    re.compile(r"\bkidding\b"),
    re.compile(r"(?:^|\s)/s(?:\s|$|[.!])"),
    re.compile(r"\bjk\b"),
)

# Short-message positives: only when the whole message is terse (<=3 words),
# so "fair" inside a long complaint does not count as playing along.
_SHORT_POSITIVE = frozenset(
    {"fair", "bet", "touche", "touché", "lol", "lmao", "haha", "hehe", "deal",
     "dead", "sahi", "mast", "ekdum"}
)

# Negative: shut it down, redirect, or answer with annoyance.
# Bare "stop"/"enough"/"whatever"/"focus" only count in terse messages
# (<=4 words); inside longer text they are usually ordinary words
# ("stop by the store", "enough time", "focus on the API").
_NEGATIVE_RES = (
    re.compile(r"\bnot now\b"),
    re.compile(r"\bnot funny\b"),
    re.compile(r"\b(haha|lol|hehe|ha)\s+(very funny|so funny|sure)\b"),
    re.compile(r"\bjust answer\b"),
    re.compile(r"\bbas (kar|karo|bhai)\b"),
    re.compile(r"^bas[.!?\s]*$"),
    re.compile(r"\bchup\b"),
    re.compile(r"\bseriously[?!.,\s]*$"),
    re.compile(r"\bshut up\b"),
    re.compile(r"\bstop (it|that|joking|kidding|talking)\b"),
    re.compile(r"\bjust (do|send|fix|run|build|show|tell|give) (it|that|me)\b"),
    re.compile(r"\bnever mind\b"),
    re.compile(r"\bdrop it\b"),
    re.compile(r"\bcan (it|this) wait\b"),
    re.compile(r"\bi'?m (not in the mood|serious)\b"),
    re.compile(r"\bthis isn'?t funny\b"),
    re.compile(r"\btoo soon\b"),
    re.compile(r"\bread the room\b"),
    re.compile(r"\bthat'?s enough\b"),
    re.compile(r"\benough already\b"),
    re.compile(r"\bhad enough\b"),
)

_SHORT_NEGATIVE = frozenset({"stop", "enough", "whatever", "focus"})

# Phrases that rescue a terse message from the short-negative rule: the
# words look like a shutdown but the phrase is ordinary ("stop by the
# store", "fair enough, continue"). Explicit shutdown regexes above still
# win; this guard only covers the bare-word path.
_SHORT_GUARD_RES = (
    re.compile(r"\bstop by\b"),
    re.compile(r"\bfair enough\b"),
)

# "Roast me anyway" override (spec §4 rules 2–3, 6): the owner explicitly
# asks to be roasted. "hit me" is excluded: too ambiguous ("hit me up").
_ROAST_ME_ANYWAY_RES = (
    re.compile(r"\broast me\b"),
    re.compile(r"\broast (me|this|it|that)\b"),
    re.compile(r"\blay it on me\b"),
    re.compile(r"\bi can take it\b"),
    re.compile(r"\bdon'?t hold back\b"),
    re.compile(r"\blet me have it\b"),
    re.compile(r"\bgive me your worst\b"),
    re.compile(r"\bgive it to me\b"),
    re.compile(r"\bgo (on|ahead)[,. ]{0,3}roast\b"),
)

# Roman-script Hinglish markers (used only as a group; see hinglish()).
_HINGLISH_MARKERS = frozenset(
    {
        "bhai", "yaar", "arre", "kya", "hai", "nahi", "nahin", "raha", "rahe",
        "rahi", "karo", "batao", "dekho", "suno", "achha", "acha", "theek",
        "sahi", "galat", "bahut", "bohot", "zyada", "kam", "abhi", "kal",
        "aaj", "yeh", "woh", "mera", "meri", "tere", "tum", "tumhe", "aap",
        "mujhe", "hum", "kaise", "kyun", "kyunki", "lekin", "agar", "bhi",
        "chal", "chalo", "wah", "matlab", "scene", "jugaad", "pakka",
    }
)
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097f]")

# Coarse topic buckets for learning weights (spec §6 per-topic weights).
_TOPIC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("code", ("code", "bug", "test", "deploy", "error", "refactor", "commit",
              "merge", "branch", "server", "api", "function", "class", "debug",
              "stack", "trace", "build", "lint", "ci", "pr ")),
    ("habit", ("gym", "sleep", "diet", "habit", "routine", "wake", "exercise",
               "run", "walk", "meditat", "read", "journal")),
    ("opinion", ("should i", "vs ", "versus", "better", "microservice",
                 "monolith", "framework", "startup", "company", "product",
                 "phone", "laptop", "buy", "worth it")),
    ("sleep", ("tired", "sleep", "nap", "bed", "awake", "insomnia", "rest")),
)

_WORD_RE = re.compile(r"[a-z0-9'\u0900-\u097f]+")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _has_distress(text: str) -> bool:
    lowered = text.lower()
    return any(pattern.search(lowered) for pattern in _DISTRESS_RES)


def _has_laugh(text: str) -> bool:
    lowered = text.lower()
    if any(emoji in text for emoji in _LAUGH_EMOJI):
        return True
    words = _words(lowered)
    if any(token in words for token in _LAUGH_TOKENS):
        return True
    if any(emoji in text for emoji in _CONTEXT_EMOJI):
        if _has_distress(text):
            return False
        if "💀" in text and len(words) <= 2:
            return True
        if _has_banter_back(text):
            return True
        return any(word in _SHORT_POSITIVE for word in words)
    return False


def _has_banter_back(text: str) -> bool:
    lowered = text.lower()
    return any(pattern.search(lowered) for pattern in _BANTER_BACK_RES)


def _has_negative(text: str) -> bool:
    lowered = text.lower()
    if any(pattern.search(lowered) for pattern in _NEGATIVE_RES):
        return True
    # Bare short words ("lol stop") bow to laugh/banter markers: with no
    # explicit shutdown phrase, a laughing message is playing along.
    if _has_laugh(text) or _has_banter_back(text):
        return False
    if any(pattern.search(lowered) for pattern in _SHORT_GUARD_RES):
        return False
    words = _words(lowered)
    return bool(words) and len(words) <= 4 and any(
        word in _SHORT_NEGATIVE for word in words
    )


def _is_terse_repeat(text: str, prev_meta: Any) -> bool:
    """A terse repeat of the owner's own request (annoyance signal).

    Needs the earlier request text; without it there is nothing to compare
    and the check abstains (neutral).
    """
    request: str | None = None
    if isinstance(prev_meta, Mapping):
        raw = prev_meta.get("owner_request")
        if isinstance(raw, str) and raw.strip():
            request = raw
    if not request:
        return False
    words = [word for word in _words(text) if len(word) > 3]
    if not words or len(_words(text)) > 8:
        return False
    request_words = {word for word in _words(request) if len(word) > 3}
    shared = sum(1 for word in words if word in request_words)
    return shared >= 2 and not _has_laugh(text) and not _has_banter_back(text)


def classify_reaction(owner_msg: str | None, prev_meta: Any = None) -> str:
    """Classify the owner's reaction: positive | neutral | negative.

    Negative wins over positive when both match (a shutdown with a laugh
    in it is still a shutdown). Empty or unclear input is neutral.
    """
    text = (owner_msg or "").strip()
    if not text:
        return NEUTRAL
    if _has_negative(text) or _is_terse_repeat(text, prev_meta):
        return NEGATIVE
    if _has_laugh(text) or _has_banter_back(text):
        return POSITIVE
    words = _words(text)
    if words and len(words) <= 3 and all(word in _SHORT_POSITIVE for word in words):
        return POSITIVE
    return NEUTRAL


def roast_me_anyway(msg: str | None) -> bool:
    """True when the owner explicitly asks to be roasted (spec §4)."""
    lowered = (msg or "").lower()
    if not lowered.strip():
        return False
    return any(pattern.search(lowered) for pattern in _ROAST_ME_ANYWAY_RES)


def banter_invited(msg: str | None) -> bool:
    """True when the owner jokes or teases first (spec §4 rule 6)."""
    text = (msg or "").strip()
    if not text:
        return False
    return bool(_has_laugh(text) or _has_banter_back(text) or roast_me_anyway(text))


def hinglish(msg: str | None) -> bool:
    """True when the owner writes Hinglish (Devanagari or roman markers).

    Roman detection needs 2+ distinct markers with at least one of length
    3+, so English ("hi", "na") never trips it.
    """
    text = (msg or "").strip()
    if not text:
        return False
    if _DEVANAGARI_RE.search(text):
        return True
    hits = {word for word in _words(text) if word in _HINGLISH_MARKERS}
    return len(hits) >= 2 and any(len(word) >= 3 for word in hits)


def topic_of(text: str | None) -> str:
    """Coarse topic bucket for learning weights. Always returns a bucket."""
    lowered = f" {(text or '').lower()} "
    for topic, keywords in _TOPIC_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return topic
    return "general"
