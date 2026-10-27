"""Cliché lexicon loader (§3.1). Data lives in cliches.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

CLICHES_PATH = Path(__file__).with_name("cliches.yaml")


@lru_cache(maxsize=1)
def load() -> dict[str, tuple[str, ...]]:
    """Load the lexicon (cached; the file never changes at runtime)."""
    raw = yaml.safe_load(CLICHES_PATH.read_text(encoding="utf-8")) or {}
    return {
        key: tuple(str(item) for item in (raw.get(key) or []))
        for key in ("openers", "fillers", "stock_humour")
    }


def all_phrases() -> tuple[str, ...]:
    """Every phrase, longest first (so matching prefers specifics)."""
    lexicon = load()
    phrases = list(lexicon["openers"] + lexicon["fillers"] + lexicon["stock_humour"])
    seen: set[str] = set()
    unique: list[str] = []
    for phrase in phrases:
        if phrase not in seen:
            seen.add(phrase)
            unique.append(phrase)
    return tuple(sorted(unique, key=len, reverse=True))
