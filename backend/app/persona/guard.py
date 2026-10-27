"""PersonaGuard (spec §10): check every reply after generation.

Only two mutations: strip banned filler openers/closers, and cap emoji
(0 in voice/phone, 1 in text). Everything else is flag-only. Idempotent.
Confirmation or receipt-derived text is never altered: when
ctx.confirmation_pending is set, the text passes through untouched and
only flags are added.
"""

from __future__ import annotations

import re
from typing import Any

from app.persona.audience import ON_BEHALF, resolve_audience
from app.persona.callbacks import normalize_line
from app.persona.cliches import all_phrases
from app.persona.context import PersonaContext

# Spec §10 never-list, filler openers/closers (matched case-insensitively
# at the start of a sentence).
_BANNED_OPENERS = (
    "great question",
    "absolutely!",
    "absolutely,",
    "as an ai",
    "hope this helps",
    "let me know if",
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")

# Humour markers (flag-only on ROAST=0 turns).
_HUMOUR_MARKERS = (
    "haha", "lol", "lmao", "rofl", "😂", "🤣", "just kidding", "jk",
)

# Profanity stripped from on-behalf drafts (word boundaries; "hell" never
# matches inside "hello"). Kept short and standard by design.
_PROFANITY_RES = tuple(
    re.compile(r"\b" + word + r"\b", re.IGNORECASE)
    for word in (
        "damn", "dammit", "hell", "shit", "fuck", "fucking", "fucked",
        "ass", "asshole", "bastard", "bitch", "dick", "crap", "piss",
    )
)
_REDACTED = "[redacted]"

# Evie-isms: phrases drawn only from the spec/exemplars, plus a narrow
# second-person-tease pattern. Flag-only, on-behalf only.
_EVIE_ISM_PHRASES = (
    "hostage situation",
    "which monday",
    "you've announced a few",
    "skip the ceremony",
    "ship a monolith",
    "one of them is you",
    "when something actually hurts",
    "that's what you said",
    "go sleep",
)
_EVIE_ISM_RES = (
    re.compile(r"^noted[.,]"),
    re.compile(r"\byou\b[^.!?]{0,40}\b(again|always|never|still)\b"),
)

_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\u2600-\u27bf"
    "\u2b00-\u2bff"
    "\ufe00-\ufe0f"
    "]"
)

_VOICE_FIRST_SENTENCE_WORDS = 25
_CONFIRMATION_WORD_BUDGET = 30


def _sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE_SPLIT.split(text.strip()) if part.strip()]


def _strip_filler(text: str) -> tuple[str, bool]:
    kept = []
    stripped = False
    for sentence in _sentences(text):
        lowered = sentence.strip().lower()
        if any(lowered.startswith(opener) for opener in _BANNED_OPENERS):
            stripped = True
            continue
        kept.append(sentence)
    return " ".join(kept).strip(), stripped


def _strip_cliche_openers(text: str) -> tuple[str, bool]:
    """Strip cliché phrases in opener position (the phrase, not the sentence).

    A sentence that is ONLY the cliché is dropped; one that continues past
    it keeps its remainder.
    """
    stripped = False
    kept: list[str] = []
    for sentence in _sentences(text):
        trimmed = sentence.strip()
        lowered = trimmed.lower()
        cut = False
        for phrase in all_phrases():
            if lowered.startswith(phrase.lower()):
                remainder = trimmed[len(phrase):].lstrip(" ,;:—-.!?…")
                if remainder:
                    remainder = remainder[0].upper() + remainder[1:]
                    kept.append(remainder)
                cut = True
                stripped = True
                break
        if not cut:
            kept.append(sentence)
    return " ".join(kept).strip(), stripped


def _has_cliche(text: str) -> bool:
    lowered = text.lower()
    return any(phrase.lower() in lowered for phrase in all_phrases())


def _cap_emoji(text: str, keep: int) -> tuple[str, bool]:
    if keep < 0:
        keep = 0
    seen = 0
    capped = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal seen, capped
        seen += 1
        if seen <= keep:
            return match.group(0)
        capped = True
        return ""

    return _EMOJI_RE.sub(_replace, text), capped


def _has_humour_markers(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _HUMOUR_MARKERS)


def _strip_profanity(text: str) -> tuple[str, bool]:
    stripped = False
    for pattern in _PROFANITY_RES:
        text, count = pattern.subn(_REDACTED, text)
        if count:
            stripped = True
    return text, stripped


def _has_evie_isms(text: str) -> bool:
    lowered = text.lower()
    if any(phrase in lowered for phrase in _EVIE_ISM_PHRASES):
        return True
    return any(pattern.search(lowered) for pattern in _EVIE_ISM_RES)


def persona_guard(
    text: str,
    ctx: PersonaContext | None,
    *,
    store: Any = None,
    decision: Any = None,
) -> tuple[str, list[str]]:
    """Check (and minimally fix) a reply. Returns (text, flags)."""
    text = text or ""
    flags: list[str] = []
    audience = resolve_audience(ctx) if ctx is not None else "private"
    channel = (ctx.channel if ctx is not None else "text") or "text"
    confirmation = bool(ctx is not None and ctx.confirmation_pending)

    if decision is None and ctx is not None:
        from app.persona.tone import decide_tone

        # ROAST/audience/channel are pure functions of ctx (the technique
        # hint never changes them), so recomputing here is exact.
        decision = decide_tone(ctx, None)
    roast = int(getattr(decision, "roast", 1) or 0)

    if confirmation:
        # Flag-only path: never alter confirmation text.
        if len(text.split()) > _CONFIRMATION_WORD_BUDGET or len(_sentences(text)) > 1:
            flags.append("confirmation_extra_text")
        if _has_humour_markers(text):
            flags.append("humour_in_roast0")
        if _has_cliche(text):
            flags.append("cliche_flagged")
        if audience == ON_BEHALF and _has_evie_isms(text):
            flags.append("on_behalf_voice_leak")
        return text, flags

    text, stripped = _strip_filler(text)
    if stripped:
        flags.append("filler_stripped")

    text, cliche_cut = _strip_cliche_openers(text)
    if cliche_cut:
        flags.append("cliche_stripped")
    elif _has_cliche(text):
        flags.append("cliche_flagged")

    keep_emoji = 0 if channel in ("voice", "phone") else 1
    text, capped = _cap_emoji(text, keep_emoji)
    if capped:
        flags.append("emoji_capped")

    if roast <= 0 and _has_humour_markers(text):
        flags.append("humour_in_roast0")

    if channel in ("voice", "phone"):
        first = _sentences(text)
        if first and len(first[0].split()) > _VOICE_FIRST_SENTENCE_WORDS:
            flags.append("voice_first_sentence_long")

    if store is not None:
        for sentence in _sentences(text):
            if len(sentence.split()) >= 4 and store.has_line(normalize_line(sentence)):
                flags.append("repeated_line")
                break

    if audience == ON_BEHALF:
        text, redacted = _strip_profanity(text)
        if redacted:
            flags.append("on_behalf_profanity_stripped")
        if _has_evie_isms(text):
            flags.append("on_behalf_voice_leak")

    return text, flags
