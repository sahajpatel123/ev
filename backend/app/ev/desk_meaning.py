"""Desk meaning: act + payload from structure, not required verbs.

Cheap layer: named kind (packing list) + inventory shape (quotes, comma/and
lists) or a request-class marker. The owner does not have to say make/create.
Muse Spark 1.3 maps leftover wording onto an act. The kind, folder, and
filename are never written as the body.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("ev.desk_meaning")

_DEST_PP = re.compile(
    r"\b(?:on|in|to|into|onto|inside)\s+(?:my\s+|the\s+)?(?:desktop|documents|downloads|"
    r"icloud(?:\s+drive)?|computer|laptop)\b",
    re.I,
)
_KIND_SPAN = re.compile(
    r"\b([A-Za-z][\w+\-]{1,32}(?:\s+[A-Za-z][\w+\-]{1,20}){0,2})\s+"
    r"(?:to-?do\s+lists?|todo\s+lists?|check-?lists?|check\s*lists?|lists?|notes?)\b",
    re.I,
)
_KIND_FILE = re.compile(
    r"\b([A-Za-z][\w+\-]{2,24})\s+files?\b",
    re.I,
)
_INVENTORY_SPLIT = re.compile(
    r"\s*(?:,\s*(?:and\s+)?|(?:;|/)\s+|\s+(?:and|&|plus)\s+)\s*",
    re.I,
)
_SCAFFOLD = frozenset(
    {
        "a",
        "an",
        "the",
        "my",
        "me",
        "on",
        "in",
        "to",
        "into",
        "onto",
        "for",
        "of",
        "with",
        "that",
        "this",
        "those",
        "these",
        "which",
        "whose",
        "make",
        "create",
        "start",
        "write",
        "drop",
        "leave",
        "build",
        "put",
        "add",
        "please",
        "new",
        "also",
        "just",
        "some",
        "few",
        "list",
        "lists",
        "note",
        "notes",
        "file",
        "files",
        "checklist",
        "check-list",
        "todo",
        "desktop",
        "documents",
        "downloads",
        "icloud",
        "folder",
        "computer",
        "laptop",
        "names",
        "name",
        "items",
        "item",
        "things",
        "thing",
        "stuff",
        "bits",
        "including",
        "includes",
        "include",
        "like",
        "namely",
        "called",
        "named",
        "titled",
        "saying",
        "says",
        "reads",
        "contains",
        "containing",
        "text",
        "words",
        "word",
        "contents",
        "there",
        "here",
        "following",
    }
)
_COMPLETENESS = frozenset(
    {
        "full",
        "complete",
        "whole",
        "proper",
        "typical",
        "usual",
        "necessary",
        "needed",
        "essential",
        "standard",
    }
)
_REASON_CLAUSE = re.compile(
    r"\s+(?:because|since|now that|after)\s+.+$",
    re.I,
)
_DESIRE = re.compile(
    r"\b(?:need|want|wanna|think|feel|love|like|hate|hope|wish|guess|also)\b",
    re.I,
)
_LIST_EXIT = re.compile(
    r"\b(?:"
    r"remove|delete|erase|cut|"
    r"take(?:\s+it)?\s+off|take\s+out|"
    r"don'?t need|do not need|no longer need|"
    r"get rid of|cross(?:ed)? out"
    r")\b",
    re.I,
)
_FIRST_PERSON = re.compile(
    r"^\s*(?:please\s+)?i(?:['’]m|\s+am|['’]ve|\s+have|\s+had)?\b",
    re.I,
)
_FOREIGN_LIFE = re.compile(
    r"\b(?:calendar|reminder|email|message|inbox|alarm|event|timer)\b",
    re.I,
)
_DESK_NOUN = re.compile(r"\b(?:lists?|check-?lists?|notes?|files?|documents?)\b", re.I)
_DELEGATE = re.compile(
    r"\byou\s+(?:think|decide|consider|suggest|recommend|pick|choose|figure)\b|"
    r"\bwhat(?:ever)?\s+you\s+(?:think|want|decide)\b|"
    r"\bitems?\s+you\s+think\b|"
    r"\bwhat\s+i(?:['’]ll|\s+will)?\s+(?:need|should\s+(?:bring|pack|take|get|buy|have))\b|"
    r"\bwhatever\s+i\s+need\b|"
    r"\bthe\s+usual\b|"
    r"\bcome\s+up\s+with\b|"
    r"\bfigure\s+(?:it\s+|this\s+)?out\b|"
    r"\bput\s+together\b|"
    r"\baccording to (?:you|yourself|evie)\b|"
    r"\bup to you\b|"
    r"\b(?:you|evie)\s+(?:can |should )?(?:decide|choose|pick|make (?:one|it) up)\b",
    re.I,
)
_HAVE_OCCASION = re.compile(
    r"\b(?:i(?:['’]m|\s+am)?\s+)?(?:have|got)\s+(?:a|an|the)\s+([A-Za-z][\w+\-]{2,32})",
    re.I,
)
_GERUND_OCCASION = re.compile(
    r"\b(?:i(?:['’]m|\s+am)\s+|going\s+)"
    r"(flying|traveling|travelling|camping|moving|interviewing|driving|packing|hiking)\b",
    re.I,
)
_GERUND_STEM = {
    "flying": "flight",
    "traveling": "trip",
    "travelling": "trip",
    "camping": "camping",
    "moving": "move",
    "interviewing": "interview",
    "driving": "drive",
    "packing": "packing",
    "hiking": "hike",
}
_MOTION_OCCASION = re.compile(
    r"\b(?:going|heading|leaving|off)\s+(?:to\s+)?"
    r"(?:the\s+|a\s+|an\s+|my\s+)?"
    r"(?!make|create|start|write|build|save|put|get|do\b)"
    r"([A-Za-z][\w+\-]{2,32})",
    re.I,
)
_NEED_OCCASION = re.compile(
    r"\bi\s+need\s+(?!you\b|to\b)(?:a|an|the\s+)?([A-Za-z][\w+\-]{2,32})",
    re.I,
)
_FOR_OCCASION = re.compile(
    r"\b(?:for|before)\s+(?:the\s+|my\s+|our\s+|this\s+|a\s+|an\s+)?"
    r"(?:new\s+|old\s+)?"
    r"([A-Za-z][\w+\-]{2,32})\b",
    re.I,
)
_REQUEST_PAYLOAD = re.compile(
    r"^(?:what(?:ever)?\s+(?:i|you|we)\s+(?:need|should|think|want)|"
    r"what\s+i(?:['’]ll|\s+will)?\s+(?:need|should\s+\w+)|"
    r"items?\s+you\s+think|"
    r"the\s+usual(?:\s+\w+)?|"
    r"talking\s+points|"
    r"together\s+what)\b",
    re.I,
)
_ACT_CHUNK = re.compile(
    r"\b(?:want you|you think|what i(?:['’]ll| will)? (?:need|should)|"
    r"whatever i need|figure out|come up with|put together)\b",
    re.I,
)
_WHEN_WORD = frozenset(
    {
        "tomorrow",
        "today",
        "tonight",
        "soon",
        "morning",
        "afternoon",
        "evening",
        "weekend",
        "week",
        "saturday",
        "sunday",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
    }
)
_KIND_SKIP = frozenset(
    {
        "you",
        "i",
        "we",
        "of",
        "on",
        "about",
        "to",
        "for",
        "with",
        "from",
        "decide",
        "think",
        "want",
        "need",
        "figure",
        "come",
        "put",
        "get",
        "leave",
        "save",
        "write",
        "draft",
        "drop",
        "whatever",
        "verify",
        "verified",
        "verification",
        "according",
        "yourself",
        "desktop",
        "documents",
        "downloads",
        "icloud",
        "text",
    }
)
_PAYLOAD_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"type": "string"}},
        "empty": {"type": "boolean"},
    },
    "required": ["items"],
}
_ACT_SCHEMA = {
    "type": "object",
    "properties": {
        "act": {
            "type": "string",
            "enum": [
                "chat",
                "write_list",
                "write_note",
                "append",
                "checkoff",
                "undo",
                "remind",
                "text",
                "read",
            ],
        },
        "label": {"type": "string"},
        "items": {"type": "array", "items": {"type": "string"}},
        "when": {"type": "string"},
        "who": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["act"],
}
_QUESTION = re.compile(
    r"^\s*(?:what|what's|whats|why|"
    r"how(?:'s| is| are| was| do| does| did)|"
    r"when|who|where|which|is|are|do|does|did)\b",
    re.I,
)
_NOT_CREATE = re.compile(
    r"\b(?:"
    r"append|"
    r"what's on|what is on|what's in|what is in|"
    r"read (?:the|my|it|this)|open (?:the|my|it|this)|pull up|"
    r"check(?:ed)? off|cross(?:ed)? off|"
    r"undo|put it back|"
    r"add .{0,80} to (?:(?:the|my)\s+)?(?:it|this|that|(?:\w+\s+){0,2}(?:lists?|notes?|files?))|"
    r"to (?:(?:the|my)\s+)?(?:it|this|that|"
    r"(?!create|make|start|write|build|save|draft)(?:[A-Za-z][\w+\-]*\s+){0,2}(?:lists?|notes?|files?))"
    r")\b",
    re.I,
)
_ILLOCUTION = re.compile(
    r"\b(?:"
    r"need|want|wanna|could use|would like|looking for|"
    r"get(?:\s+me)?|give(?:\s+me)?|"
    r"can you|could you|would you|please|"
    r"let'?s|how about|time for|"
    r"make|create|start|write|drop|leave|build|jot|put|draft|"
    r"prepare|set\s*up"
    r")\b",
    re.I,
)
_NOTES_APP = re.compile(r"\bin\s+(?:the\s+)?notes?\s+app\b", re.I)
_FILE_CALLED = re.compile(
    r"\b(?:file|txt)\b.{0,48}\b(?:called|named|titled|spelled)\b|"
    r"\b(?:called|named|titled)\s+\S+\.(?:txt|md|csv|json)\b",
    re.I,
)
_GENERIC_KIND = frozenset(
    {
        "a",
        "an",
        "the",
        "my",
        "this",
        "that",
        "new",
        "quick",
        "little",
        "small",
        "another",
        "nice",
        "good",
        "short",
        "simple",
        "full",
        "complete",
        "whole",
        "proper",
        "necessary",
        "typical",
        "usual",
        "needed",
        "called",
        "named",
        "titled",
        "write",
        "edit",
        "save",
        "make",
        "create",
        "leave",
        "drop",
        "jot",
        "file",
        "files",
        "spelled",
        "open",
        "read",
        "show",
        "pull",
        "you",
        "of",
        "on",
        "about",
        "to",
        "for",
        "with",
        "from",
        "decide",
        "think",
        "whatever",
        "come",
        "figure",
        "get",
    }
)
_CHAT_BANTER = frozenset(
    {
        "fine",
        "ok",
        "okay",
        "sure",
        "thanks",
        "yeah",
        "yep",
        "cool",
        "tired",
        "hello",
        "hi",
        "hey",
        "alright",
    }
)


def reject_terms(text: str, label: str = "") -> set[str]:
    deny = set(_SCAFFOLD)
    deny.update(_COMPLETENESS)
    deny.update(_WHEN_WORD)
    deny.update({"have", "got", "save", "document", "documents", "inside", "think", "necessary"})
    deny.update({"according", "yourself", "verify", "verified", "verification", "txt"})
    deny.update({"i", "i'm", "im", "i've", "ive", "we", "you", "is", "are", "was", "be"})
    deny.update({"it", "them", "whatever", "figure", "come", "together", "out"})
    for token in re.findall(r"[A-Za-z0-9][\w+\-]*", label or ""):
        deny.add(token.lower())
    span = _KIND_SPAN.search(text or "")
    if span:
        for token in span.group(1).lower().split():
            deny.add(token)
    occ = occasion_label(text)
    if occ:
        deny.add(occ)
    return deny


def is_kind_echo(body: str, label: str = "") -> bool:
    """True when the file body is just the list/note kind, not owner content."""

    raw = (body or "").strip().strip(" .")
    if not raw:
        return False
    lines = [re.sub(r"^[\-\*\d\.\)\s]+", "", line).strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return False
    deny = reject_terms(raw, label)
    if len(lines) == 1:
        token = lines[0].lower().strip(" .")
        if token in deny:
            return True
        label_l = re.sub(r"\s+", " ", (label or "").strip().lower())
        if label_l and token in {label_l, f"{label_l} list", f"{label_l} note"}:
            return True
        words = token.split()
        if words and all(word in deny or word in {"list", "note"} for word in words):
            return True
        if re.fullmatch(r"note for [a-z]+", token):
            return True
        if re.fullmatch(r"note from evie", token):
            return True
        if is_request_payload(token):
            return True
        occ = occasion_label(label) or occasion_label(" ".join(words))
        if occ and words and words[0] == occ:
            return True
        if label_l and re.fullmatch(
            rf"{re.escape(label_l)}(?:\s+(?:tomorrow|today|tonight|morning))?",
            token,
        ):
            return True
    return all(
        re.sub(r"[^a-z0-9]+", "", line.lower()) in deny
        or (label and line.lower() in {label.lower(), f"{label.lower()} list"})
        for line in lines
    )


def kind_label(text: str) -> str | None:
    """Label of a named list/note/file kind, if the utterance names one."""

    span = _KIND_SPAN.search(text or "")
    if not span:
        return None
    tokens = [
        token
        for token in re.sub(r"\s+", " ", span.group(1).strip().lower()).split()
        if token not in _GENERIC_KIND and token not in _KIND_SKIP
    ]
    if not tokens:
        return None
    raw = " ".join(tokens)
    if raw in {"to-do", "todo", "to do"}:
        return "todo"
    if raw in {"check", "check list", "check-list"}:
        return "checklist"
    return raw


def occasion_label(text: str) -> str | None:
    """Situation noun: have/got, I'm flying, heading to X, I need X, for the trip."""

    raw = text or ""

    def _ok(token: str | None) -> str | None:
        word = (token or "").strip().lower()
        if not word or word in _GENERIC_KIND or word in _SCAFFOLD or word in _KIND_SKIP:
            return None
        if word in {"document", "documents", "folder", "computer", "laptop"}:
            return None
        if word in _WHEN_WORD:
            return None
        return word

    hit = _HAVE_OCCASION.search(raw)
    found = _ok(hit.group(1) if hit else None)
    if found:
        return found
    gerund = _GERUND_OCCASION.search(raw)
    if gerund:
        stem = _GERUND_STEM.get(gerund.group(1).strip().lower())
        return _ok(stem) or stem
    motion = _MOTION_OCCASION.search(raw)
    found = _ok(motion.group(1) if motion else None)
    if found:
        return found
    need = _NEED_OCCASION.search(raw)
    found = _ok(need.group(1) if need else None)
    if found:
        return found
    if _DEST_PP.search(raw) or _DESK_NOUN.search(raw):
        for_hit = _FOR_OCCASION.search(raw)
        found = _ok(for_hit.group(1) if for_hit else None)
        if found:
            return found
    return None


