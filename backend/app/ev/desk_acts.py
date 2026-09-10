"""Meaning-based desk acts: undo, check-off, which-note, list remind/text, named and dated notes.

Not a phrase book. Frames (undo / mark-done / create-a-kind-of-list / note-for-a-day /
remind-or-text-the-live-list) plus the live scene decide the act. Chat stays chat.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.ev.desk_scene import (
    ADD_TO_NAMED_RE,
    OTHER_RE,
    PACKET_PUT_RE,
    last_mutating_entry,
    last_note_object,
    object_label,
    pending_choice,
    referent_file_path,
    text_note_objects,
)
from app.ev.resolve import owner_now

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_LIST_SKIP_NAMES = frozenset(
    {
        "new",
        "quick",
        "little",
        "small",
        "another",
        "nice",
        "good",
        "short",
        "simple",
        "a",
        "an",
        "the",
        "this",
        "that",
        "my",
    }
)
_FOREIGN_DEST = re.compile(
    r"\b(?:calendar|reminder|email|message|inbox|alarm|event|timer)\b",
    re.I,
)
UNDO_RE = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+)?"
    r"(?:undo(?:\s+(?:that|this|it|the last(?:\s+(?:one|change|edit))?))?|"
    r"put\s+(?:it|that)\s+back|"
    r"revert(?:\s+(?:that|this|it))?|"
    r"roll\s+(?:that|it)\s+back|"
    r"take\s+(?:that|it)\s+back)"
    r"\s*[.!?]*$",
    re.I,
)
DONE_FRAME_RE = re.compile(
    r"\b(?:"
    r"check(?:ed)?\s+off|cross(?:ed)?\s+off|tick(?:ed)?\s+off|"
    r"mark(?:ed)?\s+(?:as\s+)?(?:done|complete|off)|"
    r"strike(?:\s+through)"
    r")\b",
    re.I,
)
GOT_FRAME_RE = re.compile(
    r"^(?:please\s+)?(?:i(?:['’]?m|\s+am)?\s+)?"
    r"(?:got|picked\s+up|bought|grabbed|found|finished|did|"
    r"done\s+with|done\s+getting)"
    r"\s+(.+)$",
    re.I,
)
DONE_TAIL_RE = re.compile(
    r"\b(?:is|are|was)\s+(?:done|checked\s+off|crossed\s+off)\s*[.!?]*$",
    re.I,
)
STRONG_DEIXIS_RE = re.compile(
    r"\b(?:to|on|into)\s+(?:it|that|this)\b",
    re.I,
)
LIST_CREATE_RE = re.compile(
    r"\b(?:make|create|start|write|drop|leave|build)\s+(?:me\s+)?"
    r"(?:a\s+|an\s+|the\s+|my\s+|new\s+)?"
    r"(?:"
    r"(?P<kind>to-?do|todo|check)\s*lists?"
    r"|(?P<named>[A-Za-z][\w+\-]{0,32}(?:\s+[A-Za-z][\w+\-]{0,20}){0,2})\s+lists?"
    r"|lists?"
    r")\b",
    re.I,
)
LIST_BODY_RE = re.compile(
    r"(?:that\s+says|saying|that\s+reads|with(?:\s+the)?(?:\s+text|\s+words)?|"
    r"containing|that\s+contains|of|:)\s+(.+)$",
    re.I,
)
NOTE_FOR_WHEN_RE = re.compile(
    r"\b(?:drop|leave|create|write|jot|make|put|add)\s+(?:down\s+)?"
    r"(?:a\s+|the\s+|new\s+)?note\s+(?:for|dated)\s+"
    r"(?P<when>tomorrow|today|tonight|next week|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.I,
)
TOMORROW_NOTE_RE = re.compile(
    r"\b(?:tomorrow(?:'s)?\s+note|note\s+for\s+tomorrow)\b",
    re.I,
)
REMIND_FRAME_RE = re.compile(
    r"\b(?:remind me|set a reminder|don'?t (?:let me )?forget)\b",
    re.I,
)
REMIND_TO_TASK_RE = re.compile(
    r"\bremind me to\b(?!.{0,40}\b(?:this|that|it|the (?:list|note|file))\b)",
    re.I,
)
LIST_DEIXIS_RE = re.compile(
    r"\b(?:this|that|it|the (?:list|note|file)|this (?:list|note|file)|"
    r"that (?:list|note|file))\b",
    re.I,
)
TEXT_LIST_RE = re.compile(
    r"\b(?:text|message|i'?m message|imessage|sms|send)\b",
    re.I,
)
TEXT_TO_NAME_RE = re.compile(
    r"\b(?:to|for)\s+(?:my\s+)?([A-Za-z][\w'-]{1,40})\s*[.!?]*$",
    re.I,
)
TEXT_NAME_THEN_LIST_RE = re.compile(
    r"\b(?:text|message|send)\s+(?:my\s+)?([A-Za-z][\w'-]{1,40})\s+"
    r"(?:this|that|the (?:list|note|file)|it)\b",
    re.I,
)
CHOICE_ORDINAL_RE = re.compile(
    r"\b(?:the\s+)?(first|second|third|other(?:\s+one)?|that one)\b",
    re.I,
)
CHOICE_NAMED_RE = re.compile(
    r"^(?:please\s+|use\s+|the\s+)?"
    r"([A-Za-z][\w \-]{0,40}?)(?:\s+(?:one|list|note|file))?"
    r"\s*[.!?]*$",
    re.I,
)
_DONE_MARK = re.compile(r"^\s*(?:\[x\]|\[X\]|✓|✔|done:)\s*", re.I)


def parse_desk_act(text: str, last_path: str | None = None) -> dict[str, Any] | None:
    """Route a turn to a file goal, a life tool, or nothing (chat)."""

    raw = _norm(text)
    if not raw:
        return None
    tool = parse_list_tool(raw, last_path=last_path)
    if tool is not None:
        return tool
    goal = parse_desk_file_goal(raw, last_path)
    if goal is not None:
        return {"channel": "file", "goal": goal}
    return None


def looks_like_desk_file_act(text: str, last_path: str | None = None) -> bool:
    return parse_desk_file_goal(text, last_path) is not None


def parse_desk_file_goal(text: str, last_path: str | None = None) -> dict[str, Any] | None:
    raw = _norm(text)
    if not raw:
        return None
    chosen = parse_pending_choice(raw)
    if chosen is not None:
        return chosen
    if PACKET_PUT_RE.search(raw) and "packet" in raw.lower():
        return None
    if OTHER_RE.search(raw) and pending_choice() is None:
        return None
    undo = parse_undo(raw)
    if undo is not None:
        return undo
    checkoff = parse_checkoff(raw, last_path=last_path)
    if checkoff is not None:
        return checkoff
    dropped = parse_live_drop(raw, last_path=last_path)
    if dropped is not None:
        return dropped
    named = parse_named_list_create(raw)
    if named is not None:
        return named
    from app.ev.desk_meaning import note_create_parts

    note_parts = note_create_parts(raw)
    if note_parts is not None:
        dated = parse_dated_note(raw)
        if dated is not None:
            return dated
        return meaning_note_write_goal(
            raw,
            str(note_parts.get("body") or ""),
            list(note_parts.get("items") or []),
        )
    dated = parse_dated_note(raw)
    if dated is not None:
        return dated
    asked = maybe_ambiguous_append(raw)
    if asked is not None:
        return asked
    return None


def maybe_ambiguous_append(text: str, items: list[str] | None = None) -> dict[str, Any] | None:
    """Ask which note when several text notes are live and the dest is not unique."""

    raw = _norm(text)
    if not raw or ADD_TO_NAMED_RE.search(raw) or STRONG_DEIXIS_RE.search(raw):
        return None
    from app.ev.laptop_files import extract_append_items

    found = list(items or extract_append_items(raw))
    if not found:
        return None
    notes = text_note_objects()
    if len(notes) < 2:
        return None
    from app.ev.desk_scene import resolve_spoken_object

    named = resolve_spoken_object(raw)
    if named is not None and named.get("kind") == "note":
        return None
    candidates = _choice_candidates(notes)
    return {
        "action": "ask_which",
        "items": found,
        "content": "\n".join(found),
        "candidates": candidates,
        "goal": raw,
        "spoken": _which_note_spoken(candidates),
    }


def parse_pending_choice(text: str) -> dict[str, Any] | None:
    pending = pending_choice()
    if pending is None:
        return None
    raw = _norm(text)
    if not raw:
        return None
    candidates = list(pending.get("candidates") or [])
    picked = _match_choice(raw, candidates)
    if picked is None:
        return None
    items = list(pending.get("items") or [])
    path = str(picked.get("path") or "")
    if not path:
        return None
    return {
        "action": "append",
        "path": path,
        "query": Path(path).name,
        "content": "\n".join(items),
        "items": items,
        "receipt": "append",
        "clear_choice": True,
        "goal": raw,
    }


def parse_undo(text: str) -> dict[str, Any] | None:
    raw = _norm(text)
    if not raw or not UNDO_RE.search(raw):
        return None
    if _FOREIGN_DEST.search(raw):
        return None
    entry = last_mutating_entry()
    if entry is None:
        return None
    return {
        "action": "undo",
        "path": str(entry.get("path") or ""),
        "goal": raw,
        "receipt": "undo",
    }


def parse_checkoff(text: str, last_path: str | None = None) -> dict[str, Any] | None:
    raw = _norm(text)
    if not raw:
        return None
    notes = list(text_note_objects())
    live = _live_note(last_path)
    if live is not None:
        live_path = str(live.get("path") or "")
        if live_path and not any(str(item.get("path") or "") == live_path for item in notes):
            notes.insert(0, live)
    if not notes:
        return None
    hits: list[tuple[dict[str, Any], list[str]]] = []
    for note in notes:
        held = _held_items(note)
        if not held:
            continue
        tokens = _checkoff_tokens(raw, held)
        if tokens:
            hits.append((note, tokens))
    if not hits:
        return None
    note, tokens = hits[0]
    path = str(note.get("path") or "")
    return {
        "action": "checkoff",
        "path": path,
        "query": Path(path).name if path else "",
        "items": tokens,
        "instruction": raw,
        "goal": raw,
        "receipt": "checkoff",
    }


def named_list_write_goal(raw: str, label: str, items: list[str]) -> dict[str, Any]:
    token = re.sub(r"\s+", " ", (label or "list").strip().lower()) or "list"
    filename = _list_filename(token)
    folder = _desktop_folder()
    return {
        "action": "write",
        "path": str(Path(folder) / filename) if folder else filename,
        "query": filename,
        "content": "\n".join(items),
        "items": items,
        "aliases": _list_aliases(token),
        "unique_name": True,
        "receipt": "named_list",
        "label": token,
        "goal": raw,
    }


def meaning_note_write_goal(
    raw: str, body: str, items: list[str], *, when: str = ""
) -> dict[str, Any]:
    dated = parse_dated_note(raw) if when or TOMORROW_NOTE_RE.search(raw) or NOTE_FOR_WHEN_RE.search(raw) else None
    if dated is not None:
        if body and not str(dated.get("content") or "").strip():
            dated["content"] = body
            dated["items"] = items
        return dated
    folder = _desktop_folder()
    name = "evie-note.txt"
    return {
        "action": "write",
        "path": str(Path(folder) / name) if folder else name,
        "query": name,
        "content": body or "\n".join(items),
        "items": items,
        "receipt": "write_note",
        "label": "note",
        "goal": raw,
    }


def parse_named_list_create(text: str) -> dict[str, Any] | None:
    raw = _norm(text)
    if not raw:
        return None
    from app.ev.desk_meaning import list_create_parts

    parts = list_create_parts(raw)
    if parts is not None:
        label = str(parts.get("label") or "list")
        if label in _LIST_SKIP_NAMES:
            return None
        items = list(parts.get("items") or [])
        from app.ev.desk_meaning import wants_generated_contents

        if label == "list" and len(items) < 2 and not wants_generated_contents(raw, items, label=label):
            return None
        return named_list_write_goal(raw, label, items)
    match = LIST_CREATE_RE.search(raw)
    if not match:
        return None
    kind = (match.group("kind") or "").strip().lower()
    named = (match.group("named") or "").strip().lower()
    if kind:
        label = "todo" if kind.replace("-", "") == "todo" else "checklist"
    else:
        label = re.sub(r"\s+", " ", named).strip()
    if label in _LIST_SKIP_NAMES:
        return None
    body_match = LIST_BODY_RE.search(raw[match.end() :] if match.end() <= len(raw) else raw)
    payload = (body_match.group(1) if body_match else "").strip()
    from app.ev.desk_meaning import extract_inventory, occasion_label, reject_terms, wants_generated_contents

    if not label:
        label = occasion_label(raw) or "list"
    deny = reject_terms(raw, label)
    items = extract_inventory(raw, reject=deny)
    generate = wants_generated_contents(raw, items, label=label)
    if generate:
        items = []
    elif not items:
        items = [item for item in _list_items(payload) if item.lower() not in deny]
        if wants_generated_contents(raw, items, label=label):
            items = []
    if not label:
        label = "list"
    if label == "list" and len(items) < 2 and not wants_generated_contents(raw, items, label=label):
        return None
    return named_list_write_goal(raw, label, items)


def parse_dated_note(text: str) -> dict[str, Any] | None:
    raw = _norm(text)
    if not raw:
        return None
    match = NOTE_FOR_WHEN_RE.search(raw)
    when_token = ""
    if match:
        when_token = (match.group("when") or "").strip().lower()
    elif TOMORROW_NOTE_RE.search(raw):
        when_token = "tomorrow"
    else:
        return None
    when_date = _owner_date_for(when_token)
    if when_date is None:
        return None
    stamp = when_date.strftime("%Y-%m-%d")
    filename = f"note-{stamp}.txt"
    folder = _desktop_folder()
    content = _note_body(raw)
    if not content:
        from app.ev.desk_meaning import extract_inventory, reject_terms

        items = extract_inventory(raw, reject=reject_terms(raw, when_token))
        content = "\n".join(items)
    weekday = when_date.strftime("%A")
    aliases = [
        when_token,
        f"{when_token}'s note" if not when_token.endswith("s") else f"{when_token} note",
        f"note for {when_token}",
        weekday.lower(),
        f"{weekday.lower()}'s note",
    ]
    return {
        "action": "write",
        "path": str(Path(folder) / filename) if folder else filename,
        "query": filename,
        "content": content or f"Note for {weekday}.\n",
        "aliases": aliases,
        "unique_name": True,
        "receipt": "dated_note",
        "label": when_token,
        "when_label": weekday,
        "goal": raw,
    }


def parse_list_tool(text: str, last_path: str | None = None) -> dict[str, Any] | None:
    raw = _norm(text)
    if not raw:
        return None
    note = _live_note(last_path)
    if note is None:
        return None
    body = _note_text(note)
    if not body.strip():
        return None
    held = _held_items(note)
    if REMIND_FRAME_RE.search(raw) and not REMIND_TO_TASK_RE.search(raw):
        target = _reminder_text(raw, held, body)
        if target is None:
            return None
        return {
            "channel": "tool",
            "name": "set_reminder",
            "args": {"text": target[:2000], "when": raw[:128]},
        }
    if TEXT_LIST_RE.search(raw) and LIST_DEIXIS_RE.search(raw):
        who = _text_recipient(raw)
        if not who:
            return None
        from app.ev.send_intent import channel_from_text

        send_args: dict[str, object] = {
            "to": who,
            "text": _message_body(held, body)[:4000],
        }
        desk_channel = channel_from_text(raw)
        if desk_channel:
            send_args["channel"] = desk_channel
        return {
            "channel": "tool",
            "name": "send_message",
            "args": send_args,
        }
    return None


def spoken_receipt(
    *,
    action: str,
    items: list[str] | None = None,
    name: str = "",
    label: str = "",
    when_label: str = "",
    instruction: str = "",
    preview: str = "",
) -> str | None:
    found = [str(item).strip() for item in (items or []) if str(item).strip()]
    joined = join_spoken(found)
    receipt = action
    if receipt == "append" and found:
        return f"{_cap(joined)}, on the list."
    if receipt in {"write", "write_note"} and found:
        dest = "list" if re.search(r"\blist\b", f"{label} {name}", re.I) else "note"
        return f"{_cap(joined)}, on the {dest}."
    if receipt == "checkoff" and found:
        return f"Checked off {joined}."
    if receipt == "undo":
        return "Put it back."
    if receipt == "named_list":
        title = label or Path(name).stem.replace("-", " ") or "list"
        if found:
            verb = "is" if len(found) == 1 else "are"
            return f"Made a {title} list. {_cap(joined)} {verb} on it."
        return f"Made a {title} list."
    if receipt == "dated_note":
        day = when_label or label or "that day"
        snippet = preview.strip().split("\n")[0].strip() if preview else joined
        if snippet:
            return f"Left a note for {day}: {snippet}."
        return f"Left a note for {day}."
    if receipt == "keep_only" and found:
        return f"Just {joined} left."
    if receipt == "drop" and found:
        return f"Took {joined} off the list."
    return None


def join_spoken(items: list[str]) -> str:
    parts: list[str] = []
    for item in items:
        token = _DONE_MARK.sub("", str(item)).strip(" .,-")
        if token:
            parts.append(token)
    if not parts:
        return "that"
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def apply_checkoff(body: str, tokens: list[str]) -> tuple[str, list[str]]:
    from app.ev.laptop_files import _line_has_token

    hit: list[str] = []
    out: list[str] = []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if line.strip() and not _DONE_MARK.match(line.strip()) and any(
            _line_has_token(line, token) for token in tokens
        ):
            stripped = re.sub(r"^[\-\*\d\.\)\s]+", "", line.strip())
            stripped = _DONE_MARK.sub("", stripped).strip()
            out.append(f"[x] {stripped}")
            hit.append(stripped)
        else:
            out.append(line)
    return "\n".join(out), hit


def _norm(text: str) -> str:
    from app.ev.laptop_files import is_system_confirmation, normalize_file_utterance

    raw = normalize_file_utterance(text)
    if not raw or is_system_confirmation(raw):
        return ""
    return raw


def _cap(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return raw
    return raw[0].upper() + raw[1:] if raw[0].islower() else raw


def _desktop_folder() -> str:
    from app.ev.laptop_files import _alias_folder

    return _alias_folder("desktop")


def _list_items(payload: str) -> list[str]:
    from app.ev.laptop_files import _split_list_items

    return _split_list_items(payload)


def _list_filename(label: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "list"
    if token.endswith("list"):
        return f"{token}.txt"
    return f"{token}-list.txt"


def _list_aliases(label: str) -> list[str]:
    token = re.sub(r"\s+", " ", label).strip().lower()
    aliases = [token]
    if not token.endswith("list"):
        aliases.append(f"{token} list")
    return aliases


def _owner_date_for(token: str):
    now = owner_now()
    key = (token or "").strip().lower()
    if key in {"today", "tonight"}:
        return now.date()
    if key == "tomorrow":
        return (now + timedelta(days=1)).date()
    if key == "next week":
        days = 7 - now.weekday()
        if days <= 0:
            days = 7
        return (now + timedelta(days=days)).date()
    if key in _WEEKDAYS:
        target = _WEEKDAYS[key]
        delta = (target - now.weekday()) % 7
        return (now + timedelta(days=delta)).date()
    return None


def _note_body(raw: str) -> str:
    from app.ev.laptop_files import NOTE_COLON_RE, SAYS_RE, _spoken_file_body

    says = SAYS_RE.search(raw)
    if says:
        return _spoken_file_body(says.group(1))
    colon = NOTE_COLON_RE.search(raw)
    if colon:
        return _spoken_file_body(colon.group(1))
    return ""


def _live_note(last_path: str | None = None) -> dict[str, Any] | None:
    note = last_note_object()
    if note is not None:
        return note
    path = referent_file_path(last_path)
    if path is None:
        return None
    for obj in text_note_objects():
        obj_path = str(obj.get("path") or "")
        if obj_path in {str(path), str(path.resolve())}:
            return obj
    if path.suffix.lower() in {".txt", ".md", ".markdown", ".rtf"}:
        return {"path": str(path), "kind": "note", "aliases": [], "held_items": []}
    return None


def _held_items(note: dict[str, Any]) -> list[str]:
    held = [str(item).strip() for item in (note.get("held_items") or []) if str(item).strip()]
    if held:
        return held
    return [item for item in _note_text(note).splitlines() if item.strip()]


def _note_text(note: dict[str, Any]) -> str:
    path = Path(str(note.get("path") or "")).expanduser()
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return ""


def _checkoff_tokens(raw: str, held: list[str]) -> list[str]:
    from app.ev.laptop_files import _line_has_token, _spoken_file_body

    mentioned = [item for item in held if _line_has_token(raw, _bare_item(item))]
    if DONE_FRAME_RE.search(raw):
        payload = DONE_FRAME_RE.sub("", raw)
        payload = re.sub(
            r"^(?:please\s+|can you\s+|could you\s+|i\s+)?",
            "",
            payload,
            flags=re.I,
        ).strip(" .,")
        payload = _spoken_file_body(payload)
        items = _list_items(payload)
        matched = [item for item in (items or mentioned) if any(_line_has_token(h, _bare_item(item)) or _line_has_token(item, _bare_item(h)) for h in held)]
        if not matched:
            matched = mentioned
        return matched
    got = GOT_FRAME_RE.search(raw)
    if got:
        return mentioned
    if DONE_TAIL_RE.search(raw):
        return mentioned
    from app.ev.desk_meaning import live_list_mutate

    act, tokens = live_list_mutate(raw, held)
    if act == "checkoff":
        return tokens
    return []


def parse_live_drop(text: str, last_path: str | None = None) -> dict[str, Any] | None:
    raw = _norm(text)
    if not raw:
        return None
    notes = list(text_note_objects())
    live = _live_note(last_path)
    if live is not None:
        live_path = str(live.get("path") or "")
        if live_path and not any(str(item.get("path") or "") == live_path for item in notes):
            notes.insert(0, live)
    if not notes:
        return None
    from app.ev.desk_meaning import live_list_mutate

    for note in notes:
        held = _held_items(note)
        if not held:
            continue
        act, tokens = live_list_mutate(raw, held)
        if act == "drop" and tokens:
            path = str(note.get("path") or "")
            return {
                "action": "drop",
                "path": path,
                "query": Path(path).name if path else "",
                "items": tokens,
                "instruction": raw,
                "goal": raw,
                "receipt": "drop",
            }
    return None


def apply_drop(body: str, tokens: list[str]) -> tuple[str, list[str]]:
    from app.ev.laptop_files import _drop_token_lines

    current = body or ""
    hit: list[str] = []
    for token in tokens:
        nxt = _drop_token_lines(current, token)
        if nxt is not None:
            current = nxt
            hit.append(token)
    return current, hit


def _bare_item(item: str) -> str:
    return _DONE_MARK.sub("", item or "").strip()


def _choice_candidates(notes: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for obj in notes[:4]:
        path = str(obj.get("path") or "")
        if not path:
            continue
        out.append(
            {
                "id": str(obj.get("id") or ""),
                "path": path,
                "label": object_label(obj),
            }
        )
    return out


def _which_note_spoken(candidates: list[dict[str, str]]) -> str:
    labels = [_the(str(item.get("label") or "note")) for item in candidates if item.get("label")]
    if len(labels) >= 2:
        return f"Which note — {labels[0]} or {labels[1]}?"
    return "Which note did you mean?"


def _the(label: str) -> str:
    token = (label or "").strip()
    if token.lower().startswith(("the ", "my ")):
        return token
    return f"the {token}"


def _match_choice(raw: str, candidates: list[dict[str, str]]) -> dict[str, str] | None:
    if not candidates:
        return None
    ordinal = CHOICE_ORDINAL_RE.search(raw)
    if ordinal:
        token = re.sub(r"\s+", " ", ordinal.group(1).lower())
        index = {"first": 0, "second": 1, "third": 2}.get(token)
        if token.startswith("other") or token == "that one":
            index = 1 if len(candidates) > 1 else 0
        if index is not None and index < len(candidates):
            return candidates[index]
    lowered = raw.lower()
    hits: list[dict[str, str]] = []
    for item in candidates:
        label = str(item.get("label") or "").lower()
        if not label:
            continue
        if re.search(rf"\b{re.escape(label)}\b", lowered) or label in lowered:
            hits.append(item)
        else:
            stem = label.replace(" list", "").replace(" note", "").strip()
            if stem and re.search(rf"\b{re.escape(stem)}\b", lowered):
                hits.append(item)
    if len(hits) == 1:
        return hits[0]
    named = CHOICE_NAMED_RE.search(raw)
    if named and len(hits) == 1:
        return hits[0]
    return None


def _reminder_text(raw: str, held: list[str], body: str) -> str | None:
    from app.ev.laptop_files import _line_has_token

    if LIST_DEIXIS_RE.search(raw):
        return _message_body(held, body)
    mentioned = [item for item in held if _line_has_token(raw, _bare_item(item))]
    if mentioned:
        return join_spoken(mentioned)
    return None


def _message_body(held: list[str], body: str) -> str:
    if held:
        return join_spoken(held)
    blob = re.sub(r"\s+", " ", (body or "").strip())
    return blob[:400] or "the list"


def _text_recipient(raw: str) -> str:
    named = TEXT_NAME_THEN_LIST_RE.search(raw)
    if named:
        who = named.group(1).strip()
        if who.lower() not in {"this", "that", "the", "my", "it"}:
            return who
    dest = TEXT_TO_NAME_RE.search(raw)
    if dest:
        who = dest.group(1).strip()
        if who.lower() not in {"this", "that", "the", "my", "it", "me"}:
            return who
    return ""
