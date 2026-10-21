"""Resume registry: "resume X" resolves to a saved layout (WP4).

Generic pattern table: aliases map shorthand to layout names, and any saved
layout is addressable by exact or prefix name. Layouts live in the terminal
store; this module only resolves names, never invents specs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ALIASES: dict[str, str] = {
    "standup": "standup",
    "morning": "standup",
    "evie": "evie-dev",
    "dev": "evie-dev",
}

_ALIAS_FILE = ".ev-resume-aliases.json"


def _alias_path(root) -> Path:
    return Path(str(root)) / ".ev-layouts" / _ALIAS_FILE


def aliases(root=None) -> dict[str, str]:
    merged = dict(ALIASES)
    try:
        raw = json.loads(_alias_path(root or ".").read_text())
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(key, str) and isinstance(value, str):
                    merged[key.strip().lower()] = value.strip()
    except (OSError, ValueError):
        pass
    return merged


def set_alias(name: str, layout: str, root=None) -> dict:
    name = (name or "").strip().lower()
    layout = (layout or "").strip()
    if not name or not layout or "/" in name:
        return {"ok": False, "verified": True, "evidence": {},
                "error": "bad alias", "undo": None}
    try:
        path = _alias_path(root or ".")
        path.parent.mkdir(parents=True, exist_ok=True)
        current: dict[str, Any] = {}
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                current = raw
        except (OSError, ValueError):
            pass
        current[name] = layout
        path.write_text(json.dumps(current))
        back = json.loads(path.read_text())
        ok = back.get(name) == layout
        return {"ok": ok, "verified": ok, "evidence": {"alias": name, "layout": layout},
                "error": None if ok else "read-back mismatch", "undo": None}
    except OSError as e:
        return {"ok": False, "verified": True, "evidence": {},
                "error": str(e), "undo": None}


def resolve(text: str, root=None) -> dict:
    """Resolve 'resume X' / bare 'X' to a saved layout name or a no-match."""
    from . import terminal as term

    words = (text or "").strip().lower()
    for prefix in ("resume ", "restore ", "open layout "):
        if words.startswith(prefix):
            words = words[len(prefix):].strip()
    if not words:
        return {"ok": False, "verified": True, "evidence": {},
                "error": "nothing to resume", "undo": None}
    table = aliases(root)
    if words in table:
        words = table[words]
    saved = term.list_layouts(root=root)
    names = [row["name"] for row in saved.get("layouts", [])] if saved.get("ok") else []
    if words in names:
        return {"ok": True, "verified": True,
                "evidence": {"layout": words, "match": "exact"}, "error": None,
                "undo": None}
    prefixed = [n for n in names if n.startswith(words)]
    if len(prefixed) == 1:
        return {"ok": True, "verified": True,
                "evidence": {"layout": prefixed[0], "match": "prefix"},
                "error": None, "undo": None}
    if len(prefixed) > 1:
        return {"ok": False, "verified": True,
                "evidence": {"candidates": sorted(prefixed)},
                "error": "ambiguous", "undo": None}
    return {"ok": False, "verified": True, "evidence": {"known": sorted(names)},
            "error": "unknown layout", "undo": None}