def is_request_payload(body: str) -> bool:
    """True when a would-be file body is still the request, not the contents."""

    raw = (body or "").strip().strip(" .")
    if not raw:
        return False
    return bool(_REQUEST_PAYLOAD.search(raw.lower()))


def strip_reason_clause(text: str) -> str:
    """Drop because/since tails so the act is the first clause, not the reason."""

    return _REASON_CLAUSE.sub("", text or "").strip(" .,")


def mentioned_held(text: str, held: Iterable[str]) -> list[str]:
    """Held-list lines named in this turn. Live scene, not a verb list."""

    from app.ev.laptop_files import _line_has_token

    raw = text or ""
    found: list[str] = []
    seen: set[str] = set()
    for item in held:
        token = re.sub(r"^\s*(?:\[x\]|\[X\]|✓|✔|done:)\s*", "", str(item or ""), flags=re.I).strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        if _line_has_token(raw, token):
            seen.add(key)
            found.append(token)
    return found


def live_list_mutate(text: str, held: Iterable[str]) -> tuple[str | None, list[str]]:
    """Completion or list-exit against live items. Chat and desire stay out.

    Structure: mentioned held items + residue after stripping them.
    List-exit (remove / don't need / take off) drops lines.
    First-person completion about those items checks them off.
    """

    raw = (text or "").strip()
    if not raw or _QUESTION.search(raw) or raw.endswith("?"):
        return None, []
    if _FOREIGN_LIFE.search(raw):
        return None, []
    from app.ev.laptop_files import CONTENT_MUTATE_RE, KEEP_ONLY_RE

    if KEEP_ONLY_RE.search(raw) or CONTENT_MUTATE_RE.search(raw):
        return None, []
    if list_create_parts(raw) is not None or note_create_parts(raw) is not None:
        return None, []
    mentioned = mentioned_held(raw, held)
    if not mentioned:
        return None, []
    if _LIST_EXIT.search(raw):
        return "drop", mentioned
    if not _FIRST_PERSON.search(raw):
        return None, []
    if _DESIRE.search(raw):
        return None, []
    core = raw
    for item in mentioned:
        core = re.sub(rf"\b{re.escape(item)}\b", " ", core, flags=re.I)
    core = strip_reason_clause(core)
    words = [word.lower() for word in re.findall(r"[A-Za-z']+", core)]
    skip = {
        "i",
        "i'm",
        "im",
        "i've",
        "ive",
        "and",
        "the",
        "a",
        "an",
        "my",
        "please",
        "them",
        "it",
        "those",
        "these",
        "that",
        "this",
        "too",
        "already",
    }
    residue = [word for word in words if word not in skip]
    if not residue:
        return None, []
    return "checkoff", mentioned


