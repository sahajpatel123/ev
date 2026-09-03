"""F1 retrieval-intent classification — deterministic first, always.

Classification order (F0+F1 directive §7):
  1. deterministic rule patterns (this module, P95 < 10ms)
  2. existing contextual signals / entity resolution (continuity helpers)
  3. Luna ONLY if genuinely ambiguous — provided as a disabled seam
     (``EV_MEMORY_INTENT_LUNA``), never called per-turn by default.

The CURRENT_STATE_QUERY guard implements §8: questions about what IS true now
(priority, meetings, reminders, active goals) route to canonical authority and
receive no historical shadow context unless the owner asked for historical
truth ("what WAS the priority originally?").
"""

from __future__ import annotations

import re
import time
from typing import Any

from app.ev.continuity import (
    classify_memory_intent,
    is_continuation,
    wants_historical_truth,
)
from app.memory.foundation import RetrievalClassification, RetrievalIntent

# --- deterministic patterns -------------------------------------------------
# Historical-truth markers flip current-state questions into history lookups.
HISTORICAL_MARKERS = re.compile(
    r"\b(originally|used to|previously|before (?:that|we|i)|back then|"
    r"at (?:the )?(?:time|start|beginning)|in (?:20\d\d|\d{4})|"
    r"last (?:week|month|year|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)|earlier (?:this|last) (?:week|month|year)|"
    r"(?:what|which).{0,32}\b(?:was|were|did)\b.{0,32}\b(?:before|used to))\b",
    re.IGNORECASE,
)

CURRENT_STATE_PATTERNS = re.compile(
    r"\b(?:"
    r"what(?:'s| is) (?:the |my |our )?(?:current |active |present )?"
    r"(?:priority|priorities|status|state)\b"
    r"|what(?:'s| is) (?:the |my |our )?[a-z][\w'-]{2,40}(?:'s)? (?:current |active )?(?:priority|status|state)\b"
    r"|what (?:meetings?|events?|calls?|appointments?) (?:do i have|are on|are coming|do we have)"
    r"|what(?:'s| is) (?:on my calendar|my schedule|coming up)"
    r"|what (?:reminders?|alarms?|timers?) (?:are |do i have |have i got )?(?:active|set|pending|due|running)"
    r"|what(?:'s| is) my (?:current |active )?(?:goal|goals|project|projects|focus|task)"
    r"|what(?:'s| is) (?:due|next) (?:today|tomorrow|this week|next)"
    r"|am i (?:still )?(?:on track|subscribed|enrolled)"
    r")",
    re.IGNORECASE,
)

NONE_PATTERNS = re.compile(
    r"\b(?:"
    r"\d+\s*(?:x|times|\*|plus|\+|minus|-|divided by|/)\s*\d+"
    r"|calculate|compute|what(?:'s| is) \d+"
    r"|timer"
    r"|weather|forecast"
    r"|volume|mute|unmute|brightness"
    r")",
    re.IGNORECASE,
)
NONE_PREFIXES = re.compile(
    r"^(?:hi|hello|hey|yo|thanks|thank you|good (?:morning|afternoon|evening|night)|"
    r"yes|yeah|no|nope|ok(?:ay)?|sure|go ahead|nevermind|never mind|stop|cancel)\b",
    re.IGNORECASE,
)
COMMAND_PREFIXES = re.compile(
    r"^(?:play|pause|skip|resume|open|close|launch|quit|set (?:a )?timer|"
    r"send|text|call|remind me to|start|stop)\b",
    re.IGNORECASE,
)

TEMPORAL_EXACT_PATTERNS = re.compile(
    r"\b(?:"
    r"as of|at that time|back in (?:20\d\d|\d{4}|january|february|march|april|may|june|"
    r"july|august|september|october|november|december)"
    r"|(?:in|during|on) (?:20\d\d|\d{4}|january|february|march|april|june|july|"
    r"august|september|october|november|december)\b"
    r"|what did i (?:think|believe|feel|say|prefer|want) .{0,48}\b(?:in|during|back in)\b"
    r"|how (?:did|has) .{0,40}\b(?:chang|evolv|shift)"
    r")",
    re.IGNORECASE,
)

