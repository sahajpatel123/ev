"""Live desk presence: Evie already knows the job. The owner should not re-teach it.

Not a phrase book. If a note or list is live, follow-ups that mean “more of
that”, “what’s on it”, or “what are we doing” resolve from the scene. Chat
stays chat. This is dynamic task context, not personality: it is used only to
resolve an explicit task follow-up and must never become an unsolicited feature
mention or offer.
"""

from __future__ import annotations

import re
from typing import Any

from app.ev.desk_scene import (
    last_note_object,
    last_spoken,
    object_label,
    pending_choice,
    scene_is_live,
    text_note_objects,
)

_CHAT_LEXICON = frozenset(
    {
        "fine",
        "ok",
        "okay",
        "sure",
        "thanks",
        "thank",
        "yeah",
        "yep",
        "yup",
        "nah",
        "nope",
        "cool",
        "wow",
        "right",
        "true",
        "good",
        "great",
        "nice",
        "sorry",
        "wait",
        "huh",
        "hmm",
        "mm",
        "hello",
        "hi",
        "hey",
        "tired",
        "crazy",
        "interesting",
        "alright",
        "whatever",
        "anyway",
    }
)
_QUESTION_START = re.compile(
    r"^\s*(?:what|what's|whats|why|how|when|who|where|which|is|are|do|does|did|can|could)\b",
    re.I,
)
_INVENTORY_RE = re.compile(
    r"\b(?:"
    r"what(?:'s|s| is) on (?:it|that|this|there|the (?:list|note|file))|"
    r"what(?:'s|s| is) (?:on )?the (?:list|note)|"
    r"what(?:'s|s| is) on there|"
    r"read (?:it|that|this|the list) back|"
    r"what do we have(?: so far)?|"
    r"what(?:'s|s| is) on (?:the )?list so far|"
    r"how's the list|"
    r"how(?:'s| is) the (?:list|note) looking"
    r")\b",
    re.I,
)
_JOB_RE = re.compile(
    r"\b(?:"
    r"what are we (?:doing|working on)|"
    r"what(?:'s|s| is) this (?:list|note|one)|"
    r"which list|"
    r"what list is this|"
    r"where were we|"
    r"what was i doing"
    r")\b",
    re.I,
)
_HAS_ITEM_RE = re.compile(
    r"\b(?:is|did we (?:add|put|get)|have we got|do we have)\s+"
    r"(.+?)\s+"
    r"(?:on (?:it|that|this|there|the (?:list|note))|there yet)\b",
    re.I,
)
_FOREIGN_ASK = re.compile(
    r"\b(?:calendar|reminder|email|message|inbox|alarm|weather|clock|time)\b",
    re.I,
)
_BARE_CONNECTIVE = re.compile(
    r"(?:,|&&|&|\band\b|\bplus\b|\btoo\b|\bas well\b|\balso\b)",
    re.I,
)
_BARE_LEAD = re.compile(
    r"^(?:and|plus|also|then)\s+",
    re.I,
)
_BARE_TAIL = re.compile(
    r"\s+(?:too|as well|also)\s*$",
    re.I,
)
_OP_SKIP_RE = re.compile(
    r"\b(?:"
    r"read|open|list|edit|delete|remove|clear|keep|rewrite|rename|copy|"
    r"duplicate|move|run|execute|what's in|what is in|what's on|what is on|"
    r"show me|pull up|cross(?:ed)? off|check(?:ed)? off|tick(?:ed)? off"
    r")\b",
    re.I,
)
_GENERIC_BODY = re.compile(r"^note from evie\.?$", re.I)


def live_work_block() -> str:
    """Prompt fact sheet. Empty when nothing is live so chat stays uncluttered."""

    facts = live_work_facts()
    if not facts:
        return ""
    lines = [
        "LIVE JOB (silent task context — use only for an explicit follow-up; do not announce, promote, or offer it unprompted; do not ask them to restate it):",
        f"- Working on: {facts['label']}.",
    ]
    if facts.get("items"):
        shown = ", ".join(facts["items"][:12])
        lines.append(f"- On it now: {shown}.")
    if facts.get("last_spoken"):
        lines.append(f"- Last you told them: {facts['last_spoken']}")
    pending = facts.get("pending")
    if pending:
        lines.append(f"- Waiting on: {pending}")
    lines.append(
        "If they name more things, check one off, undo, or ask what's on it, that is this job. "
        "Feelings, opinions, and small talk are not the list. Never mention this context in "
        "a fresh unrelated answer."
    )
    return "\n".join(lines)


def live_work_facts() -> dict[str, Any] | None:
    note = last_note_object()
    notes = text_note_objects()
    if note is None and not notes and not pending_choice():
        return None
    target = note or (notes[0] if notes else None)
    if target is None and pending_choice():
        return {
            "label": "a note they haven't chosen yet",
            "items": [],
            "last_spoken": last_spoken(),
            "pending": "which note to use",
        }
    if target is None:
        return None
    label = object_label(target) or "the live note"
    items = _open_items(target)
    pending = None
    choice = pending_choice()
    if choice:
        labels = [
            str(item.get("label") or "")
            for item in (choice.get("candidates") or [])
            if item.get("label")
        ]
        if labels:
            pending = "which note — " + " or ".join(labels[:3])
    return {
        "label": label,
        "items": items,
        "last_spoken": last_spoken(),
        "pending": pending,
        "path": str(target.get("path") or ""),
    }


def parse_presence_spoken(text: str) -> str | None:
    """Answer from the live job without making the owner explain it again."""

    raw = (text or "").strip()
    if not raw or _FOREIGN_ASK.search(raw):
        return None
    if not scene_is_live() and last_note_object() is None:
        return None
    facts = live_work_facts()
    if facts is None:
        return None
    if _JOB_RE.search(raw):
        return _job_spoken(facts)
    if _INVENTORY_RE.search(raw):
        return _inventory_spoken(facts)
    has_item = _HAS_ITEM_RE.search(raw)
    if has_item:
        needle = has_item.group(1).strip(" \"'")
        return _has_item_spoken(facts, needle)
    return None