def wants_generated_contents(text: str, items: list[str], *, label: str = "") -> bool:
    """True when they asked Evie to propose the lines, not to copy named items."""

    raw = text or ""
    if _enumerated_contents(raw, items):
        return False
    if re.search(r"(?:that\s+says|saying|that\s+reads|containing)\s+\S", raw, re.I):
        return False
    named = (label or kind_label(raw) or occasion_label(raw) or "").strip()
    if not named:
        hit = _KIND_FILE.search(raw)
        token = (hit.group(1).strip().lower() if hit else "")
        if token and token not in _GENERIC_KIND and token not in _KIND_SKIP:
            named = token
        elif re.search(r"\bcheck-?lists?\b", raw, re.I):
            named = "checklist"
        elif _DESK_NOUN.search(raw) and (
            _DELEGATE.search(raw)
            or bool({w.lower() for w in re.findall(r"[A-Za-z0-9][\w+\-]*", raw)} & _COMPLETENESS)
        ):
            named = "list"
        elif _DEST_PP.search(raw) and _DELEGATE.search(raw):
            named = "list"
    if not named:
        return False
    if _DELEGATE.search(raw):
        return True
    tokens = {word.lower() for word in re.findall(r"[A-Za-z0-9][\w+\-]*", raw)}
    if tokens & _COMPLETENESS:
        return True
    if not _ILLOCUTION.search(raw):
        return False
    return leftover_needs_model(raw, items, label=named) and bool(_DEST_PP.search(raw))