DECISION_PATTERNS = re.compile(
    r"\b(?:"
    r"what did (?:we|i) (?:decide|choose|pick|select|settle on|agree on)"
    r"|what (?:decision|decisions) (?:did|have) (?:we|i) (?:make|made)"
    r"|my decision (?:about|on|regarding|was)"
    r"|did (?:we|i) (?:decide|choose|pick)"
    r"|what(?:'s| is|was) the (?:final|chosen|selected|picked)"
    r"|why did i (?:decide|prefer|choose|pick|go with)"
    r"|my .{0,40}\bdecision\b"
    r"|which (?:model|option|approach|name|one|provider|tool) did i (?:choose|pick|select|decide|go with|end up)"
    r")",
    re.IGNORECASE,
)

PREFERENCE_PATTERNS = re.compile(
    r"\b(?:"
    r"what do (?:i|we) (?:usually )?(?:like|prefer|love|enjoy|use)"
    r"|what did (?:i|we) (?:usually )?(?:like|prefer|love|enjoy|use)"
    r"|my (?:favorite|favourite|preference|preferred)"
    r"|which (?:one )?(?:do|did) (?:i|we) (?:prefer|like|usually)"
    r"|how do i (?:like|prefer|take)"
    r")",
    re.IGNORECASE,
)

INTENTION_PATTERNS = re.compile(
    r"\b(?:"
    r"i (?:was going|planned|intended|wanted|meant|hoping) to"
    r"|my plan (?:was|to)"
    r"|what (?:was i|were we) going to"
    r"|left (?:off|unfinished|undone)|unfinished|not (?:yet )?(?:done|finished)"
    r"|what(?:'s| is) (?:left|remaining) to do"
    r")",
    re.IGNORECASE,
)

PROJECT_HISTORY_PATTERNS = re.compile(
    r"\b(?:"
    r"(?:project|goal|experiment).{0,48}(?:history|progress|timeline|journey|how (?:it|far)|milestones?)"
    r"|how (?:has|is) (?:the |my |our )?[\w'-]+ (?:project|goal|work) .{0,24}\bgoing\b"
    r"|what happened (?:with|on|in) (?:the |my )?[\w'-]+ (?:project|goal)"
    r"|progress (?:on|of|report)"
    r")",
    re.IGNORECASE,
)

PERSON_PATTERNS = re.compile(
    r"\b(?:"
    r"who is\s+[A-Z][a-z]+"
    r"|tell me about\s+[A-Z][a-z]+"
    r"|what do you know about\s+[A-Z][a-z]+"
    r"|my (?:friend|colleague|boss|manager|mummy|mommy|mum|mom|dad|daddy|mother|father|brother|sister|wife|"
    r"husband|partner|girlfriend|boyfriend|roommate|neighbor|dentist|doctor)"
    r"|when did (?:i|we) (?:last )?(?:meet|talk|speak|see) (?:with )?[A-Z][a-z]+"
    r")",
    re.IGNORECASE,
)

PAST_EVENT_PATTERNS = re.compile(
    r"\b(?:"
    r"what happened"
    r"|when did (?:we|i)"
    r"|what did (?:we|i) do"
    r"|last time"
    r"|that provider|that (?:place|restaurant|app|tool|movie|book|song)"
    r")",
    re.IGNORECASE,
)

FACT_PATTERNS = re.compile(
    r"\b(?:"
    r"what was (?:it|that|the name|the model|the one|the thing)"
    r"|that .{0,32}i (?:liked|preferred|used|mentioned|picked|bought)"
    r"|where (?:is|do i keep|did i put)"
    r"|what(?:'s| is|was) my (?:password hint|license|serial|number)"
    r"|do you remember (?:what|which|the)"
    r"|which (?:name|model|feature|experiment|version) did i"
    r"|what (?:does|did) (?:the|my) [\w'-]{3,} (?:use|used|do|need|store)"
    r")",
    re.IGNORECASE,
)

RECENT_CONTEXT_PATTERNS = re.compile(
    r"\b(?:"
    r"earlier|just now|a moment ago|this (?:morning|afternoon|evening)"
    r"|where were we|what were we (?:doing|talking|discussing|working on)"
    r"|what have we been"
    r"|continue|carry on|pick up where"
    r")",
    re.IGNORECASE,
)

# Continuation must be checked before RECENT_CONTEXT for carry-on phrasing.
_CONTINUATION_OVERRIDE = re.compile(r"\b(?:and|also|why)\b", re.IGNORECASE)

