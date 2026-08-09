"""Sensitive voice command classification.

An unlocked owner session is enough for casual chat and everyday actions, but
destructive memory operations, identity/voice changes, and external writes
require a fresh, purpose-bound re-verification proof (plan §16.2 / §5.3).
"""

from __future__ import annotations

import re

SENSITIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(delete|forget|erase|wipe|clear|remove)\b.*\b"
            r"(memory|memories|everything|history|all data|all of it)\b",
            re.IGNORECASE,
        ),
        "memory.delete",
    ),
    (
        re.compile(
            r"\b(revoke|delete|remove|disable)\b.*\b(voice|voiceprint|enrollment)\b",
            re.IGNORECASE,
        ),
        "voice.delete",
    ),
    (
        re.compile(
            r"\b(send|transfer|pay|spend|purchase|buy|post|publish|tweet|email|message|"
            r"execute|run|deploy)\b.*\b(money|payment|funds|crypto|external|message|email|"
            r"tweet|post|command|deploy)\b",
            re.IGNORECASE,
        ),
        "external.write",
    ),
]

REVERIFY_PURPOSE = "voice.sensitive_action"


def classify_sensitive(text: str) -> str | None:
    """Return the sensitive-action purpose for a transcript, or None."""
    lowered = text.lower()
    for pattern, purpose in SENSITIVE_PATTERNS:
        if pattern.search(lowered):
            return purpose
    return None