def _enumerated_contents(raw: str, items: list[str]) -> bool:
    """True when the owner already named the lines (quotes or a real comma list)."""

    if not items:
        return False
    if len(re.findall(r"[\"'][^\"']{1,80}[\"']", raw or "")) >= 2:
        return True
    if (raw or "").count(",") >= 2 and len(items) >= 3:
        return True
    return False


def looks_like_desk_job(text: str) -> bool:
    """Cheap gate: this turn is a desk/file job without requiring a magic verb."""

    return list_create_parts(text) is not None or note_create_parts(text) is not None


def list_create_parts(text: str) -> dict[str, Any] | None:
    """Create a named list from kind + contents or a request — any wording."""

    raw = (text or "").strip()
    if not raw or _NOTES_APP.search(raw) or _FILE_CALLED.search(raw):
        return None
    if _QUESTION.search(raw) or raw.endswith("?"):
        return None
    if _NOT_CREATE.search(raw):
        return None
    if re.search(r"\bnotes?\b", raw, re.I) and not kind_label(raw):
        return None
    label = kind_label(raw) or occasion_label(raw)
    deny = reject_terms(raw, label or "")
    items = extract_inventory(raw, reject=deny)
    asked = bool(_ILLOCUTION.search(raw))
    generate = wants_generated_contents(raw, items, label=label or "")
    dest = bool(_DEST_PP.search(raw))
    if not label and (asked or generate):
        hit = _KIND_FILE.search(raw)
        token = (hit.group(1).strip().lower() if hit else "")
        if token and token not in _GENERIC_KIND and token not in _KIND_SKIP:
            label = token
            deny = reject_terms(raw, label)
            items = extract_inventory(raw, reject=deny)
            generate = wants_generated_contents(raw, items, label=label)
        elif generate and (dest or _DESK_NOUN.search(raw)):
            label = occasion_label(raw) or "list"
        elif asked and _DESK_NOUN.search(raw) and (dest or generate):
            label = "list"
    if label:
        if generate:
            items = []
        elif items and wants_generated_contents(raw, [], label=label):
            items = [
                item
                for item in items
                if not re.search(r"\b(?:i|you|we|adding|create|want|need|make)\b", item, re.I)
            ]
            if len(items) < 2:
                items = []
        if items:
            return {"label": label, "items": items}
        if generate or (asked and (dest or _DESK_NOUN.search(raw))):
            return {"label": label, "items": []}
        return None
    if len(items) >= 2 and asked and re.search(r"\blists?\b", raw, re.I) and not generate:
        return {"label": "list", "items": items}
    return None


