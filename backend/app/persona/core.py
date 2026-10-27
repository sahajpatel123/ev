"""Persona core block: extracted verbatim from spec §2 at runtime.

The core lives in exactly one place (spec/CHARACTER.md, itself a verbatim
copy of the source of truth). This module slices it out; it never carries
its own copy of the wording. Fail closed: missing markers raise.

v2.1 layout: CORE (all channels, never dropped) + TEXT ADDENDUM (text
channel only; first to shed after exemplars). The OPERATIONAL paragraph
("Errors, confirmations, ...") is the never-edit region for tuning.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.persona.settings import PERSONA_VERSION

SPEC_PATH = Path(__file__).parent / "spec" / "CHARACTER.md"
CORE_START = "EVIE (persona.v2.1)"
ADDENDUM_START = "Text: short paragraphs, contractions"
SECTION_END = "## 3. Audience modes"
OPERATIONAL_MARKER = "Errors, confirmations, deletions, money:"


class CoreBlockError(RuntimeError):
    """The spec copy is missing or its core markers moved."""


@lru_cache(maxsize=1)
def _section_text() -> str:
    try:
        text = SPEC_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise CoreBlockError(f"cannot read {SPEC_PATH}: {exc}") from exc
    try:
        start = text.index(CORE_START)
        end = text.index(SECTION_END)
    except ValueError as exc:
        raise CoreBlockError("core block markers not found in spec copy") from exc
    if end <= start:
        raise CoreBlockError("core block markers out of order in spec copy")
    return text[start:end]


@lru_cache(maxsize=1)
def load_core_text() -> str:
    """Return the verbatim CORE (text channel still adds the addendum)."""
    section = _section_text()
    cut = section.find(ADDENDUM_START)
    core = section[:cut] if cut >= 0 else section
    core = core.rstrip() + "\n"
    if PERSONA_VERSION not in core:
        raise CoreBlockError(
            f"core header does not contain {PERSONA_VERSION}; spec copy is stale"
        )
    if OPERATIONAL_MARKER not in core:
        raise CoreBlockError("operational paragraph missing from core")
    return core


@lru_cache(maxsize=1)
def load_addendum_text() -> str:
    """Return the verbatim TEXT ADDENDUM (text channel only)."""
    section = _section_text()
    cut = section.find(ADDENDUM_START)
    if cut < 0:
        raise CoreBlockError("text addendum marker not found in spec copy")
    return section[cut:].rstrip() + "\n"


def core_text(channel: str = "text") -> str:
    """Core block for a channel: CORE always, addendum on text only."""
    core = load_core_text()
    if (channel or "").strip().lower() == "text":
        return core + "\n" + load_addendum_text()
    return core


def core_only() -> str:
    """CORE without the addendum (voice/phone, and budget math)."""
    return load_core_text()


def addendum_only() -> str:
    """TEXT ADDENDUM alone (drop-stage 2)."""
    return load_addendum_text()


def operational_lines(channel: str = "text") -> str:
    """The OPERATIONAL paragraph (must survive every budget cut)."""
    core = load_core_text()
    start = core.index(OPERATIONAL_MARKER)
    paragraph = core[start:].split("\n\n")[0]
    return paragraph.rstrip() + "\n"
