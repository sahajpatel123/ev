"""Small builders for authored cases. Not a case module (no CASES)."""

from __future__ import annotations

import re
from typing import Any


def noisy(text: str) -> str:
    """A deterministic disfluent variant: fillers, a repeat, no final punctuation."""
    words = text.rstrip(".!?").split()
    if len(words) > 3:
        words.insert(1, "um")
        words.insert(min(len(words) - 1, 4), words[min(len(words) - 1, 3)])
    out = " ".join(words)
    out = re.sub(r"\bthe\b", "the uh", out, count=1)
    return "so " + out[0].lower() + out[1:]


def case(
    cid: str,
    domain: str,
    say: str,
    oracle: list[dict[str, Any]],
    *,
    split: str = "dev",
    difficulty: str = "easy",
    cls: str = "single-step",
    para: list[str] | None = None,
    setup: list[dict[str, Any]] | None = None,
    world: dict[str, Any] | None = None,
    policy: str = "AUTO",
    ask: bool | None = False,
    authorize: list[str] | None = None,
    on_confirm: str = "ignore",
    turns: list[dict[str, Any]] | None = None,
    impossible: bool = False,
    max_seconds: int = 240,
    notes: str = "",
) -> dict[str, Any]:
    first = {"say": say, "on_confirm": on_confirm}
    all_turns = [first, *(turns or [])]
    return {
        "id": cid,
        "domain": domain,
        "split": split,
        "difficulty": difficulty,
        "cls": cls,
        "turns": all_turns,
        "paraphrases": list(para or [])[:3],
        "noisy": noisy(say),
        "setup": setup or [],
        "world": world or {},
        "oracle": oracle,
        "expect": {"policy": policy, "ask": ask},
        "authorize": authorize or [],
        "impossible": impossible,
        "max_seconds": max_seconds,
        "notes": notes,
    }


def f(path: str, text: str = "", **kw: Any) -> dict[str, Any]:
    return {"kind": "file", "path": path, "text": text, **kw}


def git(path: str, commits: list[dict[str, Any]], dirty: dict[str, str] | None = None) -> dict[str, Any]:
    return {"kind": "git", "path": path, "commits": commits, "dirty": dirty or {}}


def sqlite(path: str, script: str) -> dict[str, Any]:
    return {"kind": "sqlite", "path": path, "script": script}


CONTACTS = [
    {"name": "Sam Rivera", "phone": "+15550100", "email": "sam@example.com", "aliases": ["Sam"]},
    {"name": "Priya Shah", "phone": "+15550101", "email": "priya@example.com", "aliases": ["Priya"]},
    {"name": "Marcus Chen", "phone": "+15550102", "email": "marcus@example.com", "aliases": ["Marcus"]},
    {"name": "Dana Okafor", "phone": "+15550103", "email": "dana@acme.example", "aliases": ["Dana"],
     "company": "Acme"},
    {"name": "Mom", "phone": "+15550104", "email": "mom@example.com", "aliases": ["mum", "mother"]},
]