def note_create_parts(text: str) -> dict[str, Any] | None:
    """Create a note from dest/body without requiring drop/leave/write."""

    raw = (text or "").strip()
    if not raw or _NOTES_APP.search(raw) or _FILE_CALLED.search(raw):
        return None
    if _QUESTION.search(raw) or raw.endswith("?"):
        return None
    if _NOT_CREATE.search(raw):
        return None
    if not re.search(r"\bnote\b", raw, re.I):
        return None
    named = kind_label(raw)
    if named and named not in {"note", "notes"}:
        return None
    has_dest = bool(_DEST_PP.search(raw))
    asked = bool(_ILLOCUTION.search(raw))
    deny = reject_terms(raw, "note")
    says = re.search(
        r"(?:that\s+says|saying|that\s+reads|containing|:)\s+(.+)$",
        raw,
        re.I,
    )
    spoken = (says.group(1).strip() if says else "")
    items = extract_inventory(raw, reject=deny)
    generate = wants_generated_contents(raw, items, label=named or "note")
    body = spoken or "\n".join(items)
    if is_request_payload(body):
        body = ""
    if spoken:
        generate = False
    elif generate and not items:
        body = ""
    if body and (asked or has_dest):
        return {"label": "note", "items": items, "body": body}
    if asked and has_dest:
        return {"label": "note", "items": items, "body": body}
    if generate and (asked or has_dest):
        return {"label": "note", "items": [], "body": ""}
    return None


