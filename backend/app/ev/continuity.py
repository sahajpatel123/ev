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
from typing import Literal

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

MemoryIntent = Literal[
    "continuation",
    "explicit_recall",
    "forget",
    "pin",
    "fresh",
]

EXPLICIT_RECALL = re.compile(
    r"("
    r"\b(what did (we|i|you) (talk|speak|discuss|say)|"
    r"what have we (been )?(talking|discussing|working)|"
    r"what were we (talking|discussing|working)|"
    r"what have we discussed|"
    r"do you remember|"
    r"remind me (?:what|about|when|of)|"
    r"what was i working on|"
    r"what (else )?were we talking about|"
    r"what have we talked about|"
    r"what did i (?:originally |first |actually )?(tell|say|call|name|give)|"
    r"what (name|model|feature|experiment|project) did i|"
    r"why did i (decide|prefer|choose)|"
    r"which (one )?(do|did) i prefer|"
    r"what did i prefer before|"
    r"when did i (first )?(mention|say|tell)|"
    r"what (decisions|preference) did we|"
    r"what(?:'s| is) (?:it|that|this) called|"
    r"what was that (experiment|project|thing|feature|name)|"
    r"what(?:'s| is) (?:the )?(?:current |original )?name|"
    r"where did we leave off|where were we|"
    r"what(?:'s| is) still (?:open|unresolved|stuck|broken|left)|"
    r"what (?:are we|were we) still (?:stuck on|working on)|"
    r"what haven'?t we finished|what was left|"
    r"what did we (?:solve|fix|finish)|what (?:got|have we) (?:fixed|solved|resolved)|"
    r"what (?:issue|problem) did we (?:solve|fix)|"
    r"what changed|how did .{0,40} change|"
    r"what did we (?:think|believe) (?:before|originally)|"
    r"what editor (?:do|did) i|what did i use before|"
    r"what should we (?:work on|do) next)\b"
    r"|"
    r"\b(what did you see|what you (?:just )?saw|"
    r"what was i wearing|was i wearing|saw me with|"
    r"when was the last time you (?:saw|looked)|last time you saw|"
    r"clip of me|photo of me|"
    r"that (?:photo|picture|pic|clip|video|recording)|"
    r"(?:photo|picture|clip|video) you (?:took|recorded|captured)|"
    r"what did i (?:just )?ask you to (?:remember|memorise|memorize|keep)|"
    r"what was i showing|"
    r"did you (?:already |ever )?(?:memorise|memorize|remember)|"
    r"have you (?:already |ever )?(?:memorised|memorized|remembered))\b"
    r"|"
    r"\bover the last (few )?(days|weeks)\b"
    r")",
    re.IGNORECASE,
)
FORGET_INTENT = re.compile(
    r"\b(forget (that|this|it)|don't remember that|do not remember that|"
    r"that was wrong|never mind that memory)\b",
    re.IGNORECASE,
)
PIN_INTENT = re.compile(
    r"("
    r"\b(remember this|don't forget (that|this)|do not forget|"
    r"this is important|from now on|"
    r"memorise|memorize)\b"
    r"|"
    r"(?:^|[.!?]\s+)(?:please )?remember that\b"
    r")",
    re.IGNORECASE,
)
HYPOTHETICAL = re.compile(
    r"\b(imagine (if |that |i )|what if |hypothetically|suppose (i|we)|"
    r"in a hypothetical)\b",
    re.IGNORECASE,
)
REFERENT_TOPIC = re.compile(
    r"\b(that|this|the same)\s+(?:\w+\s+){0,3}"
    r"(memory|camera|orb|project|feature|idea|plan|bug|issue|thing|one|"
    r"architecture|design|animation|model|voice|look|system|experiment)\b",
    re.IGNORECASE,
)
HISTORICAL_TRUTH = re.compile(
    r"\b(before|used to|previously|did i prefer before|"
    r"what did i prefer before|didn't i used to|used to use|"
    r"originally|at first|the original|first called)\b",
    re.IGNORECASE,
)
CONVERSATION_TIME = re.compile(
    r"\b(yesterday|today|last night|this week|last week|last month|"
    r"last year|last (monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"over the last (few )?(days|weeks)|in the (last|past)\s+\d+\s+(days|weeks))\b",
    re.IGNORECASE,
)


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
    if REFERENT_TOPIC.search(text):
        return True
    return _is_fragment(text)


def is_hypothetical(message: str | None) -> bool:
    return bool(HYPOTHETICAL.search((message or "").strip()))


def wants_historical_truth(message: str | None) -> bool:
    """True when the owner asked for a past truth, not the current one."""
    return bool(HISTORICAL_TRUTH.search((message or "").strip()))


def conversation_time_requested(message: str | None) -> bool:
    """True when the owner asked about when a conversation happened."""
    return bool(CONVERSATION_TIME.search((message or "").strip()))


def classify_memory_intent(message: str | None) -> MemoryIntent:
    """Map an utterance onto continuation / recall / fresh (anti-intrusion)."""

    text = (message or "").strip()
    if not text:
        return "fresh"
    if FORGET_INTENT.search(text):
        return "forget"
    from app.memory.visual import is_keep_recall_query, is_visual_recall_query

    if is_keep_recall_query(text) or is_visual_recall_query(text):
        return "explicit_recall"
    if PIN_INTENT.search(text):
        return "pin"
    if EXPLICIT_RECALL.search(text):
        return "explicit_recall"
    from app.memory.life_archive.locate import classify_shelf

    if classify_shelf(text) is not None:
        return "explicit_recall"
    if is_continuation(text):
        return "continuation"
    return "fresh"


def is_self_contained(message: str | None) -> bool:
    """True when the message names a fresh topic and should not inherit context."""
    return not is_continuation(message)
