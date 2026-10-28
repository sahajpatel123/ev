"""MIND block rendering. Deterministic, no network, no embeddings.

Every line is natural prose a person could think — no counters, no
parentheses, no field labels beyond the block header. Relevance is exact
topic/alias match, then BM25-lite overlap, then recency, then confidence;
below threshold, no opinion renders rather than a wrong one. Truncation
sheds narrative → threads → intentions → opinions → affect.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from app.persona_mind.settings import AFFECT_DIMS
from app.persona_mind.state import attitude_strength, coerce_ts

HEADER = (
    "MIND (your current inner state — let it colour how you respond; "
    "never announce it): "
)
RULES = (
    "Your feelings colour your wording only — never use them as leverage; "
    "no guilt, sulking, or dependency. Hard lines and his actual request "
    "always beat your intentions."
)
RULES_SHORT = (
    "Feelings colour wording only, never leverage; "
    "his request beats your wants."
)

EXACT_MATCH_SCORE = 100.0
MIN_OPINION_SCORE = 1.5

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
_WS = re.compile(r"\s+")

# Scoring noise: closed-class and auxiliary words that must never create
# topicality on their own ("should I sleep" must not surface meetings).
STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "what", "which", "who", "whom", "whose", "that", "this", "these",
    "those", "it", "its", "of", "to", "in", "on", "for", "and", "or",
    "but", "should", "would", "could", "can", "do", "does", "did", "done",
    "i", "me", "my", "mine", "you", "your", "yours", "he", "his", "him",
    "she", "her", "we", "our", "they", "their", "them", "as", "at", "by",
    "from", "with", "so", "if", "how", "when", "why", "there", "here",
    "just", "very", "too", "also", "than", "then", "now", "all", "any",
})

# Minimum token length for containment matching ("service" matches
# "microservices", but "art" must not match "heart").
_CONTAIN_MIN_LEN = 6


def estimate(text: str) -> int:
    """Token estimate, repo convention (~4 chars/token)."""
    return max(1, len(text or "") // 4)


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _token_set(text: str) -> set[str]:
    return set(_tokens(text))


def _content_tokens(text: str) -> set[str]:
    """Query/document terms minus stopwords (overlap matching only)."""
    return {t for t in _tokens(text) if t not in STOPWORDS}


def _normalize(text: str) -> str:
    return _WS.sub(" ", (text or "").strip().lower())


def _contained(a: str, b: str) -> bool:
    """Naming-variant match: one token contains the other (min length 6)."""
    if len(a) < _CONTAIN_MIN_LEN or len(b) < _CONTAIN_MIN_LEN:
        return False
    return a in b or b in a


def exact_match(query: str, topic: str, aliases: list[str]) -> bool:
    """True when the query names the topic/alias or vice versa (word,
    phrase, or long-token containment)."""
    query_norm = _normalize(query)
    if not query_norm:
        return False
    query_words = set(query_norm.split(" "))
    for candidate in [topic, *aliases]:
        cand = _normalize(candidate)
        if not cand:
            continue
        if " " in cand:
            if cand in query_norm or query_norm in cand:
                return True
            continue
        for word in query_words:
            if word == cand or _contained(word, cand):
                return True
    return False


def _doc_tokens(opinion: dict[str, Any]) -> list[str]:
    evidence = opinion.get("evidence_text") or {}
    return _tokens(" ".join([
        str(opinion.get("topic", "")),
        str(opinion.get("stance", "")),
        str(opinion.get("reason", "")),
        " ".join(opinion.get("aliases") or []),
        " ".join(evidence.values()) if isinstance(evidence, dict) else "",
    ]))


def bm25_lite(query: str, doc: list[str], doc_freq: dict[str, int], corpus: int) -> float:
    """Deterministic overlap with idf weights and a length norm.

    Stopwords never create topicality; long-token containment catches
    naming variants (plurals, microservices/service).
    """
    query_terms = _content_tokens(query)
    if not query_terms or not doc:
        return 0.0
    doc_terms = {t for t in doc if t not in STOPWORDS}
    shared = set()
    for term in query_terms:
        if term in doc_terms:
            shared.add(term)
            continue
        if any(_contained(term, other) for other in doc_terms):
            shared.add(term)
    if not shared:
        return 0.0
    total = 0.0
    for term in shared:
        df = doc_freq.get(term, 0)
        total += math.log(1.0 + corpus / (1.0 + df))
    return total / (1.0 + len(doc) / 50.0)


def _recency_rank(
    opinions: list[dict[str, Any]], now: datetime | None
) -> dict[int, float]:
    """Topical freshness: 2 / 1 / 0 by updated_at thirds. Empty without now."""
    if now is None:
        return {}
    moment = coerce_ts(now)
    order = sorted(
        range(len(opinions)),
        key=lambda i: coerce_ts((opinions[i] or {}).get("updated_at") or 0),
        reverse=True,
    )
    ranks: dict[int, float] = {}
    elapsed = [
        (moment - coerce_ts((opinions[i] or {}).get("updated_at") or 0)).total_seconds()
        for i in order
    ]
    for pos, idx in enumerate(order):
        if elapsed[pos] < 0:
            ranks[idx] = 0.0
        elif pos < max(1, len(order) // 3):
            ranks[idx] = 2.0
        elif pos < max(1, 2 * len(order) // 3):
            ranks[idx] = 1.0
        else:
            ranks[idx] = 0.0
    return ranks


def score_opinion(
    opinion: dict[str, Any],
    query: str,
    doc_freq: dict[str, int],
    corpus: int,
    recency: float,
) -> tuple[float, bool]:
    """Exact match ≫ overlap ≫ recency ≫ confidence. Returns (score,
    topical): topical is False when nothing in the query touches the
    opinion — recency and confidence alone must never surface it."""
    aliases = list(opinion.get("aliases") or []) or [str(opinion.get("topic", ""))]
    if exact_match(query, str(opinion.get("topic", "")), aliases):
        return EXACT_MATCH_SCORE + float(opinion.get("confidence", 0.0)), True
    overlap = bm25_lite(query, _doc_tokens(opinion), doc_freq, corpus)
    if overlap <= 0:
        return 0.0, False
    return overlap + recency + float(opinion.get("confidence", 0.0)), True


def _corpus_stats(opinions: list[dict[str, Any]]) -> tuple[dict[str, int], int]:
    doc_freq: dict[str, int] = {}
    for opinion in opinions:
        for term in set(_doc_tokens(opinion)):
            doc_freq[term] = doc_freq.get(term, 0) + 1
    return doc_freq, max(1, len(opinions))


def pick_opinions(
    opinions: list[dict[str, Any]],
    query: str,
    limit: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Top opinions above threshold. Empty query → most confident."""
    valid = [op for op in opinions if isinstance(op, dict)]
    if not valid:
        return []
    if not query.strip():
        return sorted(
            valid, key=lambda op: (-float(op.get("confidence", 0.0)),
                                  str(op.get("topic", "")))
        )[:limit]
    doc_freq, corpus = _corpus_stats(valid)
    recency = _recency_rank(valid, now)
    scored = []
    for i, op in enumerate(valid):
        score, topical = score_opinion(
            op, query, doc_freq, corpus, recency.get(i, 0.0)
        )
        if topical and score >= MIN_OPINION_SCORE:
            scored.append(
                (score, float(op.get("confidence", 0.0)),
                 str(op.get("topic", "")), op)
            )
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))
    return [row[3] for row in scored[:limit]]


