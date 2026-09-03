"""Live desk twin: named objects, packets, deixis slots, and a mutation ledger.

desk_names is the compatibility loader. This module is the scene: several
live objects, not one last_path pointer. Not RAG.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

from app.memory.paths import atomic_write_json, ensure_tree, memory_root, read_json

logger = logging.getLogger("ev.desk_scene")

MAX_OBJECTS = 80
MAX_ALIASES = 10
MAX_LEDGER = 40
MAX_HELD = 24
GENERIC_ALIASES = frozenset(
    {
        "note",
        "file",
        "document",
        "pdf",
        "text",
        "item",
        "this",
        "that",
        "it",
        "list",
        "stuff",
        "thing",
        "desktop",
        "documents",
        "downloads",
        "other",
        "packet",
    }
)
DEIXIS_DEST = frozenset({"it", "that", "this", "file", "note"})
GROCERY_HINT = re.compile(
    r"\b(?:grocery|groceries|shopping list)\b"
    r"|\bbuy\b.{0,48}\b(?:milk|eggs|bread|bananas|apples|butter|coffee)\b"
    r"|\b(?:milk|eggs)\b.{0,24}\b(?:milk|eggs)\b",
    re.I,
)
BIND_RE = re.compile(
    r"(?:"
    r"(?:that(?:'s| is)|this is)\s+(?:my\s+|the\s+|called\s+)?"
    r"|(?:call this|name this|remember this as|remember that as)\s+(?:my\s+|the\s+)?"
    r")(.+?)$",
    re.I,
)
ADD_TO_NAMED_RE = re.compile(
    r"\b(?:add|append)\s+(?:the (?:text|line|words?)\s+)?(.+?)\s+to\s+(?:the\s+|my\s+)?(.+?)$",
    re.I,
)
PACKET_PUT_RE = re.compile(
    r"\b(?:put|add|file)\s+(?:it|that|this|them)?\s*(?:in(?:to)?|with)\s+(?:the\s+|my\s+)?(.+?)$",
    re.I,
)
BELONGS_RE = re.compile(
    r"\bbelongs to\s+(?:the\s+|my\s+)?(.+)$",
    re.I,
)
ALSO_RE = re.compile(
    r"^(?:and\s+)?also(?:\s+add)?\s+(.+)$",
    re.I,
)
OTHER_RE = re.compile(
    r"\b(?:no[,.]?\s+)?(?:the\s+)?other(?:\s+(?:pdf|file|one|document))?\b",
    re.I,
)
ADD_SLOT_RE = re.compile(
    r"^\s*(?:add|file)\s+(?:it|that|this|them)\b",
    re.I,
)
OPEN_SLOT_RE = re.compile(
    r"^\s*open\s+(?:it|that|this|the new one)\b",
    re.I,
)
YES_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|ok|okay|do it|add it|file it|yes please)\b",
    re.I,
)
LAND_NAME = re.compile(
    r"i-?20|resume|invoice|receipt|passport|visa|ds-?160|w-?2|i-?94",
    re.I,
)
FRAGMENT_RE = re.compile(
    r"\b(?:add (?:it|that|this)|the other|put (?:it|that|this) in|"
    r"belongs to|also\b|visa packet|file it|the new one)\b",
    re.I,
)

_STORE: dict[str, Any] | None = None
_SEEN_MTIMES: dict[str, float] = {}


def scene_store_path() -> Path:
    ensure_tree()
    return memory_root() / "catalog" / "desk-scene.json"


def names_store_path() -> Path:
    ensure_tree()
    return memory_root() / "catalog" / "desk-names.json"


def reset_desk_names() -> None:
    global _STORE, _SEEN_MTIMES
    _STORE = None
    _SEEN_MTIMES = {}
    for path in (scene_store_path(), names_store_path()):
        if path.exists():
            path.unlink()


reset_scene = reset_desk_names


def normalize_alias(text: str) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip().lower())
    raw = re.sub(r"^(?:the|my|a|an)\s+", "", raw)
    raw = re.sub(r"[.!?]+$", "", raw).strip(" \"'")
    return raw[:60]


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _empty_scene() -> dict[str, Any]:
    return {
        "version": 2,
        "objects": [],
        "slots": {"focus": None, "that": None, "other": None, "landed": None, "also": None},
        "ledger": [],
        "pending_offer": None,
        "session_id": None,
    }


def _load() -> dict[str, Any]:
    global _STORE
    if _STORE is not None:
        return _STORE
    payload = read_json(scene_store_path())
    if isinstance(payload, dict) and payload.get("version") == 2:
        scene = _empty_scene()
        scene.update(payload)
        scene.setdefault("objects", [])
        scene.setdefault("slots", _empty_scene()["slots"])
        scene.setdefault("ledger", [])
        _STORE = scene
        return _STORE
    legacy = read_json(names_store_path()) or {}
    scene = _empty_scene()
    for item in list(legacy.get("items") or []):
        path = str(item.get("path") or "")
        obj = {
            "id": _new_id(),
            "kind": _kind_for_path(path),
            "path": path,
            "aliases": list(item.get("aliases") or []),
            "inode": item.get("inode"),
            "name": item.get("name") or Path(path).name,
            "members": [],
            "held_items": [],
            "source": item.get("source") or "touch",
            "touched_at": float(item.get("touched_at") or time.time()),
        }
        scene["objects"].append(obj)
    _STORE = scene
    return _STORE


def _save(scene: dict[str, Any]) -> None:
    global _STORE
    scene["objects"] = list(scene.get("objects") or [])[-MAX_OBJECTS:]
    scene["ledger"] = list(scene.get("ledger") or [])[-MAX_LEDGER:]
    _STORE = scene
    atomic_write_json(scene_store_path(), scene)


def _kind_for_path(path_raw: str) -> str:
    suffix = Path(path_raw or "").suffix.lower()
    if suffix in {".txt", ".md", ".markdown", ".rtf"}:
        return "note"
    return "doc"


def _unique_aliases(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in names:
        token = normalize_alias(raw)
        if not token or token in GENERIC_ALIASES or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def inferred_aliases(path: Path, *, goal: str = "", content: str = "", query: str = "") -> list[str]:
    aliases: list[str] = []
    stem = normalize_alias(path.stem.replace("_", " ").replace("-", " "))
    if stem and stem not in GENERIC_ALIASES and stem != "evie note":
        aliases.append(stem)
    blob = f"{goal} {content} {query} {path.name}"
    if "resume" in blob.lower() or "curriculum vitae" in blob.lower():
        if "flagship" not in path.name.lower():
            aliases.extend(["resume", "cv"])
    if GROCERY_HINT.search(blob):
        aliases.extend(["grocery list", "shopping list", "groceries"])
    if query:
        q = normalize_alias(query)
        if q and q not in GENERIC_ALIASES:
            aliases.append(q)
    return _unique_aliases(aliases)


def held_items_from_content(content: str) -> list[str]:
    items: list[str] = []
    for line in (content or "").splitlines():
        raw = re.sub(r"^[\-\*\d\.\)\s]+", "", line).strip()
        if not raw:
            continue
        items.append(raw[:80])
        if len(items) >= MAX_HELD:
            break
    return items


def scene_is_live() -> bool:
    scene = _load()
    slots = scene.get("slots") or {}
    if any(slots.get(key) for key in ("focus", "that", "landed", "also")):
        return True
    return bool(scene.get("pending_offer"))


def set_session_id(session_id: str | None) -> None:
    if not session_id:
        return
    scene = _load()
    scene["session_id"] = str(session_id)
    _save(scene)


def _objects() -> list[dict[str, Any]]:
    return list(_load().get("objects") or [])


def _by_id(oid: str | None) -> dict[str, Any] | None:
    if not oid:
        return None
    for obj in _objects():
        if obj.get("id") == oid:
            return obj
    return None


def _live_path(obj: dict[str, Any]) -> Path | None:
    raw = str(obj.get("path") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_file():
        return path.resolve()
    inode = obj.get("inode")
    parent = path.parent
    if inode and parent.is_dir():
        try:
            for child in parent.iterdir():
                if child.is_file() and child.stat().st_ino == inode:
                    obj["path"] = str(child.resolve())
                    _save(_load())
                    return child.resolve()
        except OSError:
            pass
    return None


def slot_path(slot: str) -> Path | None:
    obj = slot_object(slot)
    if obj is None:
        return None
    return _live_path(obj)


def scene_file_path(*slots: str) -> Path | None:
    for slot in slots:
        obj = slot_object(slot)
        if obj is None or obj.get("kind") == "packet":
            continue
        path = _live_path(obj)
        if path is not None:
            return path
    return None


def referent_file_path(last_path: str | None = None) -> Path | None:
    """The file 'it / that / this' should hit: last path, then live scene slots."""

    raw = str(last_path or "").strip()
    if raw:
        path = Path(raw).expanduser()
        try:
            if path.is_file():
                return path.resolve()
        except OSError:
            pass
    found = scene_file_path("that", "focus", "also", "landed")
    if found is not None:
        return found
    note = last_note_object()
    if note is not None:
        return _live_path(note)
    return None


def slot_object(slot: str) -> dict[str, Any] | None:
    scene = _load()
    return _by_id((scene.get("slots") or {}).get(slot))


def _set_slot(slot: str, oid: str | None) -> None:
    scene = _load()
    slots = dict(scene.get("slots") or {})
    slots[slot] = oid
    scene["slots"] = slots
    _save(scene)


def _promote_focus(oid: str | None) -> None:
    if not oid:
        return
    scene = _load()
    slots = dict(scene.get("slots") or {})
    previous = slots.get("focus")
    incoming = _by_id(oid)
    if previous and previous != oid:
        prev = _by_id(previous)
        if incoming is None or incoming.get("kind") != "packet":
            if prev is not None and prev.get("kind") != "packet":
                slots["other"] = previous
        if prev is not None and prev.get("kind") == "note":
            slots["also"] = previous
    slots["focus"] = oid
    slots["that"] = oid
    scene["slots"] = slots
    _save(scene)


def _ledger(action: str, obj: dict[str, Any] | None, *, detail: str = "", before: str = "") -> None:
    scene = _load()
    scene.setdefault("ledger", []).append(
        {
            "at": time.time(),
            "action": action,
            "object_id": (obj or {}).get("id"),
            "path": (obj or {}).get("path"),
            "detail": detail[:120],
            "before": before[:4000],
        }
    )
    _save(scene)


def last_note_object() -> dict[str, Any] | None:
    also = slot_object("also")
    if also is not None and also.get("kind") == "note":
        return also
    notes = [obj for obj in _objects() if obj.get("kind") == "note"]
    notes.sort(key=lambda item: float(item.get("touched_at") or 0), reverse=True)
    return notes[0] if notes else None


def active_packet() -> dict[str, Any] | None:
    focus = slot_object("focus")
    if focus is not None and focus.get("kind") == "packet":
        return focus
    packets = [obj for obj in _objects() if obj.get("kind") == "packet"]
    packets.sort(key=lambda item: float(item.get("touched_at") or 0), reverse=True)
    return packets[0] if packets else None


def remember_file(
    path_raw: str | Path | None,
    *,
    goal: str = "",
    content: str = "",
    query: str = "",
    aliases: list[str] | None = None,
    source: str = "touch",
) -> dict[str, Any] | None:
    if not path_raw:
        return None
    path = Path(str(path_raw)).expanduser()
    try:
        if not path.is_file():
            return None
        path = path.resolve()
    except OSError:
        return None
    from app.ev.laptop_files import path_denied

    if path_denied(path) is not None:
        return None
    names = _unique_aliases(list(aliases or []) + inferred_aliases(path, goal=goal, content=content, query=query))
    scene = _load()
    existing = None
    kept: list[dict[str, Any]] = []
    for obj in scene.get("objects") or []:
        if str(obj.get("path") or "") == str(path):
            existing = obj
            continue
        kept.append(obj)
    now = time.time()
    inode = None
    try:
        inode = path.stat().st_ino
    except OSError:
        inode = None
    merged = _unique_aliases(list((existing or {}).get("aliases") or []) + names)
    if not merged and existing is None:
        return None
    oid = str((existing or {}).get("id") or _new_id())
    for obj in kept:
        obj["aliases"] = [
            alias
            for alias in (obj.get("aliases") or [])
            if normalize_alias(alias) not in merged
        ]
    kept = [obj for obj in kept if obj.get("kind") == "packet" or obj.get("aliases") or obj.get("members")]
    record = {
        "id": oid,
        "kind": (existing or {}).get("kind") or _kind_for_path(str(path)),
        "path": str(path),
        "aliases": merged[:MAX_ALIASES],
        "inode": inode,
        "name": path.name,
        "members": list((existing or {}).get("members") or []),
        "held_items": held_items_from_content(content)
        if content
        else list((existing or {}).get("held_items") or []),
        "source": source,
        "touched_at": now,
    }
    if content:
        record["held_items"] = held_items_from_content(content)
    kept.append(record)
    scene["objects"] = kept
    _save(scene)
    if record["kind"] == "note":
        _set_slot("also", oid)
    if source in {"open", "search", "read"}:
        _promote_focus(oid)
    elif source in {"write", "append", "edit"}:
        _set_slot("also", oid)
        if not (_load().get("slots") or {}).get("focus"):
            _promote_focus(oid)
    else:
        _promote_focus(oid)
    _ledger(source, record, detail=",".join(record.get("aliases") or [])[:80])
    logger.info("desk_scene path=%s aliases=%s source=%s", path.name, ",".join(record["aliases"]), source)
    return record


def forget_file(path_raw: str | Path | None) -> None:
    if not path_raw:
        return
    try:
        gone = str(Path(str(path_raw)).expanduser().resolve())
    except OSError:
        gone = str(path_raw)
    scene = _load()
    kept: list[dict[str, Any]] = []
    removed_ids: set[str] = set()
    for obj in scene.get("objects") or []:
        try:
            obj_path = str(Path(str(obj.get("path") or "")).expanduser().resolve())
        except OSError:
            obj_path = str(obj.get("path") or "")
        if obj_path == gone or str(obj.get("path") or "") == str(path_raw):
            removed_ids.add(str(obj.get("id") or ""))
            continue
        kept.append(obj)
    scene["objects"] = kept
    slots = dict(scene.get("slots") or {})
    for key, value in list(slots.items()):
        if value in removed_ids:
            slots[key] = None
    scene["slots"] = slots
    _save(scene)


def resolve_alias(alias: str) -> Path | None:
    obj = resolve_alias_object(alias)
    if obj is None:
        return None
    return _live_path(obj)


def resolve_alias_object(alias: str) -> dict[str, Any] | None:
    token = normalize_alias(alias)
    if not token or token in GENERIC_ALIASES:
        return None
    hits: list[tuple[float, dict[str, Any]]] = []
    for obj in _objects():
        aliases = [normalize_alias(a) for a in (obj.get("aliases") or [])]
        if token in aliases or any(token == normalize_alias(a) or a.endswith(" " + token) for a in aliases):
            hits.append((float(obj.get("touched_at") or 0), obj))
        elif token in " ".join(aliases):
            hits.append((float(obj.get("touched_at") or 0) - 1, obj))
    if not hits:
        return None
    hits.sort(key=lambda row: row[0], reverse=True)
    return hits[0][1]


def resolve_spoken_file(text: str) -> tuple[Path, str] | None:
    obj = resolve_spoken_object(text)
    if obj is None:
        return None
    path = _live_path(obj)
    if path is None:
        return None
    aliases = [normalize_alias(a) for a in (obj.get("aliases") or [])]
    alias = aliases[0] if aliases else obj.get("name") or path.name
    return path, str(alias)


def resolve_spoken_object(text: str) -> dict[str, Any] | None:
    lowered = re.sub(r"\s+", " ", (text or "").lower())
    ranked: list[tuple[int, dict[str, Any]]] = []
    for obj in _objects():
        for alias in obj.get("aliases") or []:
            token = normalize_alias(alias)
            if not token or token in GENERIC_ALIASES:
                continue
            if re.search(rf"\b{re.escape(token)}\b", lowered):
                ranked.append((len(token), obj))
    if not ranked:
        return None
    ranked.sort(key=lambda row: row[0], reverse=True)
    return ranked[0][1]


def parse_bind_goal(text: str, last_path: str | None) -> dict[str, Any] | None:
    if not last_path:
        last_path = str(scene_file_path("focus", "that", "landed") or "") or None
    if not last_path:
        return None
    raw = (text or "").strip()
    if BELONGS_RE.search(raw) or (PACKET_PUT_RE.search(raw) and "packet" in raw.lower()):
        return None
    match = BIND_RE.search(raw)
    if not match:
        return None
    alias = normalize_alias(match.group(1))
    if not alias or alias in GENERIC_ALIASES or len(alias) < 2:
        return None
    if len(alias.split()) > 6:
        return None
    if "packet" in alias:
        return {
            "action": "packet_add",
            "path": last_path,
            "packet": alias,
            "alias": alias,
            "goal": raw,
        }
    return {
        "action": "bind",
        "path": last_path,
        "alias": alias,
        "query": alias,
        "goal": raw,
    }


def ensure_packet(alias: str) -> dict[str, Any]:
    token = normalize_alias(alias)
    if "packet" not in token:
        token = f"{token} packet".strip()
    existing = resolve_alias_object(token) or resolve_alias_object(alias)
    if existing is not None and existing.get("kind") == "packet":
        existing["touched_at"] = time.time()
        _save(_load())
        _promote_focus(str(existing["id"]))
        return existing
    scene = _load()
    record = {
        "id": _new_id(),
        "kind": "packet",
        "path": "",
        "aliases": _unique_aliases([token, alias, token.replace(" packet", "")]),
        "inode": None,
        "name": token,
        "members": [],
        "held_items": [],
        "source": "packet",
        "touched_at": time.time(),
    }
    scene.setdefault("objects", []).append(record)
    _save(scene)
    _promote_focus(str(record["id"]))
    _ledger("packet", record, detail=token)
    return record


def add_member(packet: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    mid = str(member.get("id") or "")
    members = [str(item) for item in (packet.get("members") or [])]
    if mid and mid not in members:
        members.append(mid)
    packet["members"] = members
    packet["touched_at"] = time.time()
    scene = _load()
    for obj in scene.get("objects") or []:
        if obj.get("id") == packet.get("id"):
            obj["members"] = members
            obj["touched_at"] = packet["touched_at"]
    _save(scene)
    _ledger("packet_add", packet, detail=str(member.get("name") or mid))
    return packet


def packet_inventory(packet: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for mid in packet.get("members") or []:
        obj = _by_id(str(mid))
        if obj is not None:
            out.append(obj)
    return out


def note_search_hits(paths: list[Path]) -> None:
    files = [path for path in paths if path.is_file()][:4]
    if not files:
        return
    records = [remember_file(path, source="search", query=path.stem) for path in files]
    records = [item for item in records if item is not None]
    if not records:
        return
    _promote_focus(str(records[0]["id"]))
    if len(records) > 1:
        _set_slot("other", str(records[1]["id"]))
        _set_slot("that", str(records[0]["id"]))


def bind_visible_text(*chunks: str) -> dict[str, Any] | None:
    blob = " ".join(str(chunk or "") for chunk in chunks)
    if not blob.strip():
        return None
    from app.ev.laptop_files import resolve_existing

    names = re.findall(
        r"\b[\w.\-]+\.(?:pdf|txt|md|png|jpg|jpeg|html)\b",
        blob,
        flags=re.I,
    )
    hits: list[Path] = []
    for name in names:
        found, _matches, error = resolve_existing("", name)
        if error is None and found is not None and found.is_file():
            hits.append(found)
    spoken = resolve_spoken_object(blob)
    if spoken is not None:
        path = _live_path(spoken)
        if path is not None:
            hits.insert(0, path)
    # unique preserve order
    seen: set[str] = set()
    unique: list[Path] = []
    for path in hits:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    if not unique:
        return None
    note_search_hits(unique)
    return slot_object("that")


def looks_like_scene_turn(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if scene_is_live() and (
        FRAGMENT_RE.search(raw)
        or OTHER_RE.search(raw)
        or ALSO_RE.search(raw)
        or ADD_SLOT_RE.search(raw)
        or OPEN_SLOT_RE.search(raw)
        or (YES_RE.search(raw) and (_load().get("pending_offer")))
    ):
        return True
    if resolve_spoken_object(raw) and FRAGMENT_RE.search(raw):
        return True
    if BELONGS_RE.search(raw) or PACKET_PUT_RE.search(raw):
        return True
    return False


def _packet_name_from(text: str) -> str:
    match = BELONGS_RE.search(text) or PACKET_PUT_RE.search(text)
    if not match:
        if "visa" in (text or "").lower():
            return "visa packet"
        return ""
    return normalize_alias(match.group(1))


def parse_scene_goal(text: str, last_path: str | None = None) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    clauses = [part.strip() for part in re.split(r"[.;]+", raw) if part.strip()]
    if len(clauses) >= 2:
        turns = []
        for clause in clauses:
            one = _parse_one_scene(clause, last_path)
            if one is not None:
                turns.append(one)
        if len(turns) >= 2:
            return {"action": "scene_turns", "turns": turns, "goal": raw}
        if len(turns) == 1:
            return turns[0]
    return _parse_one_scene(raw, last_path)


def _parse_one_scene(raw: str, last_path: str | None) -> dict[str, Any] | None:
    lowered = raw.lower().strip()
    offer = _load().get("pending_offer")
    if offer and YES_RE.search(lowered):
        return {"action": "confirm_land", "goal": raw}
    if OTHER_RE.search(lowered) and not ALSO_RE.search(lowered) and "put" not in lowered:
        return {"action": "scene_other", "goal": raw}
    also = ALSO_RE.search(raw)
    if also:
        body = also.group(1).strip(" .,'\"")
        body = re.sub(r"^(?:add|append)\s+", "", body, flags=re.I).strip()
        note = last_note_object()
        if note is None:
            return None
        path = _live_path(note)
        if path is None:
            return None
        return {
            "action": "append",
            "path": str(path),
            "query": path.name,
            "content": body,
            "goal": raw,
        }
    add_to = ADD_TO_NAMED_RE.search(raw)
    if add_to:
        dest = normalize_alias(add_to.group(2))
        dest_obj = resolve_alias_object(dest)
        if dest in DEIXIS_DEST and (dest_obj is None or dest_obj.get("kind") != "packet"):
            path = referent_file_path(last_path)
            if path is None:
                return None
            body = re.sub(r"[.!?]+$", "", add_to.group(1).strip(" \"'")).strip()
            if not body:
                return None
            return {
                "action": "append",
                "path": str(path),
                "query": path.name,
                "content": body,
                "goal": raw,
            }
        if "packet" in dest or (dest_obj is not None and dest_obj.get("kind") == "packet"):
            source = last_path or scene_file_path("that", "landed", "focus", "other")
            if source is None:
                return None
            return {
                "action": "packet_add",
                "path": str(source),
                "packet": dest,
                "goal": raw,
            }
        return None
    packet_name = _packet_name_from(raw)
    if packet_name and (BELONGS_RE.search(raw) or PACKET_PUT_RE.search(raw)):
        source = last_path or scene_file_path("that", "landed", "focus", "other")
        if source is None:
            return None
        return {
            "action": "packet_add",
            "path": str(source),
            "packet": packet_name,
            "goal": raw,
        }
    if ADD_SLOT_RE.search(raw) and not ADD_TO_NAMED_RE.search(raw):
        if offer:
            return {"action": "confirm_land", "goal": raw}
        packet = active_packet()
        source = scene_file_path("that", "landed", "focus", "other")
        if packet is not None and source is not None:
            aliases = [normalize_alias(a) for a in (packet.get("aliases") or [])]
            return {
                "action": "packet_add",
                "path": str(source),
                "packet": aliases[0] if aliases else "packet",
                "goal": raw,
            }
        path = scene_file_path("that", "focus", "landed")
        if path is not None:
            return {"action": "open", "path": str(path), "query": path.name, "goal": raw}
        return None
    if OPEN_SLOT_RE.search(raw) or re.search(r"\bthe new one\b", lowered):
        slot = "landed" if "new" in lowered else "that"
        path = slot_path(slot) or slot_path("focus")
        if path is None:
            return None
        return {"action": "open", "path": str(path), "query": path.name, "goal": raw}
    spoken = resolve_spoken_object(raw)
    if spoken is not None and spoken.get("kind") == "packet" and re.search(
        r"\b(?:what's in|what is in|what's on|what is on|show)\b",
        lowered,
    ):
        return {"action": "packet_list", "packet": (spoken.get("aliases") or ["packet"])[0], "goal": raw}
    return None


async def run_scene_action(args: dict[str, Any]) -> dict[str, Any] | None:
    action = str(args.get("action") or "").strip().lower()
    if action == "scene_turns":
        spoken_parts: list[str] = []
        last: dict[str, Any] = {
            "ok": True,
            "executed": True,
            "verified": True,
            "action": "scene_turns",
            "source": "desk_scene",
        }
        for turn in args.get("turns") or []:
            piece = await run_scene_action(dict(turn))
            if piece is None:
                from app.ev.laptop_files import run_file_goal

                piece = await run_file_goal(dict(turn))
            last = piece
            if piece.get("spoken"):
                spoken_parts.append(str(piece["spoken"]))
            if not piece.get("ok"):
                piece["spoken"] = " ".join(spoken_parts).strip()
                return piece
        last["spoken"] = " ".join(spoken_parts).strip()
        last["action"] = "scene_turns"
        return last
    if action == "scene_other":
        other = slot_object("other")
        that = slot_object("that")
        if other is not None and other.get("kind") == "packet":
            files = [
                obj
                for obj in _objects()
                if obj.get("kind") != "packet"
                and obj.get("id") != (that or {}).get("id")
            ]
            files.sort(key=lambda item: float(item.get("touched_at") or 0), reverse=True)
            other = files[0] if files else None
        if other is None:
            return {
                "ok": False,
                "executed": False,
                "verified": False,
                "error": "not_found",
                "spoken": "I don't have another file in the scene.",
                "source": "desk_scene",
            }
        if that is not None:
            _set_slot("other", str(that["id"]))
        _promote_focus(str(other["id"]))
        _set_slot("that", str(other["id"]))
        name = other.get("name") or "the other file"
        return {
            "ok": True,
            "executed": True,
            "verified": True,
            "action": "scene_other",
            "path": other.get("path"),
            "spoken": f"Using {name}.",
            "source": "desk_scene",
        }
    if action == "packet_add":
        path = args.get("path")
        member = remember_file(path, source="packet_add", query=Path(str(path or "")).stem)
        if member is None:
            return {
                "ok": False,
                "executed": False,
                "verified": False,
                "error": "not_found",
                "spoken": "I need a file to put in that packet.",
                "source": "desk_scene",
            }
        packet = ensure_packet(str(args.get("packet") or "packet"))
        add_member(packet, member)
        alias = (packet.get("aliases") or ["packet"])[0]
        return {
            "ok": True,
            "executed": True,
            "verified": True,
            "action": "packet_add",
            "path": member.get("path"),
            "packet": alias,
            "spoken": f"Added {member.get('name')} to the {alias}.",
            "source": "desk_scene",
        }
    if action == "packet_list":
        packet = resolve_alias_object(str(args.get("packet") or "")) or active_packet()
        if packet is None or packet.get("kind") != "packet":
            return {
                "ok": False,
                "executed": False,
                "verified": False,
                "error": "not_found",
                "spoken": "I don't have that packet yet.",
                "source": "desk_scene",
            }
        members = packet_inventory(packet)
        names = [str(item.get("name") or "file") for item in members] or ["nothing yet"]
        alias = (packet.get("aliases") or ["packet"])[0]
        return {
            "ok": True,
            "executed": True,
            "verified": True,
            "action": "packet_list",
            "files": names,
            "spoken": f"{alias}: {', '.join(names)}.",
            "source": "desk_scene",
        }
    if action == "confirm_land":
        return confirm_land()
    return None


def _fits_land(path: Path, *, has_packet: bool) -> bool:
    if path.name.startswith("."):
        return False
    suffix = path.suffix.lower()
    if suffix not in {".pdf", ".png", ".jpg", ".jpeg", ".txt"}:
        return False
    if LAND_NAME.search(path.name):
        return True
    return bool(has_packet and suffix == ".pdf")


def scan_landed_files() -> dict[str, Any] | None:
    from app.ev.laptop_files import allowed_roots, laptop_files_allowed, path_denied

    if not laptop_files_allowed():
        return None
    if not _SEEN_MTIMES:
        seed_watch_snapshot()
        return None
    try:
        from app.ev.ev_sense import quiet_hours_active

        quiet = bool(quiet_hours_active())
    except Exception:
        quiet = False
    roots = allowed_roots()
    if len(roots) > 1:
        roots = [root for root in roots if root.name.lower() in {"desktop", "downloads"}]
    has_packet = active_packet() is not None
    newest: Path | None = None
    newest_mtime = 0.0
    for root in roots:
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_file() or path_denied(child) is not None:
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            key = str(child.resolve())
            prev = _SEEN_MTIMES.get(key)
            _SEEN_MTIMES[key] = mtime
            if prev is not None and mtime <= prev + 0.05:
                continue
            if not _fits_land(child, has_packet=has_packet):
                continue
            if mtime >= newest_mtime:
                newest = child
                newest_mtime = mtime
    if newest is None:
        return None
    record = remember_file(newest, source="landed", query=newest.stem)
    if record is None:
        return None
    _set_slot("landed", str(record["id"]))
    _set_slot("that", str(record["id"]))
    packet = active_packet()
    spoken = f"A new file landed: {newest.name}."
    if packet is not None:
        alias = (packet.get("aliases") or ["packet"])[0]
        spoken = f"{newest.name} just landed. Add it to the {alias}?"
        scene = _load()
        scene["pending_offer"] = {
            "landed_id": record["id"],
            "packet_id": packet.get("id"),
            "path": str(newest.resolve()),
            "spoken": spoken,
        }
        _save(scene)
    if quiet:
        return {"ok": True, "spoken": "", "quiet": True, "path": str(newest), "offered": True}
    return {
        "ok": True,
        "executed": True,
        "verified": True,
        "action": "landed",
        "path": str(newest.resolve()),
        "spoken": spoken,
        "source": "desk_scene",
        "offered": bool(packet is not None),
    }


def seed_watch_snapshot() -> None:
    from app.ev.laptop_files import allowed_roots, laptop_files_allowed, path_denied

    if not laptop_files_allowed():
        return
    roots = allowed_roots()
    if len(roots) > 1:
        roots = [root for root in roots if root.name.lower() in {"desktop", "downloads"}]
    for root in roots:
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_file() or path_denied(child) is not None:
                continue
            try:
                _SEEN_MTIMES[str(child.resolve())] = child.stat().st_mtime
            except OSError:
                continue


def confirm_land() -> dict[str, Any]:
    scene = _load()
    offer = scene.get("pending_offer") or {}
    landed = _by_id(str(offer.get("landed_id") or "")) or slot_object("landed")
    packet = _by_id(str(offer.get("packet_id") or "")) or active_packet()
    scene["pending_offer"] = None
    _save(scene)
    if landed is None or packet is None:
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "not_found",
            "spoken": "There's nothing waiting to file.",
            "source": "desk_scene",
        }
    add_member(packet, landed)
    alias = (packet.get("aliases") or ["packet"])[0]
    return {
        "ok": True,
        "executed": True,
        "verified": True,
        "action": "confirm_land",
        "path": landed.get("path"),
        "spoken": f"Added {landed.get('name')} to the {alias}.",
        "source": "desk_scene",
    }


async def steward_watch_loop() -> None:
    seed_watch_snapshot()
    while True:
        await _sleep_watch()
        try:
            offer = scan_landed_files()
        except Exception:
            logger.info("desk_steward scan failed", exc_info=True)
            continue
        spoken = str((offer or {}).get("spoken") or "").strip()
        if not spoken:
            continue
        await _speak_steward(spoken)


async def _sleep_watch() -> None:
    import asyncio

    await asyncio.sleep(2.5)


async def _speak_steward(spoken: str) -> None:
    try:
        from app.ev.ev_sense import quiet_hours_active

        if quiet_hours_active():
            return
    except Exception:
        pass
    try:
        from app.voice.live.layer import speak_on_live

        scene = _load()
        session_id = str(scene.get("session_id") or "") or None
        await speak_on_live(spoken, session_id=session_id)
    except Exception:
        logger.info("desk_steward speak skipped")
