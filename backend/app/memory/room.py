"""Room memory: where the owner left something Evie already saw.

Look already stores a camera.observation. This module turns that into a
speakable “last seen” for keys, chargers, wallets — not people, not AirTags,
and not the last coding file.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from app.utils.text import simple_tokens, utcnow

# Verbs that are the question, not the object.
LOCATE_SCAFFOLD = frozenset(
    {
        "did",
        "drop",
        "dropped",
        "have",
        "leave",
        "left",
        "park",
        "parked",
        "place",
        "placed",
        "put",
        "set",
        "spot",
        "see",
        "seen",
    }
)

_KINSHIP = (
    r"mom|dad|mother|father|brother|sister|wife|husband|partner|"
    r"girlfriend|boyfriend|friend|boss|roommate|neighbor|son|daughter|"
    r"mummy|mommy|papa|baba"
)

_GEAR_RE = re.compile(
    r"\b(?:air\s*tags?|backpack tag|find my|tile(?: tracker)?)\b",
    re.IGNORECASE,
)
_CODE_FILE_RE = re.compile(
    r"\bwhere.{0,32}\b(?:file|script|folder|repo|workspace)\b",
    re.IGNORECASE,
)
_PERSON_NAME_RE = re.compile(r"\bwhere(?:'s| is) [A-Z][a-z]{2,}\b")
_OBJECT_LOCATE_RE = re.compile(
    r"\b(?:"
    r"where (?:did|have) i (?:leave|left|put|drop(?:ped)?|park(?:ed)?|set|place[d]?)|"
    r"where(?:'s| is) my (?!" + _KINSHIP + r"\b)|"
    r"where did (?:you|i) last (?:see|spot) (?:my |the )|"
    r"have you seen my (?!" + _KINSHIP + r"\b)|"
    r"do you know where (?:my |the |i (?:left|put) )|"
    r"last place i (?:saw|left|put) "
    r")",
    re.IGNORECASE,
)
_NOUN_RE = re.compile(
    r"\b(?:my|the|a|an)\s+(?P<noun>[a-z][a-z0-9'-]{1,24}(?:\s+[a-z][a-z0-9'-]{1,24}){0,2})",
    re.IGNORECASE,
)
_SURFACE_RE = re.compile(
    r"\b(?:on|in|under|beside|next to|by)\s+"
    r"(?:the |your |my |an? )?"
    r"(?P<surface>"
    r"coffee table|kitchen table|side table|kitchen counter|"
    r"nightstand|charger pad|living room|entryway|"
    r"desk|table|counter|couch|sofa|bed|floor|shelf|chair|"
    r"kitchen|charger pad|hook|bag|backpack|"
    r"drawer|dresser|"
    r"bedroom|office|hallway|bathroom"
    r")\b",
    re.IGNORECASE,
)
_JUNK = frozenset(
    {
        "adult",
        "background",
        "camera",
        "equipment",
        "finger",
        "hand",
        "human",
        "image",
        "people",
        "person",
        "photo",
        "picture",
        "scene",
        "someone",
        "something",
        "thing",
        "this",
        "that",
        "view",
    }
)

_OBJECT_ALIASES: dict[str, frozenset[str]] = {
    "keys": frozenset({"key", "keys", "keychain"}),
    "key": frozenset({"key", "keys", "keychain"}),
    "charger": frozenset({"charger", "cable", "cord", "brick"}),
    "cable": frozenset({"charger", "cable", "cord"}),
    "wallet": frozenset({"wallet", "billfold"}),
    "airpods": frozenset({"airpods", "earbuds", "earphones", "headphones"}),
    "earbuds": frozenset({"airpods", "earbuds", "earphones", "headphones"}),
    "glasses": frozenset({"glasses", "spectacles"}),
    "phone": frozenset({"phone", "iphone"}),
    "iphone": frozenset({"phone", "iphone"}),
    "laptop": frozenset({"laptop", "macbook", "notebook"}),
    "bottle": frozenset({"bottle", "waterbottle"}),
    "umbrella": frozenset({"umbrella"}),
    "remote": frozenset({"remote", "clicker"}),
}


def looks_like_object_locate(message: str | None) -> bool:
    """True for “where did I leave my charger”, not Rahul, mom, or hello.py."""

    raw = (message or "").strip()
    if not raw:
        return False
    if _CODE_FILE_RE.search(raw) or _GEAR_RE.search(raw):
        return False
    if _PERSON_NAME_RE.search(raw) and not re.search(r"\bmy \w+", raw, re.IGNORECASE):
        return False
    if re.search(r"\b(?:my|the)\s+(?:" + _KINSHIP + r")\b", raw, re.IGNORECASE):
        if not re.search(r"\b(?:leave|left|put|drop)\b", raw, re.IGNORECASE):
            return False
    return bool(_OBJECT_LOCATE_RE.search(raw))


def object_locate_noun(message: str | None) -> str:
    """The thing they are hunting, stripped of question words."""

    text = (message or "").strip()
    if not text:
        return ""
    match = _NOUN_RE.search(text)
    if match:
        words = [
            token
            for token in simple_tokens(match.group("noun"))
            if token not in LOCATE_SCAFFOLD and token not in _JUNK and len(token) >= 2
        ]
        if words:
            return " ".join(words[:4])
    leftover = [
        token
        for token in simple_tokens(text)
        if token not in LOCATE_SCAFFOLD
        and token not in _JUNK
        and len(token) >= 3
        and token
        not in {
            "where",
            "did",
            "have",
            "you",
            "know",
            "last",
            "place",
            "time",
        }
    ]
    return leftover[0] if leftover else ""


def extract_placement(
    *,
    scene: str | None = None,
    labels: list[str] | None = None,
    named: str | None = None,
) -> dict[str, Any]:
    """Pull object names and a surface from a look, without a fixed catalog."""

    objects: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        words = [
            token
            for token in simple_tokens(raw)
            if token not in _JUNK and token not in LOCATE_SCAFFOLD and len(token) >= 2
        ]
        if not words:
            return
        name = " ".join(words[:4])
        key = name.lower()
        if key in seen or key in _JUNK:
            return
        seen.add(key)
        objects.append(name)

    named_s = " ".join(str(named or "").split()).strip()
    if named_s and named_s.lower() not in {"this", "that", "it", "you"}:
        _add(named_s)
    for label in labels or []:
        _add(str(label or ""))
    blob = " ".join(str(scene or "").split())
    for match in _NOUN_RE.finditer(blob):
        _add(match.group("noun"))
    surface_match = _SURFACE_RE.search(blob)
    surface = ""
    if surface_match:
        surface = " ".join(str(surface_match.group("surface") or "").split()).lower()
    phrase = _placement_phrase(objects[0] if objects else "", surface)
    return {
        "objects": objects[:8],
        "surface": surface or None,
        "phrase": phrase or None,
    }


def _placement_phrase(obj: str, surface: str) -> str:
    name = (obj or "").strip()
    if not name:
        return ""
    place = (surface or "").strip()
    if not place:
        return name
    if place in {
        "bag",
        "backpack",
        "drawer",
        "kitchen",
        "bedroom",
        "office",
        "hallway",
        "bathroom",
        "living room",
    }:
        return f"{name} in the {place}"
    return f"{name} on the {place}"


def placement_fact_text(placement: dict[str, Any]) -> str:
    phrase = str(placement.get("phrase") or "").strip()
    if not phrase:
        return ""
    return f"Last seen: your {phrase}."


def spoken_object_locate(query: str, hit: dict[str, Any] | None) -> str:
    """One useful sentence: object, place, recency — or an honest miss."""

    noun = object_locate_noun(query) or str((hit or {}).get("object") or "").strip() or "that"
    if not hit:
        return (
            f"I haven't seen your {noun} on camera yet. "
            "Point it out next time and I'll remember where it is."
        )
    surface = str(hit.get("surface") or "").strip()
    placement = str(hit.get("placement") or "").strip()
    scene = " ".join(str(hit.get("text") or hit.get("recall") or "").split()).strip()
    scene = re.sub(r"^last seen:\s*", "", scene, flags=re.IGNORECASE).strip(" .")
    when = _relative_when(hit.get("when") or hit.get("occurred_at"))
    if placement and not placement.lower().startswith("last seen"):
        where = placement
        if where.lower().startswith(noun.lower()):
            rest = where[len(noun) :].strip(" -,")
            where = rest if rest else where
    elif surface:
        where = f"{noun} on the {surface}"
    elif scene:
        where = scene.rstrip(".")
    else:
        where = noun
    if when:
        return f"Last time I saw your {noun}, it was {where}, {when}."[:400]
    return f"Last time I saw your {noun}, it was {where}."[:400]


def object_aliases() -> dict[str, frozenset[str]]:
    return _OBJECT_ALIASES


def _relative_when(value: Any) -> str:
    moment = _as_datetime(value)
    if moment is None:
        return ""
    now = utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=now.tzinfo)
    delta = now - moment
    if delta < timedelta(0):
        delta = timedelta(0)
    seconds = int(delta.total_seconds())
    if seconds < 120:
        return "just now"
    if seconds < 5400:
        minutes = max(2, seconds // 60)
        return f"about {minutes} minutes ago"
    if seconds < 36 * 3600:
        hours = max(1, seconds // 3600)
        return f"about {hours} hour{'s' if hours != 1 else ''} ago"
    days = max(1, seconds // 86400)
    if days < 14:
        return f"{days} day{'s' if days != 1 else ''} ago"
    return moment.strftime("%b %-d")


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