def spark_desk_candidate(text: str) -> bool:
    """When cheap frames miss but dest/kind plus a request still marks desk work."""

    raw = (text or "").strip()
    if not raw:
        return False
    lowered = re.sub(r"[.!?]+$", "", raw).strip().lower()
    if lowered in _CHAT_BANTER or lowered.startswith("that's ") or lowered.startswith("thats "):
        return False
    if _QUESTION.search(raw) or raw.endswith("?"):
        return False
    if _NOT_CREATE.search(raw) or _FILE_CALLED.search(raw):
        return False
    if looks_like_desk_job(raw):
        return False
    asked = bool(_ILLOCUTION.search(raw))
    dest = bool(_DEST_PP.search(raw))
    named = bool(kind_label(raw) or occasion_label(raw) or _DESK_NOUN.search(raw))
    generate = wants_generated_contents(raw, [])
    if generate and (dest or named):
        return True
    if named and asked and dest:
        return True
    if dest and _DESK_NOUN.search(raw) and asked:
        return True
    return False


async def interpret_owner_act(
    text: str, *, last_path: str | None = None
) -> dict[str, Any] | None:
    """Muse Spark 1.3 maps any wording onto a desk/life act. Chat returns None."""

    raw = (text or "").strip()
    if not raw or not spark_desk_candidate(raw):
        return None
    from app.gateway.muse import (
        MuseProviderUnavailable,
        muse_spark_key_loaded,
        muse_spark_model,
    )

    if not muse_spark_key_loaded():
        return None
    try:
        from app.contracts import ChatMessage
        from app.gateway.muse_spark import muse_spark_provider

        result = await muse_spark_provider().chat_structured(
            [
                ChatMessage(
                    role="system",
                    content=(
                        "Classify what the owner wants Evie to do with local lists, notes, "
                        "files, reminders, or messages. Wording varies; do not require "
                        "specific verbs. Return JSON with act. "
                        "act=chat if this is talk, feelings, or opinions. "
                        "write_list / write_note: they want a new list or note. If they named "
                        "items, those are the contents. If they described a situation or asked "
                        "you to decide a necessary/typical/full set and named no lines, fill "
                        "items with concrete real-world things — never the situation phrase, "
                        "kind-name, or folder. "
                        "Never invent items when they only asked for an empty named list. "
                        "append/checkoff/undo/read act on the live list. "
                        "remind/text are life tools (when/who + items as the body)."
                    ),
                ),
                ChatMessage(role="user", content=raw[:4000]),
            ],
            schema=_ACT_SCHEMA,
            schema_name="desk_act",
            model=muse_spark_model(),
            reasoning_effort="low",
        )
    except MuseProviderUnavailable:
        return None
    except Exception:  # noqa: BLE001
        logger.info("desk_meaning act interpret failed", exc_info=True)
        return None
    parsed = _parse_act_json(result.text or "")
    if not parsed:
        return None
    return _goal_from_spark_act(parsed, raw, last_path=last_path)


def extract_inventory(text: str, *, reject: Iterable[str] = ()) -> list[str]:
    """Pull owner items from list-shape: quotes, commas, and 'and' groups.

    Not a catalog of 'that says' / 'containing'. The first clause is the
    speech act; the last content noun of a long first chunk is the first item.
    """

    raw = (text or "").strip()
    if not raw:
        return []
    deny = {token.strip().lower() for token in reject if str(token).strip()}
    deny.update(reject_terms(raw))
    quoted = [
        item
        for item in (_clean_item(span, deny) for span in re.findall(r"[\"']([^\"']{1,80})[\"']", raw))
        if item
    ]
    stripped = _DEST_PP.sub(" ", raw)
    parts = [part.strip(" .,") for part in _INVENTORY_SPLIT.split(stripped) if part.strip(" .,")]
    found: list[str] = []
    if len(parts) >= 2:
        for part in parts:
            if _ACT_CHUNK.search(part):
                continue
            item = _chunk_item(part, deny)
            if item:
                found.append(item)
    if len(quoted) >= 2:
        return _dedupe(quoted)
    if len(found) >= 2:
        return _dedupe(found)
    if len(quoted) == 1:
        return quoted
    return []