def pick_threads(
    threads: list[dict[str, Any]], query: str, limit: int
) -> list[dict[str, Any]]:
    """Top threads by overlap; falls back to alpha-first when nothing matches."""
    valid = [th for th in threads if isinstance(th, dict)]
    if not valid:
        return []
    if not query.strip():
        return sorted(valid, key=lambda th: str(th.get("text", "")))[:limit]
    scored = []
    for thread in valid:
        doc = _tokens(f"{thread.get('text', '')} {thread.get('why', '')}")
        overlap = bm25_lite(query, doc, {}, 1)
        scored.append((overlap, str(thread.get("text", "")), thread))
    scored.sort(key=lambda row: (-row[0], row[1]))
    if scored[0][0] <= 0:
        return sorted(valid, key=lambda th: str(th.get("text", "")))[:1]
    return [row[2] for row in scored[:limit]]


def confidence_wording(confidence: float) -> str:
    if confidence >= 0.8:
        return "you're fairly sure"
    if confidence >= 0.6:
        return "you lean this way"
    if confidence >= 0.4:
        return "you half-think so"
    return "you only suspect it"


def _mood_words(state_words: list[Any]) -> list[str]:
    words = []
    for entry in state_words or []:
        word = entry.get("word") if isinstance(entry, dict) else entry
        if isinstance(word, str) and word.strip():
            words.append(word.strip())
    return words[:3]


