"""MIND block rendering. Deterministic, no network, no embeddings.

Relevance is keyword overlap; truncation sheds narrative → threads →
intentions → opinions → affect. The block states its own rules.
"""

from __future__ import annotations

import re
from typing import Any

from app.persona_mind.settings import AFFECT_DIMS

HEADER = (
    "MIND (your current inner state — let it colour how you respond; "
    "never announce it): "
)
RULES = (
    "Your feelings colour your wording only — never use them as leverage; "
    "no guilt, sulking, or dependency. Hard lines and his actual request "
    "always beat your intentions."
)
# Compressed rules for tight (voice) budgets. Same three constraints.
RULES_SHORT = (
    "Feelings colour wording only, never leverage; "
    "his request beats your wants."
)

_DIM_WORD = {
    "playfulness": ("playful", "flat"),
    "warmth": ("warm", "cool"),
    "concern": ("concerned", "easy"),
    "energy": ("sharp", "drained"),
    "impatience": ("impatient", "patient"),
    "satisfaction": ("pleased", "unsatisfied"),
    "curiosity": ("curious", "unbothered"),
}

_WORD_RE = re.compile(r"[a-z0-9']+")


def estimate(text: str) -> int:
    """Token estimate, repo convention (~4 chars/token)."""
    return max(1, len(text or "") // 4)


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def score_text(query: str, *fields: str) -> int:
    """Deterministic relevance: shared-token count with light length norm."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return 0
    doc = _tokens(" ".join(fields))
    shared = query_tokens & doc
    if not shared:
        return 0
    return len(shared) * 2 - (len(doc) // 12)


def confidence_wording(confidence: float) -> str:
    if confidence >= 0.8:
        return "you're fairly sure"
    if confidence >= 0.6:
        return "you lean"
    if confidence >= 0.4:
        return "you half-think"
    return "you suspect, weakly"


def mood_phrase(vector: dict[str, float], mood_words: list[Any]) -> str:
    """One natural-language mood phrase from the vector + mood words."""
    words = []
    for entry in mood_words or []:
        word = entry.get("word") if isinstance(entry, dict) else entry
        if isinstance(word, str) and word.strip():
            words.append(word.strip())
    ranked = sorted(
        AFFECT_DIMS, key=lambda d: -float(vector.get(d, 0.5))
    )
    top, second = ranked[0], ranked[1]
    top_v, second_v = float(vector.get(top, 0.5)), float(vector.get(second, 0.5))
    if top_v >= 0.62:
        base = _DIM_WORD[top][0]
        if second_v >= 0.62 and second != top:
            base = f"{base} and {_DIM_WORD[second][0]}"
    elif top_v <= 0.38:
        low = min(AFFECT_DIMS, key=lambda d: float(vector.get(d, 0.5)))
        base = _DIM_WORD[low][1]
    else:
        base = "steady"
    if words:
        return f"feeling {', '.join(words[:3])} ({base})"
    return f"feeling {base}"


def _pick_opinions(
    opinions: list[dict[str, Any]], query: str, limit: int
) -> list[dict[str, Any]]:
    scored = []
    for op in opinions:
        if not isinstance(op, dict):
            continue
        score = score_text(
            query, str(op.get("topic", "")), str(op.get("stance", "")),
            str(op.get("reason", "")),
        )
        scored.append(
            (score, float(op.get("confidence", 0.0)), str(op.get("topic", "")), op)
        )
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))
    if query.strip():
        scored = [row for row in scored if row[0] > 0] or scored[:1]
    return [row[3] for row in scored[:limit]]


def _pick_threads(
    threads: list[dict[str, Any]], query: str, limit: int
) -> list[dict[str, Any]]:
    scored = []
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        score = score_text(
            query, str(thread.get("text", "")), str(thread.get("why", ""))
        )
        scored.append((score, str(thread.get("text", "")), thread))
    scored.sort(key=lambda row: (-row[0], row[1]))
    if query.strip():
        scored = [row for row in scored if row[0] > 0] or scored[:1]
    return [row[2] for row in scored[:limit]]


def render_block(
    state: dict[str, Any],
    vector: dict[str, float],
    query: str,
    budget_tokens: int,
) -> tuple[str, int]:
    """Render the MIND block inside budget. Returns (text, estimate)."""
    budget = max(20, int(budget_tokens))
    mood = mood_phrase(vector, state.get("mood_words") or [])

    opinions = _pick_opinions(state.get("opinions") or [], query, 2)
    opinion_bits = [
        f"On {op.get('topic')}: {op.get('stance')}, "
        f"{confidence_wording(float(op.get('confidence', 0.6)))}"
        + (f" ({op.get('reason')})" if op.get("reason") else "")
        for op in opinions
    ]
    intentions = [
        str(item.get("text", "")) for item in (state.get("intentions") or [])[:2]
        if isinstance(item, dict) and str(item.get("text", "")).strip()
    ]
    threads = [
        str(item.get("text", ""))
        for item in _pick_threads(state.get("threads") or [], query, 2)
        if str(item.get("text", "")).strip()
    ]
    narrative = str(state.get("narrative") or "").strip()

    # Truncation order: narrative → threads → intentions → opinions → affect.
    # Affect (mood) is last; it sheds parenthetical detail before words.
    stages: list[tuple[str | None, list[str], list[str], list[str], str]] = []
    stages.append((narrative or None, threads, intentions, opinion_bits, mood))
    stages.append((None, threads, intentions, opinion_bits, mood))
    stages.append((None, threads[:1], intentions, opinion_bits, mood))
    stages.append((None, [], intentions, opinion_bits, mood))
    stages.append((None, [], intentions[:1], opinion_bits, mood))
    stages.append((None, [], [], opinion_bits, mood))
    stages.append((None, [], [], opinion_bits[:1], mood))
    stages.append((None, [], [], [], mood))
    bare_words = []
    for entry in state.get("mood_words") or []:
        word = entry.get("word") if isinstance(entry, dict) else entry
        if isinstance(word, str) and word.strip():
            bare_words.append(word.strip())
    bare = f"feeling {', '.join(bare_words[:3])}" if bare_words else "feeling steady"
    stages.append((None, [], [], [], bare))

    def assemble(
        narr: str | None, th: list[str], inten: list[str],
        ops: list[str], mood_text: str, rules: str,
    ) -> str:
        parts = [HEADER + mood_text + "."]
        for bit in ops:
            parts.append(bit + ".")
        if inten:
            parts.append("You want: " + "; ".join(inten) + ".")
        if th:
            parts.append("Open: " + "; ".join(th) + ".")
        if narr:
            parts.append("Lately: " + narr)
        parts.append(rules)
        return " ".join(parts)

    for narr, th, inten, ops, mood_text in stages:
        text = assemble(narr, th, inten, ops, mood_text, RULES)
        size = estimate(text)
        if size <= budget:
            return text, size
    # Tight budgets (voice): same barest content with compressed rules.
    text = assemble(None, [], [], [], bare, RULES_SHORT)
    size = estimate(text)
    if size <= budget:
        return text, size
    # Absolute guarantee: trim the mood fragment word by word.
    words = text.split()
    while words and estimate(" ".join(words)) > budget:
        words.pop()
    text = " ".join(words)
    return text, estimate(text)