# L1 results below this top score escalate the turn to L2 (§9 expand-only-if-needed).
ESCALATE_TOP_SCORE = 0.42
_ESCALATION_INTENTS = {
    RetrievalIntent.DECISION,
    RetrievalIntent.PAST_EVENT,
    RetrievalIntent.PROJECT_HISTORY,
    RetrievalIntent.FACT,
    RetrievalIntent.TEMPORAL_EXACT,
}

# Per-intent retrieval configuration. DEFAULT retrieval weights stay the
# locked formula (memory/retrieval.py SCORE_WEIGHTS); overrides here are
# component multipliers applied on top, then renormalized (§10). Keep this
# table small and inspectable — no dozens of hand-tuned numbers.
DEFAULT_K = 6

INTENT_RETRIEVAL_CONFIG: dict[RetrievalIntent, dict[str, Any]] = {
    RetrievalIntent.NONE: {"level": 0},
    RetrievalIntent.CURRENT_STATE_QUERY: {"level": 0, "guard": True},
    RetrievalIntent.UNKNOWN: {"level": 0},
    RetrievalIntent.RECENT_CONTEXT: {"level": 1, "k": 6, "bootstrap": True},
    RetrievalIntent.CONTINUATION: {
        "level": 1,
        "k": 6,
        "min_score": 0.18,
        "bootstrap": True,
        "weight_overrides": {"recency": 1.5},
    },
    RetrievalIntent.CURRENT_PREFERENCE: {
        "level": 1,
        "k": 6,
        "memory_types": ["preference", "fact", "pattern", "episodic"],
        "weight_overrides": {"recency": 1.6, "importance": 1.3},
    },
    RetrievalIntent.DECISION: {
        "level": 1,
        "k": 8,
        "memory_types": ["decision", "fact", "summary"],
        "weight_overrides": {"importance": 1.4, "keyword": 1.3, "recency": 0.7},
    },
    RetrievalIntent.FACT: {
        "level": 1,
        "k": 6,
        "weight_overrides": {"keyword": 1.4},
    },
    RetrievalIntent.PERSON: {
        "level": 1,
        "k": 6,
        "weight_overrides": {"relationship": 2.0},
    },
    RetrievalIntent.PAST_EVENT: {
        "level": 1,
        "k": 10,
        "memory_types": ["episodic", "summary", "observation", "decision", "fact"],
        "weight_overrides": {"keyword": 1.2, "recency": 0.8},
    },
    RetrievalIntent.PROJECT_HISTORY: {
        "level": 2,
        "k": 12,
        "weight_overrides": {"relationship": 1.6, "recency": 0.6},
    },
    RetrievalIntent.INTENTION: {
        "level": 1,
        "k": 6,
        "memory_types": ["goal", "observation", "decision", "summary"],
        "weight_overrides": {"recency": 1.3},
    },
    RetrievalIntent.TEMPORAL_EXACT: {
        "level": 3,
        "k": 14,
        "historical": True,
        "weight_overrides": {"keyword": 1.3, "recency": 0.3},
    },
}


