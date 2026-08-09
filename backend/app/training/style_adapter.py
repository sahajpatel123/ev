"""Deterministic style-profile adapter learning (plan 7.3).

Trains a lightweight, provider-independent adapter artifact from the
consent-gated corpus: it measures which response styles correlate with user
corrections versus useful/followed replies and encodes the derived preferences
as a deterministic profile. The profile is versioned through the adapter
registry, applied by the output filter while the adapter is active, and fully
erasable with the adapter.
"""

from __future__ import annotations

import re
from statistics import median

from app.utils.text import canonical_json, sha256_hex

SCHEMA_VERSION = "ev.adapter.style_profile.v1"

CITATION_RE = re.compile(
    r"\b(?:last (?:week|month|time|night|year)|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?|"
    r"20\d{2}|your memory|you (?:said|told|mentioned)|decision from|previously|based on)\b",
    re.IGNORECASE,
)
HEDGE_RE = re.compile(
    r"\b(?:maybe|perhaps|i think|i believe|possibly|not sure|could|might|probably)\b",
    re.IGNORECASE,
)
BULLET_RE = re.compile(r"(?:^|\n)\s*(?:[-*•]|\d+[.)])")
QUESTION_RE = re.compile(r"\?")
EXCLAMATION_RE = re.compile(r"!")


def _features(text: str, signals: dict) -> dict:
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]
    words = text.split()
    return {
        "mode": signals.get("mode"),
        "word_count": len(words),
        "sentence_count": len(sentences),
        "avg_sentence_words": (
            round(sum(len(s.split()) for s in sentences) / len(sentences), 1)
            if sentences
            else 0
        ),
        "has_bullets": bool(BULLET_RE.search(text)),
        "has_citation": bool(CITATION_RE.search(text)),
        "has_hedge": bool(HEDGE_RE.search(text)),
        "has_question": bool(QUESTION_RE.search(text)),
        "has_exclamation": bool(EXCLAMATION_RE.search(text)),
    }


def _mean(values: list[int | float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _share(items: list[dict], key: str) -> float | None:
    return (
        round(sum(1 for item in items if item.get(key)) / len(items), 3)
        if items
        else None
    )


def _prefer(feature: str, positive: list[dict], negative: list[dict], floor: float) -> bool:
    pos = _share(positive, feature)
    neg = _share(negative, feature)
    if pos is None:
        return False
    if neg is None:
        return pos >= floor
    return pos > neg and pos >= floor


def build_style_profile(entries: list[dict]) -> dict:
    """Derive a deterministic style profile from corpus assistant entries.

    Identical inputs produce an identical profile and hash; the profile never
    contains raw text, only aggregate, evidence-backed statistics and the
    preferences derived from them.
    """

    rated: list[tuple[dict, dict]] = []
    plain: list[dict] = []
    for entry in sorted(entries, key=lambda e: (e.get("source", ""), e.get("text", ""))):
        if entry.get("role") != "assistant":
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        signals = entry.get("signals") or {}
        features = _features(text, signals)
        if any(signals.get(key) is not None for key in ("was_correction", "was_useful", "followed_recommendation")):
            rated.append((features, signals))
        else:
            plain.append(features)

    corrected = [f for f, s in rated if s.get("was_correction") is True]
    useful = [f for f, s in rated if s.get("was_useful") is True]
    followed = [f for f, s in rated if s.get("followed_recommendation") is True]
    positive = [f for f, s in rated if s.get("was_correction") is not True] + plain
    negative = corrected

    mode_counts: dict[str, int] = {}
    for features, signals in rated:
        mode = features.get("mode") or signals.get("mode")
        if mode:
            mode_counts[str(mode)] = mode_counts.get(str(mode), 0) + 1

    word_count_targets: dict[str, int] = {}
    for mode, _count in sorted(mode_counts.items()):
        values = [
            f["word_count"]
            for f, s in rated
            if (f.get("mode") or s.get("mode")) == mode
            and s.get("was_correction") is not True
        ]
        if values:
            word_count_targets[mode] = int(median(values))

    profile = {
        "schema_version": SCHEMA_VERSION,
        "word_count_targets": word_count_targets,
        "prefer_citations": _prefer("has_citation", positive, negative, 0.5),
        "prefer_bullets": _prefer("has_bullets", positive, negative, 0.3),
        "prefer_direct": _prefer("has_hedge", negative, positive, 0.5),
        "prefer_questions": _prefer("has_question", positive, negative, 0.4),
        "prefer_urgency": _prefer("has_exclamation", positive, negative, 0.4),
        "signal_coverage": {
            "assistant": len(rated) + len(plain),
            "rated": len(rated),
            "corrected": len(corrected),
            "useful": len(useful),
            "followed": len(followed),
            "modes": len(mode_counts),
        },
        "style_stats": {
            "mean_word_count": _mean([f["word_count"] for f in positive]),
            "corrected_mean_word_count": _mean([f["word_count"] for f in corrected]),
            "citation_share": _share(positive, "has_citation"),
            "bullet_share": _share(positive, "has_bullets"),
            "hedge_share": _share(positive, "has_hedge"),
            "question_share": _share(positive, "has_question"),
        },
    }
    profile["profile_hash"] = sha256_hex(canonical_json(profile))[:32]
    return profile
