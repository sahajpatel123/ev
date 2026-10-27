"""Name-ban test: no real-person names or borrowed-voice markers.

The denylist lives ONLY in this file — never in the package.
"""

from __future__ import annotations

import re
from pathlib import Path

PERSONA_DIR = Path(__file__).resolve().parent.parent / "app" / "persona"

# Distinctive surnames of famous stage performers, plus the generic marker.
# Common-word surnames are deliberately excluded to avoid false positives.
DENYLIST = (
    "carlin",
    "pryor",
    "hedberg",
    "chapelle",
    "gervais",
    "mulaney",
    "burr",
    "seinfeld",
    "dangerfield",
    "kaufman",
    "hicks",
    "comedian",
    "standup",
    "stand-up",
)

_PATTERNS = tuple(re.compile(r"\b" + re.escape(word) + r"\b") for word in DENYLIST)


def _haystacks() -> list[Path]:
    paths = list(PERSONA_DIR.glob("*.py"))
    paths.append(PERSONA_DIR / "spec" / "CHARACTER.md")
    paths.append(PERSONA_DIR / "exemplars.yaml")
    return paths


def test_no_denylisted_names_in_package_or_spec_copy() -> None:
    offenders: list[str] = []
    for path in _haystacks():
        assert path.exists(), path
        text = path.read_text(encoding="utf-8").lower()
        for word, pattern in zip(DENYLIST, _PATTERNS, strict=True):
            if pattern.search(text):
                offenders.append(f"{path.name}: {word}")
    assert offenders == []
