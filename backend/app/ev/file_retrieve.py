"""Generic retrieve: get / open / read owner things on this Mac.

Not a catalog of document types. The owner names a class ("IDs") or a thing
("lease", "government ID"); Evie asks when the class is bare, binds the next
turn as a specifier, then searches filenames, folders, and content using the
owner's words. Unique hit opens or reads. Several hits ask which file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Classes of *things*, not 200 document types. Specifiers stay owner words.
_CLASS_SYNONYMS: dict[str, tuple[str, ...]] = {
    "id": ("id", "ids", "identification", "identity"),
    "document": ("document", "documents", "doc", "docs", "paper", "papers", "pdf", "pdfs"),
    "file": ("file", "files"),
    "folder": ("folder", "folders", "directory", "directories"),
    "photo": (
        "photo",
        "photos",
        "picture",
        "pictures",
        "image",
        "images",
        "pic",
        "pics",
        "scan",
        "scans",
    ),
    "record": ("record", "records"),
}
_CLASS_LOOKUP: dict[str, str] = {
    token: key for key, tokens in _CLASS_SYNONYMS.items() for token in tokens
}
_CLASS_LABEL = {
    "id": "ID",
    "document": "document",
    "file": "file",
    "folder": "folder",
    "photo": "photo",
    "record": "record",
}
_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "my",
        "me",
        "mine",
        "our",
        "please",
        "just",
        "some",
        "any",
        "that",
        "this",
        "those",
        "these",
        "one",
        "ones",
        "it",
        "of",
        "for",
        "on",
        "in",
        "from",
        "to",
        "and",
        "or",
        "called",
        "named",
        "titled",
        "copy",
        "copies",
        "thing",
        "things",
        "stuff",
        "here",
        "there",
        "now",
        "also",
        "too",
        "laptop",
        "mac",
        "macbook",
        "computer",
        "machine",
        "desktop",
        "downloads",
        "home",
        "local",
        "locally",
    }
)
_LIFE_STEAL = re.compile(
    r"\b(?:messages?|texts?|imessages?|sms|mails?|emails?|inbox|"
    r"calls?|voicemails?|calendar|reminders?|alarms?|contacts?|"
    r"weather|forecast|sandwich)\b",
    re.I,
)
_GET_RE = re.compile(
    r"\b(?:get|give|grab|fetch|retrieve|pull\s+up|bring\s+up|bring|"
    r"show(?:\s+me)?|open|read|peek)\s+"
    r"(?:me\s+)?(?:my|the|our)\s+",
    re.I,
)
_FIND_RE = re.compile(
    r"\b(?:find|locate|look(?:\s+up|\s+for)|where(?:'s| is))\s+"
    r"(?:me\s+)?(?:my|the|our)\s+",
    re.I,
)
_VERB_OPEN = re.compile(
    r"\b(?:get|give|grab|fetch|retrieve|pull\s+up|bring\s+up|bring|show|open)\b",
    re.I,
)
_VERB_READ = re.compile(r"\b(?:read|peek|what(?:'s| is) (?:in|inside))\b", re.I)
_VERB_FIND = re.compile(
    r"\b(?:find|locate|look(?:\s+up|\s+for)|where(?:'s| is))\b",
    re.I,
)
_FILE_CUE = re.compile(
    r"\b(?:files?|documents?|folders?|pdfs?|laptop|mac|macbook|computer|"
    r"desktop|downloads|icloud)\b",
    re.I,
)
_CANCEL_RE = re.compile(
    r"^(?:never mind|forget it|forget that|cancel|stop|that's all)\s*[.!?]*$",
    re.I,
)
_GOT_FRAME = re.compile(
    r"^(?:please\s+)?(?:i(?:['’]?m|\s+am)?\s+)?"
    r"(?:got|picked\s+up|bought|grabbed|found|finished|did|done\s+with)\b",
    re.I,
)
_LEAD_STRIP = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+|would you\s+)?"
    r"(?:get|give|grab|fetch|retrieve|pull\s+up|bring\s+up|bring|"
    r"show(?:\s+me)?|open|read|peek|find|locate|look(?:\s+up|\s+for)|"
    r"where(?:'s| is))\s+"
    r"(?:me\s+)?(?:up\s+)?(?:my|the|our|a|an)?\s*",
    re.I,
)
_TAIL_STRIP = re.compile(
    r"\s+(?:and\s+open(?:\s+it)?|for me|please|on (?:my |the )?"
    r"(?:laptop|mac|macbook|computer|machine|desktop|documents|downloads))\s*$",
    re.I,
)
_DOC_EXT = frozenset(
    {
        ".pdf",
        ".jpg",
        ".jpeg",
        ".png",
        ".heic",
        ".webp",
        ".tif",
        ".tiff",
        ".gif",
        ".doc",
        ".docx",
        ".txt",
        ".rtf",
        ".md",
        ".pages",
        ".html",
    }
)
_FOLDER_ONLY = frozenset(
    {
        "desktop",
        "documents",
        "downloads",
        "pictures",
        "photos",
        "movies",
        "music",
    }
)
_JOB_TTL_S = 20 * 60


@dataclass(frozen=True)
class RetrieveIntent:
    verb: str
    action: str
    cls: str
    query: str
    specified: bool
    goal: str


def looks_like_retrieve_task(text: str) -> bool:
    raw = _prep(text)
    if not raw or _LIFE_STEAL.search(raw):
        return False
    if pending_retrieve() is not None and looks_like_retrieve_followup(raw):
        return True
    return parse_retrieve_intent(raw) is not None


def looks_like_retrieve_followup(text: str) -> bool:
    raw = _prep(text)
    job = pending_retrieve()
    if not raw or job is None:
        return False
    if _LIFE_STEAL.search(raw) or _GOT_FRAME.search(raw) or _CANCEL_RE.search(raw):
        return False
    from app.ev.luna_code import looks_like_code_request
    from app.ev.send_intent import parse_send_intent
    from app.search.live import is_weather_query

    if parse_send_intent(raw) or is_weather_query(raw) or looks_like_code_request(raw):
        return False
    from app.ev.tool_select import parse_heading_out

    if parse_heading_out(raw):
        return False
    fresh = parse_retrieve_intent(raw)
    if fresh is not None:
        return True
    if len(raw.split()) > 12:
        return False
    if re.search(r"\b(?:write|create|make|append|delete|rename|copy|move)\b", raw, re.I):
        return False
    return bool(_specifier_text(raw, job.get("cls") or ""))


def parse_retrieve_intent(text: str) -> RetrieveIntent | None:
    raw = _prep(text)
    if not raw or _LIFE_STEAL.search(raw):
        return None
    get_hit = _GET_RE.search(raw)
    find_hit = _FIND_RE.search(raw)
    if not get_hit and not find_hit:
        return None
    find_only = bool(find_hit) and not get_hit
    noun = _noun_phrase(raw)
    if not noun:
        return None
    cls, specifier = _split_class(noun)
    if not specifier and noun in _FOLDER_ONLY:
        return None
    if find_only and not cls and not _FILE_CUE.search(raw):
        return None
    if not cls and not specifier:
        return None
    specified = bool(specifier)
    action = _action_for(raw)
    verb = "find" if find_only else ("read" if action == "read" else "get")
    return RetrieveIntent(
        verb=verb,
        action=action,
        cls=cls,
        query=specifier,
        specified=specified,
        goal=raw,
    )


def parse_retrieve_goal(text: str) -> dict[str, Any] | None:
    raw = _prep(text)
    if not raw:
        return None
    from app.ev.desk_scene import pending_choice

    choice = pending_choice()
    if choice is not None:
        from app.ev.desk_acts import parse_pending_choice

        picked = parse_pending_choice(raw)
        if picked is not None:
            if str(choice.get("kind") or "") == "retrieve":
                return picked
            return None
    job = pending_retrieve()
    if _CANCEL_RE.search(raw) and job is not None:
        return {
            "action": "ask_which",
            "kind": "retrieve",
            "clear_retrieve": True,
            "spoken": "Okay, I won't look.",
            "goal": raw,
        }
    fresh = parse_retrieve_intent(raw)
    if fresh is not None and (job is None or fresh.specified or fresh.cls != str(job.get("cls") or "")):
        if not fresh.specified:
            return _ask_which_payload(fresh.cls, fresh.verb, fresh.action, raw)
        return _search_payload(fresh.action, fresh.cls, fresh.query, fresh.verb, raw)
    if job is not None and looks_like_retrieve_followup(raw):
        cls = str(job.get("cls") or "")
        specifier = _specifier_text(raw, cls)
        if not specifier:
            return _ask_which_payload(cls, str(job.get("verb") or "get"), str(job.get("do") or "open"), raw)
        action = str(job.get("do") or "open")
        verb = str(job.get("verb") or "get")
        return _search_payload(action, cls, specifier, verb, raw)
    return None


def execute_retrieve(arguments: dict[str, Any]) -> dict[str, Any]:
    from app.ev.laptop_files import TEXT_EXTENSIONS, _fail, _open_file, _read_file, path_denied

    args = dict(arguments or {})
    action = str(args.get("action") or "open").strip().lower()
    if action == "search":
        do = "search"
    elif action == "read":
        do = "read"
    else:
        do = "open"
    path_hint = str(args.get("path") or "").strip()
    query = str(args.get("query") or "").strip()
    cls = str(args.get("cls") or "").strip().lower()
    if path_hint:
        target = Path(path_hint).expanduser()
        try:
            exists = target.exists()
        except OSError:
            exists = False
        if exists and path_denied(target) is None:
            clear_pending_retrieve()
            if target.is_dir() or do == "open" or target.suffix.lower() not in TEXT_EXTENSIONS:
                return _open_file(target)
            if do == "read":
                return _read_file(target)
            return _open_file(target)
    named = _named_alias(query)
    if named is not None:
        clear_pending_retrieve()
        if do == "read" and named.suffix.lower() in TEXT_EXTENSIONS:
            return _read_file(named)
        return _open_file(named)
    hits = collect_retrieve_hits(query, cls=cls, want_folder=cls == "folder" or "folder" in query.lower())
    picked, rest, reason = pick_retrieve(hits, query, cls)
    if reason == "missing":
        remember_retrieve_job(args)
        spoken = _miss_spoken(query, cls)
        return {
            **_fail("not_found", spoken),
            "action": "retrieve",
            "keep_retrieve": True,
        }
    if reason == "ambiguous" or (picked is None and rest):
        shown = rest[:6] if rest else hits[:6]
        return _ask_files(shown, do, cls, query, str(args.get("goal") or query))
    if picked is None:
        remember_retrieve_job(args)
        return {
            **_fail("not_found", _miss_spoken(query, cls)),
            "action": "retrieve",
            "keep_retrieve": True,
        }
    clear_pending_retrieve()
    from app.ev.desk_scene import clear_pending_choice

    clear_pending_choice()
    if picked.is_dir() or do != "read" or picked.suffix.lower() not in TEXT_EXTENSIONS:
        result = _open_file(picked)
        if do == "search":
            result["spoken"] = f"I found {picked.name} in {picked.parent.name}."
            result["action"] = "search"
        elif reason == "class_only":
            result["spoken"] = (
                f"Nothing named {query or _CLASS_LABEL.get(cls, 'that')} — "
                f"opened {picked.name} from {picked.parent.name}."
            )
        return result
    return _read_file(picked)


def collect_retrieve_hits(
    query: str,
    *,
    cls: str = "",
    want_folder: bool = False,
) -> list[Path]:
    from app.ev.laptop_files import allowed_roots, path_denied

    specifiers = _query_tokens(query)
    class_tokens = list(_CLASS_SYNONYMS.get(cls, ()))
    needles = specifiers or class_tokens
    if not needles and not class_tokens:
        return []
    roots = allowed_roots()
    seen: set[Path] = set()
    hits: list[Path] = []

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        if resolved in seen or path_denied(resolved) is not None:
            return
        try:
            is_dir = resolved.is_dir()
            is_file = resolved.is_file()
        except OSError:
            return
        if want_folder:
            if not is_dir:
                return
        elif not is_file:
            return
        if _is_noise(resolved):
            return
        if retrieve_score(resolved, specifiers, class_tokens) <= 0:
            return
        seen.add(resolved)
        hits.append(resolved)

    for token in needles[:6]:
        if len(token) < 2:
            continue
        for path in _spotlight_paths(token, roots, files_only=not want_folder):
            add(path)
    for path in _walk_owner_paths(want_folder=want_folder):
        add(path)
        if len(hits) >= 80:
            break
    if specifiers and not hits:
        for token in specifiers[:3]:
            if len(token) < 3:
                continue
            for path in _spotlight_content(token, roots):
                add(path)
    return hits


def retrieve_score(path: Path, specifiers: list[str], class_tokens: list[str]) -> int:
    from app.ev.laptop_files import _search_hay

    name = path.name.lower()
    stem_hay = _search_hay(path.stem)
    parent_hay = _search_hay(path.parent.name)
    hay = _path_hay(path)
    score = 0
    for token in specifiers:
        current = _token_score(token, name, stem_hay, parent_hay, hay)
        if current > 0:
            score += current
        elif _fuzzy_in(token, stem_hay) or _fuzzy_in(token, name):
            score += 70
        elif _fuzzy_in(token, parent_hay) or _fuzzy_in(token, hay):
            score += 36
    for token in class_tokens:
        current = _token_score(token, name, stem_hay, parent_hay, hay)
        if current >= 70:
            score += 28
        elif current >= 40:
            score += 18
        elif current > 0:
            score += 12
    if score <= 0:
        return 0
    if path.suffix.lower() in _DOC_EXT:
        score += 8
    parent = path.parent.name.lower()
    if parent == "desktop":
        score += 12
    elif parent in {"documents", "docs", "ids", "id"}:
        score += 14
    elif parent == "downloads":
        score += 4
    return score


def pick_retrieve(
    hits: list[Path],
    query: str,
    cls: str,
) -> tuple[Path | None, list[Path], str]:
    specifiers = _query_tokens(query)
    class_tokens = list(_CLASS_SYNONYMS.get(cls, ()))
    if not hits:
        return None, [], "missing"
    ranked = sorted(
        hits,
        key=lambda item: (retrieve_score(item, specifiers, class_tokens), -len(item.name)),
        reverse=True,
    )
    if specifiers:
        named = [
            item
            for item in ranked
            if _specifier_on_name(item, specifiers)
        ]
        placed = [
            item
            for item in ranked
            if item not in named and _specifier_on_path(item, specifiers)
        ]
        if named:
            return _unique_or_ask(named, specifiers, class_tokens, "specified")
        if placed:
            return _unique_or_ask(placed, specifiers, class_tokens, "specified")
        class_hits = [
            item
            for item in ranked
            if retrieve_score(item, [], class_tokens) > 0
        ]
        if class_hits:
            picked, rest, reason = _unique_or_ask(class_hits, [], class_tokens, "class_only")
            if reason == "unique":
                return picked, rest, "class_only"
            return picked, rest, reason
        return None, [], "missing"
    return _unique_or_ask(ranked, specifiers, class_tokens, "specified")


def pending_retrieve() -> dict[str, Any] | None:
    from app.ev.desk_scene import pending_retrieve_job

    job = pending_retrieve_job()
    if not isinstance(job, dict):
        return None
    created = float(job.get("created_at") or 0)
    if created and (_now() - created) > _JOB_TTL_S:
        clear_pending_retrieve()
        return None
    return dict(job)


def set_pending_retrieve(payload: dict[str, Any] | None) -> None:
    from app.ev.desk_scene import set_pending_retrieve_job

    set_pending_retrieve_job(payload)


def clear_pending_retrieve() -> None:
    set_pending_retrieve(None)


def remember_retrieve_job(args: dict[str, Any]) -> None:
    if args.get("clear_retrieve"):
        clear_pending_retrieve()
        return
    cls = str(args.get("cls") or "").strip()
    verb = str(args.get("verb") or "get")
    action = str(args.get("do") or args.get("action") or "open")
    if action == "ask_which":
        action = "open"
    query = str(args.get("query") or "").strip()
    set_pending_retrieve(
        {
            "cls": cls,
            "verb": verb,
            "do": action if action in {"open", "read", "search"} else "open",
            "query": query,
            "goal": str(args.get("goal") or ""),
            "created_at": _now(),
        }
    )


def _ask_which_payload(cls: str, verb: str, action: str, goal: str) -> dict[str, Any]:
    label = _CLASS_LABEL.get(cls, "one")
    spoken = (
        f"Which {label}? Name it the way it is on your Mac, or say what kind."
        if cls
        else "Which one? Name it the way it is on your Mac."
    )
    return {
        "action": "ask_which",
        "kind": "retrieve",
        "cls": cls,
        "verb": verb,
        "do": action,
        "spoken": spoken,
        "goal": goal,
    }


def _search_payload(action: str, cls: str, query: str, verb: str, goal: str) -> dict[str, Any]:
    return {
        "action": action,
        "retrieve": True,
        "cls": cls,
        "query": query,
        "verb": verb,
        "goal": goal,
    }


def _ask_files(
    hits: list[Path],
    do: str,
    cls: str,
    query: str,
    goal: str,
) -> dict[str, Any]:
    candidates = [{"path": str(item), "label": item.stem.replace("_", " ").replace("-", " ")} for item in hits]
    names = ", ".join(item.name for item in hits[:4])
    extra = f" {len(hits) - 4} more." if len(hits) > 4 else ""
    spoken = f"I found {names}.{extra} Which one?"
    remember_retrieve_job({"cls": cls, "verb": "get", "do": do, "query": query, "goal": goal})
    return {
        "action": "ask_which",
        "kind": "retrieve",
        "cls": cls,
        "verb": "get",
        "do": do,
        "query": query,
        "candidates": candidates,
        "spoken": spoken,
        "goal": goal,
    }


def _unique_or_ask(
    ranked: list[Path],
    specifiers: list[str],
    class_tokens: list[str],
    reason: str,
) -> tuple[Path | None, list[Path], str]:
    if not ranked:
        return None, [], "missing"
    if len(ranked) == 1:
        return ranked[0], [], "unique" if reason != "class_only" else "class_only"
    top = retrieve_score(ranked[0], specifiers, class_tokens)
    second = retrieve_score(ranked[1], specifiers, class_tokens)
    if top >= second + 18 and top >= 50:
        return ranked[0], ranked[1:], "unique" if reason != "class_only" else "class_only"
    return None, ranked, "ambiguous"


def _specifier_on_name(path: Path, specifiers: list[str]) -> bool:
    from app.ev.laptop_files import _search_hay

    hay = f"{path.name.lower()} {_search_hay(path.stem)}"
    return any(_token_in(token, hay) or _fuzzy_in(token, hay) for token in specifiers)


def _specifier_on_path(path: Path, specifiers: list[str]) -> bool:
    hay = _path_hay(path)
    return any(_token_in(token, hay) or _fuzzy_in(token, hay) for token in specifiers)


def _token_score(token: str, name: str, stem_hay: str, parent_hay: str, hay: str) -> int:
    if not token:
        return 0
    compact = re.sub(r"[\s_\-]+", "", token)
    if stem_hay == token or name.startswith(token) and name[len(token) : len(token) + 1] in {"", ".", "-", "_"}:
        return 100
    words = stem_hay.split()
    if token in words:
        return 80
    if token in stem_hay or token in name:
        return 70
    if compact and compact in re.sub(r"[\s_\-]+", "", name):
        return 68
    if token in parent_hay.split() or token in parent_hay:
        return 42
    if token in hay:
        return 36
    return 0


def _token_in(token: str, hay: str) -> bool:
    if not token:
        return False
    if token in hay:
        return True
    compact = re.sub(r"[\s_\-]+", "", token)
    return bool(compact and compact in re.sub(r"[\s_\-]+", "", hay))


def _fuzzy_in(token: str, hay: str) -> bool:
    if len(token) < 5:
        return False
    for word in re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", hay.lower()):
        compact = word.replace("-", "").replace("_", "")
        if _edits_at_most_one(token, word) or _edits_at_most_one(token, compact):
            return True
    return False


def _edits_at_most_one(left: str, right: str) -> bool:
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right, strict=True)) == 1
    if len(left) > len(right):
        left, right = right, left
    i = j = diffs = 0
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
            continue
        diffs += 1
        if diffs > 1:
            return False
        j += 1
    return True


def _path_hay(path: Path) -> str:
    from app.ev.laptop_files import _search_hay

    parts = [_search_hay(part) for part in path.parts[-5:]]
    return " ".join(parts).lower()


def _word_tokens(text: str) -> list[str]:
    return [part for part in re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", text or "") if part]


def _query_tokens(query: str) -> list[str]:
    raw = _prep(query)
    tokens: list[str] = []
    for part in _word_tokens(raw):
        if part in _STOP or len(part) < 2:
            continue
        if part not in tokens:
            tokens.append(part)
        if "-" in part or "_" in part:
            for bit in re.split(r"[-_]", part):
                if bit and bit not in _STOP and bit not in tokens and len(bit) > 1:
                    tokens.append(bit)
        stemmed = _stem(part)
        if stemmed != part and stemmed not in tokens and stemmed not in _STOP:
            tokens.append(stemmed)
    if len(tokens) >= 2:
        joined = "".join(tokens)
        if joined not in tokens:
            tokens.append(joined)
    return tokens[:8]


def _stem(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def _split_class(noun: str) -> tuple[str, str]:
    tokens = _word_tokens(noun)
    keys: list[str] = []
    kept: list[str] = []
    for token in tokens:
        key = _CLASS_LOOKUP.get(token, "")
        if key:
            keys.append(key)
            continue
        if token in _STOP:
            continue
        kept.append(token)
    cls = ""
    if "folder" in keys:
        cls = "folder"
        for key in keys:
            if key != "folder" and key not in kept:
                kept.append(key)
    elif keys:
        cls = keys[0]
    specifier = " ".join(kept).strip()
    return cls, specifier


def _noun_phrase(text: str) -> str:
    raw = _TAIL_STRIP.sub("", _prep(text))
    stripped = _LEAD_STRIP.sub("", raw).strip(" .,'\"")
    stripped = re.sub(r"^(?:my|the|our|a|an)\s+", "", stripped)
    return stripped[:80]


def _specifier_text(text: str, cls: str) -> str:
    raw = _TAIL_STRIP.sub("", _prep(text))
    raw = _LEAD_STRIP.sub("", raw)
    raw = re.sub(r"^(?:it'?s|it is|the|my|our|a|an)\s+", "", raw)
    raw = re.sub(r"\s+one\s*$", "", raw)
    if cls:
        for token in _CLASS_SYNONYMS.get(cls, ()):
            raw = re.sub(rf"\b{re.escape(token)}\b", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" .,'\"")
    return raw[:80]


def _action_for(text: str) -> str:
    if _VERB_READ.search(text) and not _VERB_OPEN.search(text):
        return "read"
    if _VERB_FIND.search(text) and not _VERB_OPEN.search(text):
        return "search"
    return "open"


def _miss_spoken(query: str, cls: str) -> str:
    label = query or _CLASS_LABEL.get(cls, "that")
    return f"I couldn't find {label} on this Mac. What's the file or folder called?"


def _named_alias(query: str) -> Path | None:
    if not query:
        return None
    from app.ev.desk_names import resolve_alias, resolve_spoken_file

    found = resolve_alias(query)
    if found is not None:
        return Path(found)
    spoken = resolve_spoken_file(query)
    if spoken is not None:
        return spoken[0]
    return None


def _spotlight_paths(token: str, roots: list[Path], *, files_only: bool) -> list[Path]:
    from app.ev.laptop_files import _spotlight_name_hits

    hits = _spotlight_name_hits(token, roots)
    if files_only:
        return hits
    extra: list[Path] = []
    for item in hits:
        parent = item.parent
        extra.append(parent)
    return hits + extra


def _spotlight_content(token: str, roots: list[Path]) -> list[Path]:
    from app.ev.laptop_files import _spotlight_content_hits

    return _spotlight_content_hits(token, roots)


def _walk_owner_paths(*, want_folder: bool) -> list[Path]:
    from app.ev.laptop_files import (
        _SKIP_DIR_NAMES,
        _search_depth,
        allowed_roots,
        path_denied,
    )

    found: list[Path] = []
    for root in allowed_roots():
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        if not resolved.is_dir():
            continue
        prefix_len = len(resolved.parts)
        max_depth = _search_depth(resolved)
        try:
            for dirpath, dirnames, filenames in _walk(resolved):
                current = Path(dirpath)
                depth = len(current.parts) - prefix_len
                dirnames[:] = [
                    name
                    for name in dirnames
                    if name not in _SKIP_DIR_NAMES and not name.startswith(".")
                ]
                if depth >= max_depth:
                    dirnames.clear()
                if want_folder and depth > 0 and path_denied(current) is None:
                    found.append(current)
                for name in filenames:
                    if name.startswith("."):
                        continue
                    child = current / name
                    if path_denied(child) is not None:
                        continue
                    found.append(child)
                    if len(found) >= 400:
                        return found
        except OSError:
            continue
    return found


def _walk(root: Path):
    import os

    return os.walk(root)


def _is_noise(path: Path) -> bool:
    from app.ev.laptop_files import _SEARCH_NOISE_PARTS

    return any(part.lower() in _SEARCH_NOISE_PARTS for part in path.parts)


def _prep(text: str) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip())
    raw = raw.replace("’", "'")
    raw = re.sub(r"\bid's\b", "ids", raw, flags=re.I)
    return raw.lower()


def _now() -> float:
    import time

    return time.time()
