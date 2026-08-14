"""Fresh-question detection: keep old-thread context out of new topics.

EV runs one lifelong conversation thread. If every request inherited the full
rolling summary, prior turns, and open questions, a brand-new question (e.g.
"what's the weather in Gujarat?") would be answered as if it continued the old
topic — producing confused clarifying replies like "do you mean the market
thread, or the weather?".

Continuation is decided deterministically from explicit reference phrases and
anaphora: a message that names its own topic never inherits old context unless
the owner explicitly points back at it ("as I said before", "about the
markets", "that thing"). This is intentionally conservative — false
"continuation" only costs a little extra context, while false "fresh" would
leak an old topic into a new answer.
"""

from __future__ import annotations

import re

# Phrases that unambiguously point back at earlier turns. Any match marks the
# message as a continuation that should keep the full context window.
CONTINUATION_PHRASES = re.compile(
    r"("
    r"\b(as |like |just )?(i|we|you) (said|mentioned|asked|discussed|told|"
    r"wanted|were saying|were talking|were discussing|wrote|asked for)\b"
    r"|"
    r"\b(about that|that thing|the same(?: thing)?|that one|"
    r"that (?:market|thread|conversation|topic|question|answer)|"
    r"back to|going back|follow[- ]?up)\b"
    r"|"
    r"\b(earlier|before|last time|previously|prior|above|a minute ago|"
    r"the other day|just now)\b"
    r"|"
    r"\b(you (?:just )?(?:said|told|mentioned|asked))\b"
    r"|"
    r"\b(and what about|what about (?:it|that)|and also|me too|instead|rather|"
    r"what else|anything else|any other)\b"
    r"|"
    r"\b(continue|resume|pick up|carry on|where were we|what was i doing)\b"
    r"|"
    r"\b(and then|then what|what next|what now|make it|change it|same as)\b"
    r")",
    re.IGNORECASE,
)

# Third-person / possessive anaphora that usually needs a referent from earlier
# turns ("its answer", "their plan"). Bare "it" is deliberately excluded: dummy
# subjects like "is it raining in Gujarat?" are self-contained.
ANAPHORA = re.compile(
    r"\b(he|she|him|her|his|hers|its|their|theirs|them|they)\b",
    re.IGNORECASE,
)

# Demonstratives read as "that thing from before" when the message is short.
DEMONSTRATIVE = re.compile(r"\b(that|this|these|those)\b", re.IGNORECASE)
FOLLOW_UP_PREFIX = re.compile(
    r"^(?:and|also|then|plus|instead|make it|change it)\b",
    re.IGNORECASE,
)

MAX_FRAGMENT_CHARS = 60


def _is_fragment(message: str) -> bool:
    """A short, mostly-pronominal utterance is a follow-up, not a new topic.

    "which 8?", "and the audio?", "what about that?" only make sense against
    history, so keep the context window for them instead of treating them as
    fresh questions.
    """
    text = message.strip()
    if not text:
        return False
    if len(text) > MAX_FRAGMENT_CHARS:
        return False
    return (
        bool(DEMONSTRATIVE.search(text))
        or bool(ANAPHORA.search(text))
        or bool(FOLLOW_UP_PREFIX.search(text))
    )


def is_continuation(message: str | None) -> bool:
    """True when the message points back at earlier turns and needs that context."""
    text = (message or "").strip()
    if not text:
        return False
    if CONTINUATION_PHRASES.search(text):
        return True
    return _is_fragment(text)


def is_self_contained(message: str | None) -> bool:
    """True when the message names a fresh topic and should not inherit context."""
    return not is_continuation(message)
