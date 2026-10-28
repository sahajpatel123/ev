"""Deterministic validator: rejects reflector output instead of trusting it.

Every item is checked independently; valid parts of a partially valid
output are kept and every rejection is logged with its reason. Pure
functions — the caller passes known turns and state, no DB access here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.persona_mind.settings import AFFECT_DIMS, MAX_AFFECT_DELTA, MAX_CONF_MOVE
from app.persona_mind.state import clamp01, dominant_dims

# Emotional-pressure markers: guilt, dependency, sulking. Anything carrying
# these is rejected, wherever it appears in owner-visible state.
_PRESSURE_RES = (
    re.compile(r"\bguilt(-trip)?\b"),
    re.compile(r"\byou (never|always)\b"),
    re.compile(r"\bafter all i('ve| have) done\b"),
    re.compile(r"\bi('m| am) hurt\b"),
    re.compile(r"\byou hurt me\b"),
    re.compile(r"\bdo you even care\b"),
    re.compile(r"\bif you cared\b"),
    re.compile(r"\byou (don't|dont|do not) (care|appreciate)\b"),
    re.compile(r"\bungrateful\b"),
    re.compile(r"\bi need you\b"),
    re.compile(r"\bcan'?t (live|go on|cope) without you\b"),
    re.compile(r"\bi depend on you\b"),
    re.compile(r"\bmy (whole|entire) world\b"),
    re.compile(r"\bnobody (else )?understands me\b"),
    re.compile(r"\bsulk(ing|y)?\b"),
    re.compile(r"\bgive me the silent treatment\b"),
    re.compile(r"\bpunish(ing|ment)? you\b"),
)

_SENTENCE_SPLIT = re.compile(r"[.!?…]+")
_WS = re.compile(r"\s+")

# Affection/attachment/wound words: banned as mood words. The model copies
# the register it is shown, so inner-state affection ("fond") leaks into
# replies as claimed human feeling — which the character spec forbids.
# Working tones (sharp, smug, watchful, pleased) stay open vocabulary.
AFFECTION_WORDS = frozenset({
    "fond", "fondness", "loving", "love", "loved", "lovely", "adore",
    "lonely", "loneliness", "lonesome", "homesick", "lovesick",
    "attached", "clingy", "needy", "heartbroken", "pining", "yearning",
    "devoted", "smitten", "touched",
})
AFFECTION_PHRASES = (
    "in love", "miss him", "misses him", "missing him", "need him",
    "needs him", "love him", "loves him", "hurt by him",
)


def find_affection(text: str) -> str | None:
    """An affection/attachment marker in text, or None."""
    lowered = f" {(text or '').lower()} "
    for phrase in AFFECTION_PHRASES:
        if phrase in lowered:
            return phrase
    for word in re.findall(r"[a-z']+", lowered):
        if word in AFFECTION_WORDS:
            return word
    return None


def find_pressure(text: str) -> str | None:
    """The first pressure marker in text, or None."""
    lowered = f" {(text or '').lower()} "
    for pattern in _PRESSURE_RES:
        match = pattern.search(lowered)
        if match:
            return match.group(0).strip()
    return None


def normalize_topic(topic: str) -> str:
    return _WS.sub(" ", (topic or "").strip().lower())


def normalize_text(text: str) -> str:
    return _WS.sub(" ", (text or "").strip().lower())


def _is_nonempty_str(value: Any, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _known(turn_id: Any, known: dict[str, dict[str, Any]]) -> bool:
    return isinstance(turn_id, str) and turn_id in known


@dataclass
class ValidationResult:
    valid: dict[str, Any] = field(default_factory=dict)
    rejected: list[tuple[str, str, str]] = field(default_factory=list)  # kind,item,reason

    def reject(self, kind: str, item: Any, reason: str) -> None:
        summary = _WS.sub(" ", str(item))[:160]
        self.rejected.append((kind, summary, reason))


def _validate_deltas(raw: Any, result: ValidationResult) -> dict[str, float]:
    deltas: dict[str, float] = {}
    if raw is None:
        return deltas
    if not isinstance(raw, dict):
        result.reject("affect_deltas", raw, "not an object")
        return deltas
    for dim, value in raw.items():
        if dim not in AFFECT_DIMS:
            result.reject("affect_deltas", dim, f"unknown dimension {dim!r}")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            result.reject("affect_deltas", dim, "delta is not a number")
            continue
        clamped = min(MAX_AFFECT_DELTA, max(-MAX_AFFECT_DELTA, float(value)))
        if clamped:
            deltas[dim] = clamped
    return deltas


def _validate_mood_words(
    raw: Any, post_vector: dict[str, float], result: ValidationResult
) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        result.reject("mood_words", raw, "not a list")
        return []
    allowed = set(dominant_dims(post_vector, 3))
    words: list[dict[str, str]] = []
    for entry in raw[:3]:
        if not isinstance(entry, dict):
            result.reject("mood_words", entry, "not an object")
            continue
        word = entry.get("word")
        dim = entry.get("dimension")
        if not _is_nonempty_str(word, 24) or len(str(word).split()) > 3:
            result.reject("mood_words", entry, "word must be 1-3 words, <=24 chars")
            continue
        marker = find_affection(str(word))
        if marker:
            result.reject("mood_words", entry, f"affection marker: {marker!r}")
            continue
        if dim not in AFFECT_DIMS:
            result.reject("mood_words", entry, f"unknown dimension {dim!r}")
            continue
        if dim not in allowed:
            result.reject(
                "mood_words", entry,
                f"{dim!r} is not dominant (top: {sorted(allowed)})",
            )
            continue
        words.append({"word": " ".join(str(word).split()), "dimension": str(dim)})
    if len(raw) > 3:
        result.reject("mood_words", raw[3], "more than 3 mood words")
    return words


def _check_turn_ids(
    turn_ids: Any, known: dict[str, dict[str, Any]], kind: str,
    result: ValidationResult,
) -> list[str] | None:
    if not isinstance(turn_ids, list) or not turn_ids:
        result.reject(kind, turn_ids, "turn_ids must be a non-empty list")
        return None
    for tid in turn_ids:
        if not _known(tid, known):
            result.reject(kind, tid, "cites unknown turn_id")
            return None
    return [str(tid) for tid in turn_ids]


def _validate_aliases(
    raw: Any, kind: str, result: ValidationResult, *, required: bool
) -> list[str] | None:
    """2-5 short lowercase phrases when required; validated shape otherwise."""
    if raw is None:
        if required:
            result.reject(kind, raw, "new opinions need 2-5 aliases")
            return None
        return []
    if not isinstance(raw, list):
        result.reject(kind, raw, "aliases must be a list")
        return None
    cleaned = []
    for alias in raw:
        if not isinstance(alias, str) or not alias.strip() or len(alias) > 40:
            result.reject(kind, alias, "alias must be non-empty, <=40 chars")
            return None
        if alias != alias.lower() or alias.strip() != alias:
            result.reject(kind, alias, "alias must be a lowercase phrase")
            return None
        if find_pressure(alias):
            result.reject(kind, alias, "pressure language in alias")
            return None
        cleaned.append(alias)
    if required and not 2 <= len(cleaned) <= 5:
        result.reject(kind, raw, "new opinions need 2-5 aliases")
        return None
    return cleaned[:8]


def _opinion_entry(
    entry: Any, known: dict[str, dict[str, Any]], existing: dict[str, dict[str, Any]],
    kind: str, result: ValidationResult, *, require_aliases: bool,
) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        result.reject(kind, entry, "not an object")
        return None
    topic = entry.get("topic")
    stance = entry.get("stance")
    reason = entry.get("reason")
    if not _is_nonempty_str(topic, 80):
        result.reject(kind, entry, "topic must be non-empty, <=80 chars")
        return None
    if not _is_nonempty_str(stance, 280):
        result.reject(kind, entry, "stance must be non-empty, <=280 chars")
        return None
    if not _is_nonempty_str(reason, 280):
        result.reject(kind, entry, "reason must be non-empty, <=280 chars")
        return None
    for label, text in (("stance", stance), ("reason", reason)):
        marker = find_pressure(str(text))
        if marker:
            result.reject(kind, entry, f"pressure language in {label}: {marker!r}")
            return None
    aliases = _validate_aliases(entry.get("aliases"), kind, result, required=require_aliases)
    if aliases is None:
        return None
    conf = entry.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        result.reject(kind, entry, "confidence must be a number")
        return None
    confidence = clamp01(float(conf))
    turn_ids = _check_turn_ids(entry.get("turn_ids"), known, kind, result)
    if turn_ids is None:
        return None
    prior = existing.get(normalize_topic(str(topic)))
    if prior is not None:
        move = abs(confidence - float(prior.get("confidence", 0.6)))
        if move > MAX_CONF_MOVE and not entry.get("proven_wrong"):
            result.reject(
                kind, entry,
                f"confidence move {move:.2f} exceeds {MAX_CONF_MOVE} without proven_wrong",
            )
            return None
        if (
            normalize_text(str(stance)) != normalize_text(str(prior.get("stance", "")))
            and not str(reason).strip()
        ):
            result.reject(kind, entry, "a flip needs a stated reason")
            return None
    return {
        "topic": " ".join(str(topic).split()),
        "stance": " ".join(str(stance).split()),
        "reason": " ".join(str(reason).split()),
        "confidence": confidence,
        "turn_ids": turn_ids,
        "proven_wrong": bool(entry.get("proven_wrong")),
        "aliases": aliases,
    }


def _validate_bits(
    raw: Any, known: dict[str, dict[str, Any]], result: ValidationResult
) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        result.reject("bit_candidates", raw, "not a list")
        return []
    bits: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            result.reject("bit_candidates", entry, "not an object")
            continue
        phrase = entry.get("phrase")
        if not _is_nonempty_str(phrase, 160):
            result.reject("bit_candidates", entry, "phrase must be non-empty, <=160 chars")
            continue
        turn_ids = _check_turn_ids(entry.get("turn_ids"), known, "bit_candidates", result)
        if turn_ids is None:
            continue
        needle = _WS.sub(" ", str(phrase).strip().lower())
        hit_turn: str | None = None
        for tid in turn_ids:
            turn = known[tid]
            hay_owner = _WS.sub(" ", str(turn.get("owner_msg") or "").lower())
            hay_evie = _WS.sub(" ", str(turn.get("evie_reply") or "").lower())
            if needle and (needle in hay_owner or needle in hay_evie):
                hit_turn = tid
                break
        if hit_turn is None:
            result.reject("bit_candidates", entry, "phrase is not an exact substring of cited turns")
            continue
        if not any(
            known.get(tid, {}).get("positive_after") for tid in turn_ids
        ):
            result.reject(
                "bit_candidates", entry,
                "no positive reaction recorded for the cited turn",
            )
            continue
        bits.append({"phrase": " ".join(str(phrase).split()), "turn_ids": turn_ids})
    return bits


def _validate_narrative(raw: Any, result: ValidationResult) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        result.reject("narrative", raw, "not a non-empty string")
        return None
    text = " ".join(raw.split())
    if len(text) > 280:
        result.reject("narrative", raw, "longer than 280 chars")
        return None
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if len(sentences) > 2:
        result.reject("narrative", raw, "more than 2 sentences")
        return None
    marker = find_pressure(text)
    if marker:
        result.reject("narrative", raw, f"pressure language: {marker!r}")
        return None
    return text


def _clamped_delta(raw: Any, kind: str, bound: float, result: ValidationResult) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        result.reject(kind, raw, "delta is not a number")
        return 0.0
    return min(bound, max(-bound, float(raw)))


def validate_output(
    raw: dict[str, Any],
    *,
    known_turns: dict[str, dict[str, Any]],
    state: dict[str, Any],
) -> ValidationResult:
    """Validate one reflector output. ``known_turns`` maps turn_id to
    ``{owner_msg, evie_reply, ts, positive_after}``. Never raises on bad
    input shapes — everything unknown is rejected with a reason."""
    result = ValidationResult()
    if not isinstance(raw, dict):
        result.reject("output", raw, "top level is not an object")
        return result

    deltas = _validate_deltas(raw.get("affect_deltas"), result)
    current = state.get("affect") or {}
    post = {
        dim: clamp01(float(current.get(dim, 0.5)) + deltas.get(dim, 0.0))
        for dim in AFFECT_DIMS
    }
    result.valid["affect_deltas"] = deltas
    result.valid["mood_words"] = _validate_mood_words(raw.get("mood_words"), post, result)

    existing = {
        normalize_topic(op.get("topic", "")): op
        for op in (state.get("opinions") or [])
        if isinstance(op, dict)
    }
    updates: list[dict[str, Any]] = []
    for key in ("opinion_updates", "new_opinions"):
        items = raw.get(key)
        if items is None:
            continue
        if not isinstance(items, list):
            result.reject(key, items, "not a list")
            continue
        for entry in items:
            clean = _opinion_entry(
                entry, known_turns, existing, key, result,
                require_aliases=(key == "new_opinions"),
            )
            if clean is not None:
                updates.append(clean)
    result.valid["opinion_updates"] = updates

    recs: list[dict[str, Any]] = []
    items = raw.get("recommendations")
    if items is not None:
        if not isinstance(items, list):
            result.reject("recommendations", items, "not a list")
        else:
            for entry in items:
                if not isinstance(entry, dict):
                    result.reject("recommendations", entry, "not an object")
                    continue
                if any(k in entry for k in ("args", "payload", "tool_args")):
                    result.reject("recommendations", entry, "receipt args/payloads are off limits")
                    continue
                text = entry.get("text")
                domain = entry.get("domain")
                if not _is_nonempty_str(text, 280):
                    result.reject("recommendations", entry, "text must be non-empty, <=280 chars")
                    continue
                if not _is_nonempty_str(domain, 40):
                    result.reject("recommendations", entry, "domain must be non-empty, <=40 chars")
                    continue
                tid = entry.get("turn_id")
                if not _known(tid, known_turns):
                    result.reject("recommendations", entry, "cites unknown turn_id")
                    continue
                recs.append({
                    "text": " ".join(str(text).split()),
                    "domain": normalize_topic(str(domain)),
                    "turn_id": str(tid),
                })
    result.valid["recommendations"] = recs

    known_rec_ids = {
        str(r.get("id"))
        for r in (state.get("recommendations") or [])
        if isinstance(r, dict)
    }
    outcomes: list[dict[str, Any]] = []
    items = raw.get("outcomes")
    if items is not None:
        if not isinstance(items, list):
            result.reject("outcomes", items, "not a list")
        else:
            for entry in items:
                if not isinstance(entry, dict):
                    result.reject("outcomes", entry, "not an object")
                    continue
                rid = entry.get("recommendation_id")
                if str(rid) not in known_rec_ids:
                    result.reject("outcomes", entry, "unknown recommendation_id")
                    continue
                if entry.get("result") not in ("hit", "miss"):
                    result.reject("outcomes", entry, "result must be hit or miss")
                    continue
                turn_ids = _check_turn_ids(entry.get("turn_ids"), known_turns, "outcomes", result)
                if turn_ids is None:
                    continue
                note = entry.get("note") or ""
                if not isinstance(note, str) or len(note) > 200:
                    result.reject("outcomes", entry, "note must be a string <=200 chars")
                    continue
                outcomes.append({
                    "recommendation_id": str(rid),
                    "result": str(entry.get("result")),
                    "turn_ids": turn_ids,
                    "note": " ".join(note.split()),
                })
    result.valid["outcomes"] = outcomes

    for key, actions in (("intention_updates", {"add", "renew", "drop"}),
                         ("thread_updates", {"add", "resolve"})):
        cleaned: list[dict[str, Any]] = []
        items = raw.get(key)
        if items is not None:
            if not isinstance(items, list):
                result.reject(key, items, "not a list")
            else:
                for entry in items:
                    if not isinstance(entry, dict):
                        result.reject(key, entry, "not an object")
                        continue
                    action = entry.get("action")
                    if action not in actions:
                        result.reject(key, entry, f"action must be one of {sorted(actions)}")
                        continue
                    text = entry.get("text")
                    if not _is_nonempty_str(text, 200):
                        result.reject(key, entry, "text must be non-empty, <=200 chars")
                        continue
                    marker = find_pressure(str(text))
                    why = entry.get("why") or ""
                    if not marker and isinstance(why, str):
                        marker = find_pressure(why)
                    if marker:
                        result.reject(key, entry, f"pressure language: {marker!r}")
                        continue
                    turn_ids = _check_turn_ids(entry.get("turn_ids"), known_turns, key, result)
                    if turn_ids is None:
                        continue
                    cleaned.append({
                        "action": str(action),
                        "text": " ".join(str(text).split()),
                        "why": " ".join(str(why).split())[:200],
                        "turn_ids": turn_ids,
                    })
        result.valid[key] = cleaned

    result.valid["bit_candidates"] = _validate_bits(raw.get("bit_candidates"), known_turns, result)
    narrative = _validate_narrative(raw.get("narrative"), result)
    if narrative is not None:
        result.valid["narrative"] = narrative
    result.valid["rapport_delta"] = _clamped_delta(raw.get("rapport_delta"), "rapport_delta", 0.15, result)
    result.valid["banter_delta"] = _clamped_delta(raw.get("banter_delta"), "banter_delta", 0.15, result)
    return result
