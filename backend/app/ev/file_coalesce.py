"""Keep one owner file request on one path.

Muse often rephrases a single create/save as later "text list",
"desktop <kind> list", or "verify …" effects. Each of those used to parse as
a new uniquely-named write. This module is the general binder: destination and
process words are not new artifacts; a second distinct kind is.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_VERIFY_ONLY = re.compile(
    r"\b(?:verify|verif(?:ied|ication)|double[- ]?check|confirm(?:ed)?|"
    r"make sure|check that|read(?:\s+it)?\s+back|proof(?:-|\s*)read)\b",
    re.I,
)
_CREATE = re.compile(
    r"\b(?:create|make|write|save|start|build|draft|leave|drop|put|jot)\b",
    re.I,
)
_ANOTHER = re.compile(
    r"\b(?:another|a second|a third|two (?:separate\s+)?|both a|"
    r"as well as|plus a(?:n)?|"
    r"also (?:create|make|write|save|put|start)|"
    r"and (?:also )?(?:a|an|another)\s+(?:new\s+)?(?:\w+\s+){0,3}"
    r"(?:lists?|notes?|files?|check-?lists?))\b",
    re.I,
)
_FILL = re.compile(
    r"\b(?:add(?:ing)?|append|include|fill|expand|more items?|also put)\b",
    re.I,
)
_PATH_NOISE = frozenset(
    {
        "desktop",
        "documents",
        "downloads",
        "icloud",
        "text",
        "verify",
        "verified",
        "verification",
        "note",
        "notes",
        "list",
        "lists",
        "file",
        "files",
        "checklist",
        "todo",
        "evie",
        "the",
        "my",
        "a",
        "an",
        "new",
    }
)


def is_verify_only(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or not _VERIFY_ONLY.search(raw):
        return False
    return not (_FILL.search(raw) or _CREATE.search(raw))


def wants_more_content(text: str) -> bool:
    return bool(_FILL.search((text or "").strip()))


def looks_like_distinct_new_file(text: str, last_path: str | None = None) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if _ANOTHER.search(raw):
        return True
    new_label = _effect_label(raw)
    bound_label = label_from_path(last_path) if last_path else ""
    return bool(new_label and bound_label and not kinds_compatible(new_label, bound_label))


def label_from_path(path: str | None) -> str:
    stem = Path(str(path or "")).stem.lower()
    parts = [part for part in re.split(r"[-_\s]+", stem) if part and part not in _PATH_NOISE]
    keep = [part for part in parts if not part.isdigit()]
    return " ".join(keep)


def kinds_compatible(left: str, right: str) -> bool:
    a = _normalize_kind(left)
    b = _normalize_kind(right)
    if not a or not b:
        return True
    if a == b:
        return True
    if a in b or b in a:
        return True
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if a_tokens and a_tokens <= b_tokens:
        return True
    return bool(b_tokens and b_tokens <= a_tokens)


def is_rephrase_create(text: str, last_path: str | None = None) -> bool:
    raw = (text or "").strip()
    if not raw or looks_like_distinct_new_file(raw, last_path):
        return False
    if is_verify_only(raw):
        return True
    if not _CREATE.search(raw) and not re.search(r"\b(?:lists?|notes?|files?)\b", raw, re.I):
        return False
    label = _effect_label(raw)
    bound = label_from_path(last_path) if last_path else ""
    if not label:
        return bool(last_path)
    return kinds_compatible(label, bound)


def already_written_goal(path: str, text: str = "") -> dict[str, Any]:
    target = str(path or "").strip()
    name = Path(target).name if target else "that file"
    return {
        "action": "read",
        "path": target,
        "query": name,
        "goal": text or f"read {name}",
        "receipt": "already_written",
        "unique_name": False,
        "overwrite": False,
        "artifact_complete": True,
    }


def coalesce_file_goal(
    parsed: dict[str, Any] | None,
    *,
    last_path: str | None = None,
    text: str = "",
) -> dict[str, Any] | None:
    """Retarget or skip a parsed write so it cannot mint a sibling file."""

    bound = str(last_path or "").strip()
    if not bound:
        return parsed
    raw = (text or str((parsed or {}).get("goal") or "")).strip()
    if is_verify_only(raw) and not wants_more_content(raw):
        return already_written_goal(bound, raw)
    if parsed is None:
        if is_rephrase_create(raw, bound):
            return already_written_goal(bound, raw)
        return None
    action = str(parsed.get("action") or "").strip().lower()
    if action not in {"write", "append"}:
        return parsed
    if looks_like_distinct_new_file(raw, bound):
        return parsed
    dest = str(parsed.get("path") or "").strip()
    if dest and _same_path(dest, bound):
        parsed = dict(parsed)
        parsed["unique_name"] = False
        parsed["overwrite"] = True
        parsed["path"] = bound
        return parsed
    if action == "append":
        parsed = dict(parsed)
        parsed["path"] = bound
        parsed["query"] = Path(bound).name
        parsed["unique_name"] = False
        return parsed
    if (
        not parsed.get("unique_name")
        and dest
        and not _same_path(dest, bound)
        and not kinds_compatible(
            _effect_label(raw) or str(parsed.get("label") or ""),
            label_from_path(bound),
        )
    ):
        return parsed
    if wants_more_content(raw):
        return _retarget_write(parsed, bound)
    if _existing_has_body(bound) and not str(parsed.get("content") or "").strip():
        return already_written_goal(bound, raw)
    return _retarget_write(parsed, bound)


def _retarget_write(parsed: dict[str, Any], bound: str) -> dict[str, Any]:
    out = dict(parsed)
    out["path"] = bound
    out["query"] = Path(bound).name
    out["unique_name"] = False
    out["overwrite"] = True
    return out


def _existing_has_body(path: str) -> bool:
    target = Path(path).expanduser()
    try:
        if not target.is_file():
            return False
        lines = [
            line.strip()
            for line in target.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError:
        return False
    return len(lines) >= 2


def _same_path(left: str, right: str) -> bool:
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return str(left).strip() == str(right).strip()


def _effect_label(text: str) -> str:
    try:
        from app.ev.desk_meaning import kind_label, occasion_label
    except Exception:
        return ""
    return (kind_label(text) or occasion_label(text) or "").strip().lower()


def _normalize_kind(label: str) -> str:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", (label or "").lower())
        if token not in _PATH_NOISE
    ]
    return " ".join(tokens)