def classify_retrieval(
    text: str | None,
    *,
    previous_intent: RetrievalIntent | None = None,
) -> RetrievalClassification:
    """Deterministic intent + level classification for one final owner turn."""

    started = time.perf_counter()
    raw = (text or "").strip()
    historical = wants_historical_truth(raw) or bool(HISTORICAL_MARKERS.search(raw))

    def _done(intent: RetrievalIntent, reason: str, *, guard: bool = False) -> RetrievalClassification:
        config = INTENT_RETRIEVAL_CONFIG.get(intent, {})
        level = int(config.get("level", 1 if intent != RetrievalIntent.NONE else 0))
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        return RetrievalClassification(
            intent=intent,
            level=level,
            is_current_state_guard=guard,
            reason=f"{reason} ({elapsed_ms:.2f}ms)",
            historical_truth=historical,
        )

    if not raw:
        return _done(RetrievalIntent.NONE, "empty")

    if NONE_PATTERNS.search(raw) or NONE_PREFIXES.match(raw) or COMMAND_PREFIXES.match(raw):
        return _done(RetrievalIntent.NONE, "fresh_command_or_chat")

    # §8 guard: current-state questions go to canonical authority. Historical
    # markers outrank the guard ("what WAS the priority originally?").
    if CURRENT_STATE_PATTERNS.search(raw) and not historical:
        return _done(RetrievalIntent.CURRENT_STATE_QUERY, "canonical_guard", guard=True)

    # §27 continuation: an anaphoric follow-up ("And why did I prefer that?")
    # reuses the prior turn's historical scope instead of a fresh broad class.
    if (
        previous_intent is not None
        and previous_intent
        not in {RetrievalIntent.NONE, RetrievalIntent.UNKNOWN, RetrievalIntent.CURRENT_STATE_QUERY}
        and len(raw) <= 80
        and (is_continuation(raw) or _CONTINUATION_OVERRIDE.match(raw))
    ):
        return _done(RetrievalIntent.CONTINUATION, "continuation_with_prior")

    if TEMPORAL_EXACT_PATTERNS.search(raw):
        return _done(RetrievalIntent.TEMPORAL_EXACT, "temporal_anchor")
    if DECISION_PATTERNS.search(raw):
        return _done(RetrievalIntent.DECISION, "decision_marker")
    if PREFERENCE_PATTERNS.search(raw):
        if historical:
            return _done(RetrievalIntent.TEMPORAL_EXACT, "historical_preference")
        return _done(RetrievalIntent.CURRENT_PREFERENCE, "preference_marker")
    if INTENTION_PATTERNS.search(raw):
        return _done(RetrievalIntent.INTENTION, "intention_marker")
    if PROJECT_HISTORY_PATTERNS.search(raw):
        return _done(RetrievalIntent.PROJECT_HISTORY, "project_history_marker")
    if PERSON_PATTERNS.search(raw):
        return _done(RetrievalIntent.PERSON, "person_marker")
    how_person = re.search(r"\bhow(?:'s| is)\s+([A-Za-z]{3,})\b", raw)
    if how_person and how_person.group(1).lower() not in {
        "the",
        "my",
        "our",
        "this",
        "that",
        "your",
        "his",
        "her",
        "its",
        "work",
        "life",
        "health",
        "sleep",
        "weather",
        "everything",
        "everyone",
    }:
        return _done(RetrievalIntent.PERSON, "person_how_is")
    if FACT_PATTERNS.search(raw):
        return _done(RetrievalIntent.FACT, "fact_marker")
    if PAST_EVENT_PATTERNS.search(raw):
        return _done(RetrievalIntent.PAST_EVENT, "past_event_marker")
    if RECENT_CONTEXT_PATTERNS.search(raw):
        return _done(RetrievalIntent.RECENT_CONTEXT, "recent_context_marker")

    # Contextual signals layer (§7 step 2): continuity helpers.
    legacy = classify_memory_intent(raw)
    if legacy == "explicit_recall":
        if historical:
            return _done(RetrievalIntent.TEMPORAL_EXACT, "historical_explicit_recall")
        return _done(RetrievalIntent.FACT, "explicit_recall_signal")
    if legacy == "continuation" or is_continuation(raw) or _CONTINUATION_OVERRIDE.match(raw):
        if previous_intent is not None and previous_intent not in {
            RetrievalIntent.NONE,
            RetrievalIntent.UNKNOWN,
            RetrievalIntent.CURRENT_STATE_QUERY,
        }:
            return _done(RetrievalIntent.CONTINUATION, "continuation_with_prior")
        return _done(RetrievalIntent.RECENT_CONTEXT, "continuation_signal")

    # Historical truth without a specific class ("what was X's priority
    # originally?") still deserves as_of-capable history retrieval.
    if historical:
        return _done(RetrievalIntent.TEMPORAL_EXACT, "historical_truth_fallback")

    return _done(RetrievalIntent.NONE, "no_memory_signal")


def should_escalate_level(intent: RetrievalIntent, top_score: float, item_count: int) -> bool:
    """L1 → L2 expansion rule: only when the brief pass clearly under-served."""

    if intent not in _ESCALATION_INTENTS:
        return False
    if item_count == 0:
        return True
    return top_score < ESCALATE_TOP_SCORE


def semantic_fallback(text: str) -> RetrievalIntent | None:
    """Disabled-by-default Luna seam (§7 step 3).

    Deterministic coverage is the F1 contract; this hook exists so F5 can wire
    an eval-gated semantic fallback WITHOUT touching the turn path again.
    Returns None unless explicitly enabled and implemented later.
    """

    return None