def leftover_needs_model(text: str, items: list[str], *, label: str = "") -> bool:
    """True when the utterance still has payload the list-shape missed."""

    if items:
        return False
    deny = reject_terms(text, label)
    stripped = _DEST_PP.sub(" ", text or "")
    words = [
        word.lower()
        for word in re.findall(r"[A-Za-z0-9][\w+\-]*", stripped)
        if word.lower() not in deny
    ]
    return len(words) >= 2


async def resolve_write_body(
    utterance: str,
    *,
    proposed: str = "",
    label: str = "",
    receipt: str = "",
) -> tuple[str, str, list[str]]:
    """Return (body, source, items). Body is never the kind-name."""

    deny = reject_terms(utterance, label)
    items = extract_inventory(utterance, reject=deny)
    generate = wants_generated_contents(utterance, items, label=label)
    if generate:
        spark_items = await spark_inventory(
            utterance, label=label or occasion_label(utterance) or "", generate=True
        )
        if spark_items:
            return "\n".join(spark_items), "spark", spark_items
        return "", "spark_empty", []
    if items:
        return "\n".join(items), "inventory", items
    echo = is_kind_echo(proposed, label) or is_request_payload(proposed)
    if proposed.strip() and not echo:
        proposed_items = [
            re.sub(r"^[\-\*\d\.\)\s]+", "", line).strip()
            for line in proposed.splitlines()
            if line.strip() and not is_kind_echo(line, label)
        ]
        if proposed_items:
            return "\n".join(proposed_items), "literal", proposed_items
    if leftover_needs_model(utterance, items, label=label) or wants_generated_contents(
        utterance, items, label=label
    ):
        spark_items = await spark_inventory(
            utterance,
            label=label,
            generate=wants_generated_contents(utterance, items, label=label),
        )
        if spark_items:
            return "\n".join(spark_items), "spark", spark_items
    if receipt in {"named_list", "dated_note"} or echo:
        return "", "empty", []
    return (proposed or "", "literal", [])


async def spark_inventory(
    utterance: str, *, label: str = "", generate: bool = False
) -> list[str]:
    """Ask Muse Spark 1.3 for the lines to write. Empty if Spark cannot run."""

    from app.gateway.muse import (
        MuseProviderUnavailable,
        muse_spark_key_loaded,
        muse_spark_model,
    )

    if not muse_spark_key_loaded():
        return []
    deny = reject_terms(utterance, label)
    kind = label.strip() or "the list or note"
    if generate:
        extract_rule = (
            "The owner described a situation or a kind of list and asked you to "
            "propose the contents. The situation (a flight, interview, trip, etc.) "
            "is the reason for the list, not a line in it. Propose 6-12 concrete "
            "real-world items one would actually need, in one list. Never put the "
            "occasion phrase itself, the kind, filename, folder "
            "(desktop/documents), file extensions (.txt/.md), or words like "
            "list/note/file/verify into items."
        )
    else:
        extract_rule = (
            "Items are the owner's named things, tasks, or lines. "
            f"The kind/title is {kind!r} — never put the kind, filename, folder "
            "(desktop/documents), or words like list/note into items. "
            "If they only asked to create an empty list or named no contents, "
            'return {"items": [], "empty": true}.'
        )
    try:
        from app.contracts import ChatMessage
        from app.gateway.muse_spark import muse_spark_provider

        result = await muse_spark_provider().chat_structured(
            [
                ChatMessage(
                    role="system",
                    content=(
                        "You extract the contents Evie should write into a local list or note. "
                        'Return JSON {"items": ["..."], "empty": false}. '
                        + extract_rule
                    ),
                ),
                ChatMessage(role="user", content=(utterance or "")[:4000]),
            ],
            schema=_PAYLOAD_SCHEMA,
            schema_name="desk_payload",
            model=muse_spark_model(),
            reasoning_effort="low",
        )
    except MuseProviderUnavailable:
        logger.info("desk_meaning spark unavailable")
        return []
    except Exception:  # noqa: BLE001 - payload miss must not write the kind-name
        logger.info("desk_meaning spark failed", exc_info=True)
        return []
    parsed = _parse_items_json(result.text or "")
    cleaned: list[str] = []
    for item in parsed:
        token = _clean_item(item, deny)
        if token:
            cleaned.append(token)
    return _dedupe(cleaned)