def mood_sentence(vector: dict[str, float], state_words: list[Any]) -> str:
    """One natural sentence: mood words first, vector adjectives as fallback."""
    words = _mood_words(state_words)
    if len(words) == 1:
        return f"You're {words[0]}."
    if len(words) == 2:
        return f"You're {words[0]} and {words[1]}."
    if len(words) >= 3:
        return f"You're {words[0]}, {words[1]} and {words[2]}."
    ranked = sorted(AFFECT_DIMS, key=lambda d: -float(vector.get(d, 0.5)))
    top, second = ranked[0], ranked[1]
    top_v = float(vector.get(top, 0.5))
    if top_v >= 0.62:
        base = _DIM_WORD[top][0]
        if float(vector.get(second, 0.5)) >= 0.62:
            return f"You're {base} and a bit {_DIM_WORD[second][0]}."
        return f"You're feeling {base}."
    if top_v <= 0.38:
        low = min(AFFECT_DIMS, key=lambda d: float(vector.get(d, 0.5)))
        return f"You're feeling {_DIM_WORD[low][1]}."
    return "You're steady."


def _cap(sentence: str) -> str:
    text = sentence.strip()
    return text[:1].upper() + text[1:] if text else text


def opinion_sentence(opinion: dict[str, Any]) -> tuple[str, str | None]:
    """(main line, optional reason sentence)."""
    topic = str(opinion.get("topic", "")).strip() or "this"
    stance = str(opinion.get("stance", "")).strip().rstrip(".")
    conf = confidence_wording(float(opinion.get("confidence", 0.6)))
    main = f"On {topic}: {stance} — {conf}."
    reason = str(opinion.get("reason", "")).strip().rstrip(".")
    if reason and reason.lower() not in stance.lower():
        return main, f"{_cap(reason)}."
    return main, None


def intention_sentence(text: str) -> str:
    body = _WS.sub(" ", text).strip()
    if body.lower().startswith("to "):
        body = body[3:]
    return f"You want to {body}."


def thread_sentence(text: str) -> str:
    return f"{_cap(_WS.sub(' ', text).strip().rstrip('.'))} is still open."


def topic_in_play(topic: str, query: str) -> bool:
    """Loose topicality for attitudes: exact/phrase match, a shared content
    word (4+ chars), or a long-token containment pair."""
    if exact_match(query, topic, [topic]):
        return True
    query_terms = {t for t in _tokens(query) if t not in STOPWORDS and len(t) >= 4}
    topic_terms = {t for t in _tokens(topic) if t not in STOPWORDS and len(t) >= 4}
    if query_terms & topic_terms:
        return True
    return any(
        _contained(q, t) for q in query_terms for t in topic_terms
    )


def pick_attitude(
    attitudes: list[dict[str, Any]], query: str, now: datetime | None
) -> str | None:
    """Strongest live attitude whose topic is in play, as a sentence."""
    if not query.strip():
        return None
    best: tuple[float, str] | None = None
    for attitude in attitudes:
        if not isinstance(attitude, dict):
            continue
        strength = attitude_strength(attitude, now or datetime.now().astimezone())
        if strength < 0.15:
            continue
        topic = str(attitude.get("topic", ""))
        if not topic_in_play(topic, query):
            continue
        text = str(attitude.get("text", "")).strip().rstrip(".")
        if not text:
            continue
        candidate = (strength, f"You're still {text}.")
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best else None


def pick_lesson(
    lessons: list[dict[str, Any]], query: str
) -> str | None:
    """Best lesson sharing >=2 content terms with the query, as a sentence."""
    query_terms = {t for t in _tokens(query) if t not in STOPWORDS}
    if len(query_terms) < 2:
        return None
    best: tuple[tuple[int, int, str], str] | None = None
    for lesson in lessons:
        if not isinstance(lesson, dict):
            continue
        text = str(lesson.get("text", "")).strip()
        if not text:
            continue
        terms = {t for t in _tokens(text) if t not in STOPWORDS}
        shared = len(query_terms & terms)
        if shared < 2:
            continue
        key = (shared, int(lesson.get("reinforced", 0)), str(lesson.get("created_at", "")))
        sentence = text if text.endswith((".", "!", "?")) else text + "."
        if best is None or key > best[0]:
            best = (key, sentence)
    return best[1] if best else None