def extract_bare_list_items(text: str) -> list[str]:
    """More things for the live list, even without 'add' — not chat."""

    raw = (text or "").strip()
    if not raw:
        return []
    if last_note_object() is None and not scene_is_live():
        return []
    if _QUESTION_START.search(raw) or raw.endswith("?"):
        return []
    if _INVENTORY_RE.search(raw) or _JOB_RE.search(raw) or _HAS_ITEM_RE.search(raw):
        return []
    if _FOREIGN_ASK.search(raw) or _OP_SKIP_RE.search(raw):
        return []
    if re.match(r"^\s*(?:i|i'm|im|we|you)\b", raw, re.I):
        return []
    from app.ev.laptop_files import (
        CONTENT_MUTATE_RE,
        FILE_FOLLOWUP_RE,
        JUST_KEEP_CHAT_RE,
        KEEP_ONLY_RE,
        NOTE_CREATE_RE,
    )

    if NOTE_CREATE_RE.search(raw):
        return []
    if FILE_FOLLOWUP_RE.search(raw) or CONTENT_MUTATE_RE.search(raw):
        return []
    if KEEP_ONLY_RE.search(raw) or JUST_KEEP_CHAT_RE.search(raw):
        return []
    from app.ev.desk_acts import DONE_FRAME_RE, LIST_CREATE_RE, UNDO_RE
    from app.ev.desk_meaning import list_create_parts, live_list_mutate, note_create_parts

    if UNDO_RE.search(raw) or DONE_FRAME_RE.search(raw) or LIST_CREATE_RE.search(raw):
        return []
    if list_create_parts(raw) is not None or note_create_parts(raw) is not None:
        return []
    note = last_note_object()
    if note is not None:
        held = [str(item).strip() for item in (note.get("held_items") or []) if str(item).strip()]
        act, _tokens = live_list_mutate(raw, held)
        if act is not None:
            return []
    lowered = re.sub(r"[.!?]+$", "", raw).strip().lower()
    if lowered in _CHAT_LEXICON or lowered.startswith("that's ") or lowered.startswith("thats "):
        return []
    words = re.findall(r"[A-Za-z0-9']+", raw)
    if not words or len(words) > 8:
        return []
    if any(word.lower() in _CHAT_LEXICON for word in words) and not _BARE_CONNECTIVE.search(raw):
        return []
    has_connective = bool(_BARE_CONNECTIVE.search(raw) or _BARE_LEAD.search(raw) or _BARE_TAIL.search(raw))
    body = _BARE_LEAD.sub("", raw)
    body = _BARE_TAIL.sub("", body).strip(" .,")
    from app.ev.laptop_files import _split_list_items

    items = _split_list_items(body)
    cleaned: list[str] = []
    for item in items:
        token = re.sub(r"^(?:the|a|an|my)\s+", "", item, flags=re.I).strip()
        if not token or token.lower() in _CHAT_LEXICON:
            continue
        if re.search(r"\b(?:because|should|maybe|think|feel)\b", token, re.I):
            continue
        cleaned.append(token)
    if not cleaned:
        return []
    if not has_connective and len(cleaned) == 1:
        # A lone noun onto the live list: "tape". Reject clauses.
        if len(cleaned[0].split()) > 3:
            return []
        if not re.match(r"^[A-Za-z0-9][\w'+\-]*(?:\s+[A-Za-z0-9][\w'+\-]*){0,2}$", cleaned[0]):
            return []
    return cleaned


def _open_items(note: dict[str, Any]) -> list[str]:
    held = [str(item).strip() for item in (note.get("held_items") or []) if str(item).strip()]
    if held:
        return [_bare_line(item) for item in held if not _is_done_line(item)]
    path = str(note.get("path") or "")
    if not path:
        return []
    from pathlib import Path

    try:
        body = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return []
    items: list[str] = []
    for line in body.splitlines():
        if not line.strip() or _is_done_line(line):
            continue
        items.append(_bare_line(line))
        if len(items) >= 16:
            break
    return items


def _bare_line(line: str) -> str:
    raw = re.sub(r"^[\-\*\d\.\)\s]+", "", line or "").strip()
    raw = re.sub(r"^\[x\]\s*", "", raw, flags=re.I).strip()
    return raw[:80]


def _is_done_line(line: str) -> bool:
    return bool(re.match(r"^\s*(?:\[x\]|✓|✔)", line or "", re.I))


def _inventory_spoken(facts: dict[str, Any]) -> str:
    from app.ev.desk_acts import join_spoken

    items = list(facts.get("items") or [])
    label = str(facts.get("label") or "the list")
    if not items:
        return f"The {label} is still empty."
    return f"{join_spoken(items).rstrip('.')}, on the {label}."


def _job_spoken(facts: dict[str, Any]) -> str:
    label = str(facts.get("label") or "the live note")
    items = list(facts.get("items") or [])
    if not items:
        return f"We're on the {label}."
    from app.ev.desk_acts import join_spoken

    return f"The {label}. {join_spoken(items[:8]).rstrip('.')} on it."


def _has_item_spoken(facts: dict[str, Any], needle: str) -> str:
    from app.ev.laptop_files import _line_has_token

    token = re.sub(r"^(?:the|a|an|my)\s+", "", (needle or "").strip(), flags=re.I)
    items = list(facts.get("items") or [])
    if token and any(_line_has_token(item, token) for item in items):
        return f"Yes, {token} is on it."
    return "Not yet."
