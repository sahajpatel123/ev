"""Foreground conversation vs background intelligence.

EV LIVE keeps the interaction fluid. DeepSeek, tools, search, and memory
retrieval are the brain — they run when the turn needs them, not on every
microphone frame. This module is the cheap, deterministic router that
decides whether a finished user turn is a quick conversational reply or
work that should be delegated while the live loop keeps listening (and
optionally speaking a short filler).
"""

from __future__ import annotations

import re

from app.voice.speech import choose_voice_filler

_LONG_REASONING = re.compile(
    r"\b(?:why did|why has|explain why|walk me through|compare|analyze|"
    r"what happened|what's going on with|what is going on with)\b",
    re.IGNORECASE,
)


def needs_deep_work(text: str) -> bool:
    """True when this turn should run as background intelligence.

    Cheap conversational replies (time, acknowledgements, short memory
    lookups the briefing already prefetched) stay in the foreground so
    time-to-first-audio stays small. Search, life-write tools, and
    multi-step "why" questions are delegated.
    """

    raw = (text or "").strip()
    if not raw:
        return False
    from app.ev.briefing import voice_needs_tools
    from app.ev.tool_select import SEARCH_WEB_RE
    from app.search.live import looks_world_knowledge

    if looks_world_knowledge(raw):
        return True
    if SEARCH_WEB_RE.search(raw):
        return True
    if voice_needs_tools(raw):
        return True
    return bool(_LONG_REASONING.search(raw) and len(raw.split()) >= 6)


def thinking_filler(text: str) -> str:
    """Spoken bridge while background intelligence runs."""

    return choose_voice_filler(text)
