"""Automatic PII/secret classification at ingestion time.

The chat input filter guards the model boundary, but stored observations
(raw events and live events) can enter through other APIs.  This module is the
deterministic, dependency-free classifier applied at ingestion: content that
looks like credentials/cards/SSNs is escalated to ``never_send_to_model`` and
content with emails/phones is escalated to ``sensitive`` so the model-facing
slices exclude it by default.  Classification only ever *raises* privacy; it
is recorded in event metadata for auditability.
"""

from __future__ import annotations

import re

PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE), "email"),
    (
        re.compile(
            r"(?<!\d)(?:\+?\d{1,3}[ -.]?)?\(?\d{2,4}\)?[ -.]?\d{3}[ -.]?\d{4}(?!\d)"
        ),
        "phone",
    ),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
    (re.compile(r"\b(?:[0-9][ -]?){13,19}\b"), "card_number"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "api_key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "api_key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "api_key"),
    (
        re.compile(
            r"\b(?:api[_-]?key|secret|password|passwd|token)\s*(?:[=:]|"
            r"(?:is|was))\s*\S{8,}\b",
            re.IGNORECASE,
        ),
        "credential",
    ),
]

NEVER_SEND_CATEGORIES = {"api_key", "credential", "card_number", "ssn"}
SENSITIVE_CATEGORIES = {"email", "phone"}

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}\b"
)
_DATETIME_RE = re.compile(
    r"(?<![\d-])\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"(?![\d-])"
)
_HEX_ID_RE = re.compile(
    r"\b(?=[0-9A-Fa-f]*[A-Fa-f])(?=[0-9A-Fa-f]*[0-9])[0-9A-Fa-f]{8,}\b"
)


def _scrub_structural(text: str) -> str:
    """Remove UUIDs, hex identifiers, and ISO dates/times before scanning."""
    scrubbed = _UUID_RE.sub(" ", text)
    scrubbed = _HEX_ID_RE.sub(" ", scrubbed)
    return _DATETIME_RE.sub(" ", scrubbed)


def classify_pii(*texts: str | None) -> list[str]:
    """Return the PII categories found across the given strings (ordered)."""
    categories: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        scrubbed = _scrub_structural(text)
        for pattern, label in PII_PATTERNS:
            if label in seen:
                continue
            if pattern.search(scrubbed):
                categories.append(label)
                seen.add(label)
    return categories


def escalate_privacy(privacy_level: str, categories: list[str]) -> str:
    """Raise (never lower) privacy based on detected PII categories."""
    if any(category in NEVER_SEND_CATEGORIES for category in categories):
        return "never_send_to_model"
    if (
        any(category in SENSITIVE_CATEGORIES for category in categories)
        and privacy_level != "never_send_to_model"
    ):
        return "sensitive"
    return privacy_level