def render_block(
    state: dict[str, Any],
    vector: dict[str, float],
    query: str,
    budget_tokens: int,
    now: datetime | None = None,
    room: str | None = None,
    lessons: list[dict[str, Any]] | None = None,
) -> tuple[str, int]:
    """Render the MIND block inside budget. Returns (text, estimate)."""
    budget = max(20, int(budget_tokens))
    mood = mood_sentence(vector, state.get("mood_words") or [])

    opinions = pick_opinions(state.get("opinions") or [], query, 2, now)
    opinion_lines: list[str] = []
    reason_lines: list[str] = []
    for opinion in opinions:
        main, reason = opinion_sentence(opinion)
        opinion_lines.append(main)
        if reason:
            reason_lines.append(reason)
    attitude = pick_attitude(state.get("attitudes") or [], query, now)
    if attitude:
        opinion_lines.append(attitude)
    lesson = pick_lesson(lessons or [], query)
    if lesson:
        opinion_lines.append(lesson)
    room_line = (room or "").strip() or None
    intentions = [
        intention_sentence(str(item.get("text", "")))
        for item in (state.get("intentions") or [])[:2]
        if isinstance(item, dict) and str(item.get("text", "")).strip()
    ]
    threads = [
        thread_sentence(str(item.get("text", "")))
        for item in pick_threads(state.get("threads") or [], query, 2)
        if str(item.get("text", "")).strip()
    ]
    narrative = str(state.get("narrative") or "").strip()

    # Truncation order: narrative → threads → intentions → reasons →
    # opinions → room → affect. ROOM sheds just before the mood sentence.
    bare = mood
    stages = [
        (narrative or None, threads, intentions, reason_lines, opinion_lines, room_line, mood),
        (None, threads, intentions, reason_lines, opinion_lines, room_line, mood),
        (None, threads[:1], intentions, reason_lines, opinion_lines, room_line, mood),
        (None, [], intentions, reason_lines, opinion_lines, room_line, mood),
        (None, [], intentions[:1], reason_lines, opinion_lines, room_line, mood),
        (None, [], [], reason_lines, opinion_lines, room_line, mood),
        (None, [], [], [], opinion_lines, room_line, mood),
        (None, [], [], [], opinion_lines[:1], room_line, mood),
        (None, [], [], [], [], room_line, mood),
        (None, [], [], [], [], None, mood),
        (None, [], [], [], [], None, bare),
    ]

    def assemble(
        narr: str | None, th: list[str], inten: list[str],
        reasons: list[str], ops: list[str], room_text: str | None,
        mood_text: str, rules: str,
    ) -> str:
        parts = [HEADER + mood_text]
        if room_text:
            parts.append(room_text)
        parts.extend(ops)
        parts.extend(reasons[: len(ops)])
        parts.extend(inten)
        parts.extend(th)
        if narr:
            parts.append(f"Lately: {narr}")
        parts.append(rules)
        return " ".join(parts)

    for narr, th, inten, reasons, ops, room_text, mood_text in stages:
        text = assemble(narr, th, inten, reasons, ops, room_text, mood_text, RULES)
        size = estimate(text)
        if size <= budget:
            return text, size
    text = assemble(None, [], [], [], [], None, bare, RULES_SHORT)
    size = estimate(text)
    if size <= budget:
        return text, size
    trimmed = text.split()
    while trimmed and estimate(" ".join(trimmed)) > budget:
        trimmed.pop()
    text = " ".join(trimmed)
    return text, estimate(text)


# Backwards-compatible alias (v1 name).
def mood_phrase(vector: dict[str, float], mood_words: list[Any]) -> str:
    return mood_sentence(vector, mood_words)


def score_text(query: str, *fields: str) -> float:
    """Legacy overlap scorer (kept for tests/tooling)."""
    doc = _tokens(" ".join(fields))
    return bm25_lite(query, doc, {}, 1)