def _chunk_item(part: str, deny: set[str]) -> str | None:
    words = re.findall(r"[A-Za-z0-9][\w'+\-]*", part or "")
    while words and words[0].lower() in deny:
        words.pop(0)
    while words and words[-1].lower() in deny:
        words.pop()
    content = [word for word in words if word.lower() not in deny]
    if not content:
        return None
    if len(content) > 3:
        content = content[-2:]
    if len(words) > 6:
        content = content[-1:]
    token = " ".join(content).strip()
    return _clean_item(token, deny)


def _clean_item(raw: str, deny: set[str]) -> str | None:
    token = re.sub(r"^(?:the|a|an|my)\s+", "", (raw or "").strip(" .,'\""), flags=re.I).strip()
    if not token or len(token) > 80:
        return None
    lowered = token.lower()
    if lowered in deny:
        return None
    if re.fullmatch(r"(?:list|note|file|desktop|documents)s?", lowered):
        return None
    if re.fullmatch(r"\.\w{1,8}", token):
        return None
    if re.search(r"\.\w{1,8}$", token) and len(token) <= 24:
        return None
    if "/" in token or "\\" in token:
        return None
    words = lowered.split()
    if words and all(word in deny or word in _WHEN_WORD for word in words):
        return None
    return token


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _parse_items_json(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return [str(item) for item in items if str(item).strip()]
        body = str(data.get("content") or data.get("body") or "")
        return [line.strip() for line in body.splitlines() if line.strip()]
    if isinstance(data, list):
        return [str(item) for item in data if str(item).strip()]
    return []


def _parse_act_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _goal_from_spark_act(
    data: dict[str, Any], raw: str, *, last_path: str | None
) -> dict[str, Any] | None:
    act = str(data.get("act") or "").strip().lower()
    if act in {"", "chat"}:
        return None
    items = [str(item).strip() for item in (data.get("items") or []) if str(item).strip()]
    label = str(data.get("label") or "").strip()
    body = str(data.get("body") or "").strip()
    when = str(data.get("when") or "").strip()
    who = str(data.get("who") or "").strip()
    deny = reject_terms(raw, label)
    cleaned: list[str] = []
    for item in items:
        token = _clean_item(item, deny)
        if token:
            cleaned.append(token)
    items = cleaned
    if act == "write_list":
        from app.ev.desk_acts import named_list_write_goal

        return {"channel": "file", "goal": named_list_write_goal(raw, label or "list", items)}
    if act == "write_note":
        from app.ev.desk_acts import meaning_note_write_goal

        return {
            "channel": "file",
            "goal": meaning_note_write_goal(raw, body or "\n".join(items), items, when=when),
        }
    if act in {"append", "checkoff", "undo", "read"}:
        from app.ev.desk_scene import last_note_object, referent_file_path

        note = last_note_object()
        path = str((note or {}).get("path") or last_path or referent_file_path() or "")
        if act == "undo":
            return {"channel": "file", "goal": {"action": "undo", "goal": raw, "receipt": "undo"}}
        if not path:
            return None
        if act == "append" and items:
            return {
                "channel": "file",
                "goal": {
                    "action": "append",
                    "path": path,
                    "content": "\n".join(items),
                    "items": items,
                    "receipt": "append",
                    "goal": raw,
                },
            }
        if act == "checkoff" and items:
            return {
                "channel": "file",
                "goal": {
                    "action": "checkoff",
                    "path": path,
                    "items": items,
                    "instruction": raw,
                    "goal": raw,
                    "receipt": "checkoff",
                },
            }
        if act == "read":
            return {
                "channel": "file",
                "goal": {"action": "read", "path": path, "query": Path(path).name, "goal": raw},
            }
        return None
    if act == "remind":
        text_body = body or "\n".join(items) or raw
        return {
            "channel": "tool",
            "name": "set_reminder",
            "args": {"text": text_body[:2000], "when": when or raw[:128]},
        }
    if act == "text" and who:
        from app.ev.send_intent import channel_from_text

        text_args: dict[str, object] = {
            "to": who,
            "text": (body or "\n".join(items) or raw)[:4000],
        }
        text_channel = channel_from_text(raw)
        if text_channel:
            text_args["channel"] = text_channel
        return {
            "channel": "tool",
            "name": "send_message",
            "args": text_args,
        }
    return None
