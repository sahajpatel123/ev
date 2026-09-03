"""Persist Evie's camera observations as recallable memory.

Look/capture/record used to die in tool JSON, and the live model's spoken
description never made it into search. This writes searchable observations
(people, clothing, objects, colors, path) so later questions hit
camera.observation + Memory — not Apple Photos, and not question scaffolding.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import EntityRef, MemoryCandidate
from app.models import Event
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.utils.text import simple_tokens, utcnow

logger = logging.getLogger("ev.memory.visual")

VISUAL_EVENT_TYPE = "camera.observation"
SPOKEN_SCENE_WINDOW = timedelta(minutes=3)

# Past / named visual memory — not a live look, not Apple Photos.
VISUAL_RECALL_RE = re.compile(
    r"("
    r"what did you see|"
    r"what you (?:just )?saw|"
    r"what were you looking at|"
    r"what was i wearing|"
    r"was i wearing|"
    r"what (?:t-?shirt|shirt|top|hoodie|jacket|hat|outfit) was i|"
    r"which (?:t-?shirt|shirt|top|hoodie) was i|"
    r"when was the last time you (?:saw|looked)|"
    r"when did you (?:last )?see|"
    r"last time you (?:saw|looked)|"
    r"have you (?:ever |already )?seen (?:me|this|that)|"
    r"did you (?:ever |already )?see me|"
    r"saw me with|"
    r"you (?:already |just )?(?:saw|looked at) (?:me |this |that )|"
    r"that (?:photo|picture|pic|clip|video|recording|selfie)|"
    r"(?:photo|picture|clip|video) (?:you |we |i )?(?:took|recorded|captured|saved)|"
    r"clip of me|"
    r"photo of me|"
    r"you (?:took|recorded|captured) (?:a |that )?(?:photo|picture|clip|video)|"
    r"the \w+ you (?:saw|looked at|captured|recorded|took)|"
    r"remember (?:that|the) (?:photo|clip|picture|video)|"
    r"\bthe (?:white|black|red|blue|green|yellow|grey|gray|pink|orange|silver) \w+\b|"
    r"what did i (?:just )?ask you to (?:remember|memorise|memorize|keep)|"
    r"what i (?:just )?asked you to (?:remember|memorise|memorize|keep)|"
    r"(?:just )?ask(?:ed)? you to remember|"
    r"what were you supposed to remember|"
    r"what did i (?:just )?(?:show|tell) you to remember|"
    r"what was i showing|"
    r"what did i (?:just )?show you|"
    r"the \w+ i (?:just )?(?:showed|held|was holding)|"
    r"what (?:was|is) (?:the |that )?\w+ i (?:showed|held|was holding)|"
    r"what was i holding|"
    r"what (?:was|is) (?:the |that )?(?:book|object|thing|cover|title)(?: called| titled| named)?|"
    r"what did (?:the |that )?(?:book|object|thing) (?:say|read|was)|"
    r"do you remember what i (?:just )?(?:showed|held|was holding)|"
    r"which \w+ was i (?:holding|showing|wearing|using)|"
    r"what \w+ was i (?:holding|showing|wearing|using)|"
    r"\bdid you (?:already |ever )?(?:memorise|memorize|remember)\b|"
    r"\bhave you (?:already |ever )?(?:memorised|memorized|remembered)\b|"
    r"\bdid you remember\b"
    r")",
    re.IGNORECASE,
)

PAST_VISUAL_RE = re.compile(
    r"("
    r"\blast time you (?:saw|looked)|"
    r"\bwhen was the last time you (?:saw|looked)|"
    r"\bwhen (?:was|did) you (?:last )?see\b|"
    r"\bused to wear\b|"
    r"\bwas i wearing\b|"
    r"\bwere you (?:wearing|holding|looking)\b|"
    r"\bwhat was i wearing\b|"
    r"\bdid you (?:ever |already |last )?see me\b|"
    r"\bhave you (?:ever |already )?seen (?:me|this|that)\b|"
    r"\bsaw me with\b|"
    r"\byou (?:already |just )?(?:saw|looked at) (?:me|this|that)\b|"
    r"\bthe \w+ you (?:saw|looked at|captured|recorded|took)\b|"
    r"\b(?:t-?shirt|shirt|top|hoodie|jacket|hat|outfit|wearing).{0,40}(?:earlier|before|previously)\b|"
    r"\b(?:earlier|before|previously).{0,40}(?:t-?shirt|shirt|top|hoodie|wearing|wore)\b|"
    r"\bwhat was i showing\b|"
    r"\bwhat was i holding\b|"
    r"\bwhich \w+ was i (?:holding|showing|wearing|using)\b|"
    r"\bwhat \w+ was i (?:holding|showing|wearing|using)\b"
    r")",
    re.IGNORECASE,
)

CURRENT_VISUAL_RE = re.compile(
    r"("
    r"\bwhat am i wearing\b|"
    r"\bam i wearing\b|"
    r"\bwhat(?:'s| is) my (?:t-?shirt|shirt|top|hoodie|jacket|hat|outfit|color)\b|"
    r"\bwhat color is my\b|"
    r"\bwhich (?:t-?shirt|shirt|top|hoodie|jacket) am i\b|"
    r"\bwhat (?:t-?shirt|shirt|top|hoodie) am i\b|"
    r"\bwhat am i holding\b|"
    r"\bwhat do you see\b|"
    r"\bwhat(?:'s| is) (?:this|that)\b|"
    r"\bwhat color is this\b|"
    r"\blook at (?:this|that|me)\b|"
    r"\bcan you see (?:this|that|me)\b"
    r")",
    re.IGNORECASE,
)

# Owner asked her to keep what is in view — not "do you remember", not a typed fact.
KEEP_VISIBLE_RE = re.compile(
    r"("
    r"\b(?:memorise|memorize)\b|"
    r"\bremember this\b|"
    r"\bremember that (?!i\b|i['’]m\b|we\b)|"
    r"\bremember (?:the item|the thing|what i(?:'m| am) showing|what i(?:'m| am) holding)\b|"
    r"\bremember my (?!password|name|birthday|appointment|meeting)\b|"
    r"\bkeep this in (?:mind|memory)\b|"
    r"\b(?:don't|do not) forget (?:this|that|it)\b|"
    r"\bi(?:'m| am) showing you\b|"
    r"\bremember\b.{0,80}\bshowing you\b|"
    r"\bshowing you\b.{0,40}\bremember\b|"
    r"\bthis is my\b.{0,48}\b(?:remember|memorise|memorize|keep)\b|"
    r"\b(?:remember|memorise|memorize|keep)\b.{0,80}\bthis is my\b|"
    r"\bopen (?:the )?camera\b.{0,48}\b(?:remember|memorise|memorize|look|see|showing)\b"
    r")",
    re.IGNORECASE,
)

KEEP_RECALL_RE = re.compile(
    r"("
    r"\bdid you (?:already |ever )?(?:memorise|memorize|remember)\b|"
    r"\bhave you (?:already |ever )?(?:memorised|memorized|remembered)\b|"
    r"\bdid you (?:already )?keep (?:the|this|that|it|what)\b|"
    r"\bdo you remember (?:the|this|that) (?!i\b|i['’]m\b|we\b)|"
    r"\bdo you remember what i (?:asked|showed|told you to remember|held|was holding)\b|"
    r"\bdid you remember\b|"
    r"\bwhat did i (?:just )?ask you to (?:remember|memorise|memorize|keep)\b|"
    r"\bwhat i (?:just )?asked you to remember\b|"
    r"\b(?:just )?ask(?:ed)? you to remember\b|"
    r"\b(?:were you able to|did you get to) (?:memorise|memorize|remember)\b"
    r")",
    re.IGNORECASE,
)

_KEEP_OBJECT_RE = re.compile(
    r"\b(?:memorise|memorize|remember|keep|(?:don't|do not) forget)\s+"
    r"(?:(?:this|that|the|a|an|it)\s+)?"
    r"(?P<object>(?:[a-z0-9']+\s*){0,6})",
    re.IGNORECASE,
)

_BOILERPLATE_PREFIXES = (
    "a current camera image is attached",
    "bounded camera images are attached",
    "a photo you just took is attached",
    "frames from the video you just recorded",
)

_GROUNDING_RE = re.compile(
    r"grounding:\s*(.+?)(?:\.\s*image\b|\.\s*after the description|\.|$)",
    re.IGNORECASE | re.DOTALL,
)

_EMPTY_SCENE_RE = re.compile(
    r"("
    r"nothing was detected|"
    r"i (?:do not|don't|didn'?t|could not|couldn't) see(?: any| that| this| it| the)?|"
    r"no (?:text|objects|people).{0,24}detected|"
    r"i (?:can't|cannot) see (?:anything|the (?:image|object|thing)|(?:this|that|it) clearly)"
    r")",
    re.IGNORECASE,
)

_CLARITY_HEDGE_RE = re.compile(
    r"("
    r"\b(?:can't|cannot|couldn't|could not) see.{0,28}(?:clearly|well)\b|"
    r"\bnot clearly visible\b|"
    r"\b(?:too |a bit |quite )?blurry\b|"
    r"\bhard to (?:see|make out)\b|"
    r"\b(?:isn't|is not|not) (?:clear|sharp) enough\b|"
    r"\bcan't make (?:it|that|the \w+) out\b|"
    r"\bcan't see the (?:phone|object|item) clearly\b"
    r")",
    re.IGNORECASE,
)


def is_empty_visual_scene(text: str | None) -> bool:
    """True when a look stored a blank frame as if it were the memorized object."""

    blob = " ".join(str(text or "").split()).strip().lower()
    if not blob:
        return True
    return bool(_EMPTY_SCENE_RE.search(blob))


def is_clarity_hedge(text: str | None) -> bool:
    """True when speech denies a delivered camera frame instead of naming it."""

    blob = " ".join(str(text or "").split()).strip()
    if not blob:
        return False
    return bool(_CLARITY_HEDGE_RE.search(blob))


def is_camera_prompt_echo(text: str | None) -> bool:
    """True when the look injection leaked back as an owner utterance."""

    blob = " ".join(str(text or "").split()).strip().lower()
    if not blob:
        return False
    return blob.startswith("this is a current photo from the owner") or blob.startswith(
        "a current camera image is attached"
    )


_HEDGE_OPENING = re.compile(
    r"^\s*(?:"
    r"i (?:do not|don't|cannot|can't) (?:have|find|tell)|"
    r"i have no|"
    r"there is no (?:reliable )?record|"
    r"nothing in (?:the |my )?record|"
    r"let me see what record|"
    r"i(?:'ll| will) check|"
    r"i did not see anything|"
    r"i can't see a camera|"
    r"i can't access the camera|"
    r"i (?:can't|cannot) memor|"
    r"i cannot guarantee|"
    r"i can see what you have|"
    r"for future reference|"
    r"once i glanced|"
    r"unless (?:it'?s|it is) stored"
    r")",
    re.IGNORECASE,
)

_VISUAL_SCAFFOLD = frozenset(
    {
        "a",
        "about",
        "again",
        "already",
        "am",
        "an",
        "and",
        "anything",
        "are",
        "at",
        "before",
        "can",
        "cannot",
        "could",
        "did",
        "do",
        "does",
        "earlier",
        "ever",
        "for",
        "future",
        "guarantee",
        "had",
        "have",
        "here",
        "how",
        "i",
        "in",
        "is",
        "it",
        "just",
        "know",
        "last",
        "let",
        "look",
        "looked",
        "looking",
        "me",
        "memorise",
        "memorised",
        "memorize",
        "memorized",
        "memory",
        "my",
        "of",
        "on",
        "once",
        "or",
        "previously",
        "record",
        "records",
        "reference",
        "reliable",
        "remember",
        "remembered",
        "saw",
        "see",
        "seeing",
        "seen",
        "tell",
        "that",
        "the",
        "there",
        "these",
        "this",
        "those",
        "time",
        "times",
        "to",
        "was",
        "wearing",
        "were",
        "whether",
        "what",
        "when",
        "where",
        "which",
        "who",
        "with",
        "you",
        "your",
    }
)

_CLOTHING = frozenset(
    {
        "cap",
        "clothes",
        "clothing",
        "coat",
        "dress",
        "hat",
        "hoodie",
        "jacket",
        "jeans",
        "outfit",
        "pants",
        "shirt",
        "shoes",
        "shorts",
        "sweater",
        "tee",
        "top",
        "tshirt",
        "wearing",
        "wore",
    }
)

_COLORS = frozenset(
    {
        "beige",
        "black",
        "blue",
        "brown",
        "gold",
        "gray",
        "green",
        "grey",
        "khaki",
        "navy",
        "orange",
        "pink",
        "purple",
        "red",
        "silver",
        "white",
        "yellow",
    }
)

_ALIASES: dict[str, frozenset[str]] = {
    "tshirt": frozenset({"shirt", "tee", "tshirt"}),
    "tee": frozenset({"shirt", "tshirt", "tee"}),
    "shirt": frozenset({"shirt", "tshirt", "tee"}),
    "grey": frozenset({"grey", "gray"}),
    "gray": frozenset({"grey", "gray"}),
    "iphone": frozenset({"iphone", "phone"}),
    "phone": frozenset({"iphone", "phone"}),
}

_SCENE_CUES = (
    "wearing",
    "holding",
    "held",
    "shirt",
    "hoodie",
    "jacket",
    "person",
    "people",
    "i see",
    "i'm looking",
    "you are wearing",
    "you're wearing",
    "in front",
    "on the",
    "t-shirt",
    "tshirt",
    "book",
    "paperback",
    "cover",
    "device",
    "buttons",
    "in your hand",
    "you're holding",
)


def wants_past_visual(message: str | None) -> bool:
    """True when the owner is asking about a prior look, not the live frame."""

    return bool(PAST_VISUAL_RE.search((message or "").strip()))


def is_keep_recall_query(message: str | None) -> bool:
    """True when they ask whether a prior keep-from-sight request was stored."""

    return bool(KEEP_RECALL_RE.search((message or "").strip()))


def wants_keep_visible(message: str | None) -> bool:
    """True when they want her to store what they are showing, not chat history."""

    text = (message or "").strip()
    if not text:
        return False
    if is_keep_recall_query(text):
        return False
    if re.search(r"\bdo you remember\b", text, re.IGNORECASE):
        return False
    return bool(KEEP_VISIBLE_RE.search(text))


_KEEP_TOPIC_STOP = _VISUAL_SCAFFOLD | {
    "asked",
    "evie",
    "item",
    "owner",
    "primary",
    "showed",
    "shown",
    "showing",
    "they",
    "thing",
}


def keep_topic(message: str | None) -> str:
    """Object words from the keep request, not a fixed list of things."""

    text = (message or "").strip()
    shown = _object_from_utterance(text)
    if shown and shown.split()[0] not in _JUNK_OBJECT and shown not in {
        "this",
        "that",
        "it",
        "you",
    }:
        return shown
    best = ""
    for match in _KEEP_OBJECT_RE.finditer(text):
        raw = (match.group("object") if match else "") or ""
        raw = re.split(
            r"\b(?:so that|so i|for later|please|and then|in mind|in memory)\b",
            raw,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        words = [
            token
            for token in re.findall(r"[a-z0-9']+", raw.lower())
            if token not in _KEEP_TOPIC_STOP and len(token) >= 2
        ]
        if words:
            best = " ".join(words[:6])
    if best:
        return best
    return shown or "this"


_JUNK_OBJECT = frozenset(
    {
        "adult",
        "background",
        "camera",
        "equipment",
        "finger",
        "hand",
        "hands",
        "human",
        "image",
        "indoor",
        "object",
        "optical",
        "outdoor",
        "people",
        "person",
        "photo",
        "room",
        "setting",
        "sign",
        "something",
        "structure",
        "stuff",
        "thing",
    }
)

_OBJECT_STOP = _VISUAL_SCAFFOLD | _JUNK_OBJECT | {
    "also",
    "area",
    "clearly",
    "color",
    "colors",
    "display",
    "held",
    "now",
    "pretty",
    "up",
    "visible",
}

_WAFFLE_RES = (
    re.compile(r"oh,?\s+i see it this time[—\-,:]*", re.IGNORECASE),
    re.compile(r"\byep,?\s+", re.IGNORECASE),
    re.compile(r"maybe it was just the angle[^.!?]*[.!?]?", re.IGNORECASE),
    re.compile(r"if you want,?\s+i can also[^.!?]*[.!?]?", re.IGNORECASE),
    re.compile(r"i(?:'ll| will) remember that[.!]?", re.IGNORECASE),
    re.compile(r"so nice to (?:hear|see) you[^.!?]*[.!?]?", re.IGNORECASE),
    re.compile(r"what'?s on your mind[^.!?]*[.!?]?", re.IGNORECASE),
    re.compile(r"^i looked\.?\s*", re.IGNORECASE),
    re.compile(
        r"owner asked evie to remember what they showed\.?",
        re.IGNORECASE,
    ),
    re.compile(r"they said:\s*[^.!?]{0,240}[.!]?", re.IGNORECASE),
    re.compile(
        r"\bi want you to (?:memorise|memorize|remember|keep)[^.!?]*[.!?]?",
        re.IGNORECASE,
    ),
)

_THIS_IS_MY_RE = re.compile(
    r"\bthis is my\s+(?P<object>[a-z0-9][a-z0-9' -]{1,40}?)(?=\s*(?:,|\.|$| my | and | i ))",
    re.IGNORECASE,
)
_HOLDING_OBJECT_RE = re.compile(
    r"\b(?:holding|held|showing|showed|that's|that’s|it'?s)\s+"
    r"(?:a|an|the|this|that)?\s*(?P<object>[a-z][a-z0-9' -]{1,40})",
    re.IGNORECASE,
)
_SEE_OBJECT_RE = re.compile(
    r"\b(?:i see|i can see|seeing|you're holding|you are holding)\s+"
    r"(?:a|an|the|this|that)?\s*(?P<object>[a-z][a-z0-9' ,;-]{1,60})",
    re.IGNORECASE,
)
_PRINTED_RE = re.compile(
    r"(?:printed text|it reads|text reads|titled|title is|the title[,:]?|text:)\s*"
    r"[\"'“”]?(?P<text>[^.\"]{2,80})",
    re.IGNORECASE,
)


def _trim_object_phrase(raw: str) -> str:
    words: list[str] = []
    for token in re.findall(r"[a-z0-9']+", (raw or "").lower()):
        if token in _OBJECT_STOP and words:
            break
        if token in _OBJECT_STOP or token in _JUNK_OBJECT:
            continue
        if len(token) < 2:
            continue
        words.append(token)
        if len(words) >= 4:
            break
    return " ".join(words[:4])


def _object_from_see_list(text: str) -> str:
    match = re.search(r"\bi can see\s+(.+)$", text or "", re.IGNORECASE)
    if not match:
        return ""
    parts = [part.strip() for part in re.split(r"[,;]", match.group(1)) if part.strip()]
    for part in reversed(parts):
        trimmed = _trim_object_phrase(part)
        if trimmed:
            return trimmed
    return ""


def _object_from_utterance(text: str) -> str:
    blob = " ".join(str(text or "").split())
    if not blob:
        return ""
    named = _THIS_IS_MY_RE.search(blob)
    if named:
        trimmed = _trim_object_phrase(named.group("object"))
        if trimmed and trimmed.split()[0] not in _JUNK_OBJECT:
            return trimmed
    for pattern in (_HOLDING_OBJECT_RE, _SEE_OBJECT_RE):
        match = pattern.search(blob)
        if match:
            trimmed = _trim_object_phrase(match.group("object"))
            if trimmed and trimmed.split()[0] not in _JUNK_OBJECT:
                return trimmed
    listed = _object_from_see_list(blob)
    if listed:
        return listed
    return ""


def _named_object_phrase(obj: str) -> str:
    blob = " ".join(str(obj or "").split()).strip()
    if not blob:
        return "what you showed"
    lowered = blob.lower()
    if lowered.startswith(("a ", "an ", "the ")):
        return blob
    if " " in blob:
        return "the " + blob
    article = "an" if blob[:1].lower() in "aeiou" else "a"
    return f"{article} {blob}"


def clean_visual_scene(
    scene: str | None,
    *,
    keep_request: str | None = None,
) -> str:
    """Strip keep waffle and the owner's request echo from a look description."""

    text = " ".join(str(scene or "").split()).strip()
    if not text:
        return ""
    asked = " ".join(str(keep_request or "").split()).strip()
    if asked and asked.lower() in text.lower():
        text = re.sub(re.escape(asked), " ", text, flags=re.IGNORECASE)
    for pattern in _WAFFLE_RES:
        text = pattern.sub(" ", text)
    text = " ".join(text.split()).strip(" -—")
    if is_empty_visual_scene(text) or is_memory_hedge_scene(text) or is_clarity_hedge(text):
        return ""
    return text[:500]


def _preferred_scene_line(usable: str, obj: str) -> str:
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+", usable or "")
        if part.strip()
    ]
    scored: list[tuple[int, int, str]] = []
    needle = (obj or "").split()[-1] if obj else ""
    for sentence in sentences:
        lowered = sentence.lower()
        if is_empty_visual_scene(sentence) or is_memory_hedge_scene(sentence) or is_clarity_hedge(sentence):
            continue
        if lowered.startswith("you asked me to remember"):
            continue
        score = 0
        if needle and needle in lowered:
            score += 3
        if re.search(r"\b(?:that'?s|holding|held|titled|it reads)\b", lowered):
            score += 2
        if any(color in lowered for color in _COLORS):
            score += 1
        scored.append((score, -len(sentence), sentence))
    scored.sort(reverse=True)
    if scored and scored[0][0] > 0:
        return scored[0][2]
    return sentences[0] if sentences else ""


def extract_visual_identity(
    *,
    scene: str | None = None,
    ocr: str | None = None,
    labels: list[str] | None = None,
    colors: list[str] | None = None,
    keep_request: str | None = None,
) -> dict[str, Any]:
    """Generic object identity from a keep request plus what the camera saw."""

    asked = " ".join(str(keep_request or "").split()).strip()
    usable = clean_visual_scene(
        _usable_spoken(scene) or _scene_from_prompt(scene) or scene,
        keep_request=asked,
    )
    printed = " ".join(str(ocr or "").split()).strip()
    if not printed and usable:
        match = _PRINTED_RE.search(usable)
        if match:
            printed = " ".join(match.group("text").split()).strip(" .")[:160]
    names = [
        str(item).strip()
        for item in (labels or [])
        if str(item).strip() and str(item).strip().lower() not in _JUNK_OBJECT
    ]
    color_names = [
        str(item).strip().lower()
        for item in (colors or [])
        if str(item).strip() and str(item).strip().lower() in _COLORS
    ]
    if not color_names:
        color_names = [
            token
            for token in simple_tokens(usable)
            if token in _COLORS
        ]
    topic = keep_topic(asked or scene or "")
    if topic in {"", "this", "that", "it", "you"}:
        topic = ""
    obj = (
        topic
        or _object_from_utterance(asked)
        or _object_from_utterance(usable)
        or _trim_object_phrase(names[0] if names else "")
    )
    if obj in {"this", "that", "it", "you"}:
        obj = ""
    if not usable:
        spoken = "Hold it in the camera so I can see it."
        recall = (
            f"You asked me to remember {_named_object_phrase(obj)}."
            if obj
            else "You asked me to remember what you showed."
        )
        if color_names and obj and not any(name in recall.lower() for name in color_names):
            recall = recall.rstrip(".") + ". It's " + ", ".join(color_names[:3]) + "."
        return {
            "object": obj,
            "colors": color_names[:4],
            "printed": printed,
            "scene": "",
            "usable": False,
            "recall": recall[:400],
            "spoken": spoken,
        }
    parts: list[str] = []
    if usable:
        parts.append(usable.rstrip("."))
    if printed and printed.lower() not in " ".join(parts).lower():
        parts.append("It reads " + printed[:160].rstrip("."))
    spoken = ". ".join(parts)
    if "remember" not in spoken.lower():
        spoken = spoken.rstrip(".") + ". I'll remember that."
    recall = (
        f"You asked me to remember {_named_object_phrase(obj)}."
        if obj
        else "You asked me to remember what you showed."
    )
    scene_line = _preferred_scene_line(usable, obj)
    if scene_line and scene_line.lower() not in recall.lower():
        recall = recall.rstrip(".") + ". " + scene_line[0].upper() + scene_line[1:]
        if not recall.endswith((".", "!", "?")):
            recall += "."
    if printed and printed.lower() not in recall.lower():
        recall = recall.rstrip(".") + ". It reads " + printed[:160].rstrip(".") + "."
    elif color_names and not any(name in recall.lower() for name in color_names):
        recall = recall.rstrip(".") + ". It's " + ", ".join(color_names[:3]) + "."
    return {
        "object": obj,
        "colors": color_names[:4],
        "printed": printed,
        "scene": usable,
        "usable": True,
        "recall": recall[:400],
        "spoken": spoken[:400],
    }


def recall_spoken_from_keep(
    text: str | None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Owner-facing recall line from a keep fact, including older request blobs."""

    data = payload or {}
    stored = " ".join(str(data.get("recall") or "").split()).strip()
    lowered_stored = stored.lower()
    if (
        stored
        and "they said:" not in lowered_stored
        and "asked evie" not in lowered_stored
        and "it's that" not in lowered_stored
        and "i can see the with" not in lowered_stored
    ):
        return stored[:400]
    raw = " ".join(str(text or "").split()).strip()
    if not raw:
        return ""
    lowered_raw = raw.lower()
    if (
        lowered_raw.startswith("you asked me to remember")
        and "they said:" not in lowered_raw
        and "asked evie" not in lowered_raw
        and "it's that" not in lowered_raw
        and "episode:" not in lowered_raw
    ):
        return raw[:400]
    identity = extract_visual_identity(
        scene=raw,
        ocr=str(data.get("ocr_text") or data.get("printed") or "") or None,
        labels=list(data.get("labels") or []),
        colors=list(data.get("colors") or []),
        keep_request=str(data.get("keep_request") or "") or None,
    )
    if identity.get("recall"):
        return str(identity["recall"])[:400]
    return raw[:400]


def keep_owner_spoken(
    *,
    scene: str | None = None,
    ocr: str | None = None,
    labels: list[str] | None = None,
    colors: list[str] | None = None,
    keep_request: str | None = None,
    frame_ok: bool = False,
) -> str:
    """What the owner hears after memorize-from-sight.

    Live look used to return the Mini injection prompt as ``spoken``. The
    transcript broker speaks that field, so the owner heard a camera prompt
    instead of the stored scene.
    """

    identity = extract_visual_identity(
        scene=None if (frame_ok and is_clarity_hedge(scene)) else scene,
        ocr=ocr,
        labels=labels,
        colors=colors,
        keep_request=keep_request,
    )
    spoken = str(identity.get("spoken") or "").strip()
    if spoken and not is_clarity_hedge(spoken) and "hold it in the camera" not in spoken.lower():
        return spoken[:400]
    if frame_ok:
        obj = str(identity.get("object") or "").strip()
        color_names = [str(item) for item in (identity.get("colors") or []) if item]
        if obj:
            named = _named_object_phrase(obj)
            if color_names and color_names[0] not in named.lower():
                named = f"a {color_names[0]} {obj}"
            line = f"That's {named}. I'll remember that."
            return line[:400]
        if color_names:
            return f"I can see it — {', '.join(color_names[:3])}. I'll remember that."[:400]
    return spoken[:400] or "Hold it in the camera so I can see it."


def keep_sight_text(
    *,
    user_text: str,
    scene: str | None = None,
    ocr: str | None = None,
    labels: list[str] | None = None,
    colors: list[str] | None = None,
) -> str:
    """Durable recall sentence for a show-and-remember request."""

    identity = extract_visual_identity(
        scene=scene,
        ocr=ocr,
        labels=labels,
        colors=colors,
        keep_request=user_text,
    )
    text = str(identity.get("recall") or "").strip()
    if not text:
        text = "You asked me to remember what you showed."
    if not text.endswith("."):
        text += "."
    return text[:800]


def wants_current_visual(message: str | None) -> bool:
    """True when the owner wants the camera to describe what is in view now."""

    text = (message or "").strip()
    if not text:
        return False
    if wants_past_visual(text):
        return False
    return bool(CURRENT_VISUAL_RE.search(text))


def is_visual_recall_query(message: str | None) -> bool:
    """True when the owner is asking about something she already saw or saved."""

    text = (message or "").strip()
    if not text:
        return False
    if wants_keep_visible(text):
        return False
    if is_keep_recall_query(text):
        return True
    if wants_current_visual(text) and not wants_past_visual(text):
        return False
    if wants_past_visual(text):
        return True
    return bool(VISUAL_RECALL_RE.search(text))


_HEDGE_BODY = re.compile(
    r"("
    r"i (?:do not|don't|cannot|can't) (?:have|find|tell)|"
    r"i have no (?:reliable )?record|"
    r"there is no (?:reliable )?record|"
    r"no reliable record|"
    r"if you tell me|"
    r"couldn['’]?t find any stored|"
    r"let me check (?:your |what.?s in your )?(?:records|history)|"
    r"let me think back through any records|"
    r"i don['’]?t have a (?:direct )?record|"
    r"no saved message|"
    r"you can tell me what|"
    r"checked for any record|"
    r"didn['’]?t give me|"
    r"any record of (?:a |the |what )"
    r")",
    re.IGNORECASE,
)


def is_memory_hedge_scene(text: str | None) -> bool:
    """True when stored 'scene' text is a no-record hedge, not a look."""

    blob = " ".join(str(text or "").split()).strip()
    if not blob:
        return False
    blob = re.sub(r"^i looked\.?\s*", "", blob, flags=re.IGNORECASE)
    if is_empty_visual_scene(blob):
        return True
    if _HEDGE_OPENING.search(blob) or _HEDGE_BODY.search(blob):
        return True
    lowered = blob.lower()
    return any(
        cue in lowered
        for cue in (
            "read any visible title",
            "describe visible people",
            "name the main thing they are showing",
            "look at the image and describe",
            "camera image is attached",
            "describe what you actually see",
            "system confirmation",
            "life record —",
            "a little more from you",
            "what's on your mind",
            "whats on your mind",
            "nice to meet you",
            "ready to chat",
            "what would you like to explore",
            "if you share",
            "i'm always here",
            "i am always here",
            "let me take a quick look at your past",
            "i checked for any record",
            "didn't give me",
            "didn’t give me",
        )
    )


def _usable_spoken(spoken: str | None) -> str | None:
    text = " ".join(str(spoken or "").split()).strip()
    if not text:
        return None
    lowered = text.lower()
    if any(lowered.startswith(prefix) for prefix in _BOILERPLATE_PREFIXES):
        return None
    if is_memory_hedge_scene(text) or is_clarity_hedge(text) or is_empty_visual_scene(text):
        return None
    return text[:500]


def _scene_from_prompt(spoken: str | None) -> str | None:
    """Pull Grounding labels/colors out of the live-model prompt."""

    text = str(spoken or "").strip()
    if not text:
        return None
    match = _GROUNDING_RE.search(text)
    if not match:
        return None
    facts = " ".join(match.group(1).split()).strip(" .;")
    return facts[:300] or None


def looks_like_visual_description(spoken: str | None) -> bool:
    """True when assistant speech is a scene, not a memory hedge or a prompt."""

    text = " ".join(str(spoken or "").split()).strip()
    if len(text) < 12:
        return False
    lowered = text.lower()
    if any(lowered.startswith(prefix) for prefix in _BOILERPLATE_PREFIXES):
        return False
    if _HEDGE_OPENING.search(text) or is_memory_hedge_scene(text):
        return False
    if any(cue in lowered for cue in _SCENE_CUES):
        return True
    if simple_tokens(text) & (_COLORS | _CLOTHING):
        return True
    return False


def _stems(tokens: set[str]) -> set[str]:
    out = set(tokens)
    for token in tokens:
        if token.endswith("s") and len(token) > 3:
            out.add(token[:-1])
        elif len(token) >= 4:
            out.add(token + "s")
    return out


def _expand_aliases(tokens: set[str]) -> set[str]:
    out = set(tokens)
    if "shirt" in tokens or "tshirt" in tokens or "tee" in tokens:
        out.update({"shirt", "tshirt", "tee"})
    for token in list(tokens):
        out.update(_ALIASES.get(token, ()))
    return out


def visual_content_tokens(query: str | None) -> set[str]:
    """Object / clothing / color tokens, with question scaffolding removed."""

    tokens = simple_tokens(query or "")
    if "shirt" in tokens or "tshirt" in tokens:
        tokens.update({"shirt", "tshirt"})
    tokens = {token for token in tokens if len(token) >= 3 and token not in _VISUAL_SCAFFOLD}
    return _expand_aliases(tokens)


def visual_index_tokens(text: str | None) -> set[str]:
    tokens = simple_tokens(text or "")
    return _expand_aliases({token for token in tokens if len(token) >= 3})


def visual_observation_matches(query: str, text: str) -> bool:
    """Match a stored look to a follow-up without requiring question words."""

    blob = (text or "").strip()
    if not blob:
        return False
    lowered = blob.lower()
    if is_empty_visual_scene(blob) or is_memory_hedge_scene(blob):
        return False
    topic = keep_topic(query) if is_keep_recall_query(query) else ""
    if is_keep_recall_query(query) and topic in {"", "this", "that", "it", "you"}:
        return (
            "asked evie to remember" in lowered
            or "you asked me to remember" in lowered
            or "they said:" in lowered
            or "i looked" in lowered
            or "i recorded" in lowered
            or "i took a photo" in lowered
        )
    wanted = visual_content_tokens(query)
    have = visual_index_tokens(blob)
    if wanted and (_stems(wanted) & _stems(have)):
        return True
    if wanted:
        return False
    if not is_visual_recall_query(query):
        return False
    if simple_tokens(query) & _CLOTHING:
        return bool(have & (_CLOTHING | _COLORS | {"person", "people"})) or "i looked" in lowered
    return (
        "i looked" in lowered
        or "i recorded" in lowered
        or "i took a photo" in lowered
        or "i watched" in lowered
        or "asked evie to remember" in lowered
        or "you asked me to remember" in lowered
        or "they said:" in lowered
    )


def visual_observation_text(
    *,
    labels: list[str] | None = None,
    colors: list[str] | None = None,
    people: int | None = None,
    media_kind: str | None = None,
    saved_path: str | None = None,
    ocr_text: str | None = None,
    duration_s: float | None = None,
    visual_facts: str | None = None,
    spoken: str | None = None,
    keep_named: str | None = None,
) -> str:
    """One searchable sentence Evie can recall later."""

    kind = str(media_kind or "frame").strip().lower()
    if kind in {"video", "clip"}:
        lead = "I recorded a video clip"
    elif kind in {"photo", "image"}:
        lead = "I took a photo"
    elif kind == "observe":
        lead = "I watched the camera"
    else:
        lead = "I looked"
    bits: list[str] = []
    count = int(people or 0)
    if count == 1:
        bits.append("a person")
    elif count > 1:
        bits.append(f"{count} people")
    seen: set[str] = {item.lower() for item in bits}
    for name in labels or []:
        raw = str(name or "").strip()
        if not raw or raw.lower() in seen or raw.lower() in {"person", "people", "human"}:
            continue
        bits.append(raw)
        seen.add(raw.lower())
    color_names = [str(name).strip() for name in (colors or []) if str(name).strip()]
    ocr = " ".join(str(ocr_text or "").split())
    facts = str(visual_facts or "").strip()
    scene = _usable_spoken(spoken) or _scene_from_prompt(spoken)
    parts = [lead]
    if scene:
        parts.append(scene)
    elif bits:
        parts.append("of " + ", ".join(bits[:8]))
    elif facts:
        parts.append(facts)
    if bits and scene:
        missing = [item for item in bits[:8] if item.lower() not in scene.lower()]
        if missing:
            parts.append("Also visible: " + ", ".join(missing))
    if color_names and (not scene or not any(name.lower() in scene.lower() for name in color_names)):
        parts.append("Colors: " + ", ".join(color_names[:4]))
    if ocr and (not scene or ocr.lower() not in scene.lower()):
        parts.append("Text: " + ocr[:120])
    if duration_s and kind in {"video", "clip"}:
        parts.append(f"Duration {float(duration_s):.0f} seconds")
    if saved_path:
        parts.append(f"Saved to {saved_path}")
    named = " ".join(str(keep_named or "").split()).strip()
    if named and named.lower() not in {"this", "that", "it"}:
        asked = f"They asked Evie to remember the {named}"
        blob = " ".join(parts).lower()
        if named.lower() not in blob:
            parts.append(asked)
    text = ". ".join(part.rstrip(".") for part in parts if part).strip()
    if not text:
        text = lead
    if not text.endswith("."):
        text += "."
    return text[:800]


def _iso_time(value: Any) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    return str(value)


def _event_text(event: Event) -> str:
    return str((event.content or {}).get("text") or "").strip()


def _observation_row(event: Event, *, score: float, reason: str) -> dict[str, Any]:
    text = _event_text(event)
    when = _iso_time(event.occurred_at)
    return {
        "id": str(event.id),
        "when": when,
        "text": text[:400],
        "score": round(score, 4),
        "kind": "event",
        "memory_type": "observation",
        "source": "evie",
        "confidence": "visual_observation",
        "event_type": event.event_type,
        "event_source": event.source,
        "conversation_id": str(event.conversation_id) if event.conversation_id else None,
        "occurred_at": event.occurred_at,
        "parts": {"lexical": 0.5, "speaker": 0.92, "recency": 1.0, "phrase": 1.0},
        "reason": reason,
    }


async def search_visual_observations(
    session: AsyncSession,
    query: str,
    *,
    k: int = 6,
    until=None,
) -> list[dict[str, Any]]:
    """Find camera.observation rows by object/clothing/color, not question words."""

    stmt = (
        select(Event)
        .where(
            Event.tombstoned_at.is_(None),
            Event.event_type == VISUAL_EVENT_TYPE,
            Event.privacy_level != "never_send_to_model",
            Event.privacy_level != "sensitive",
        )
        .order_by(Event.occurred_at.desc())
        .limit(80)
    )
    if until is not None:
        stmt = stmt.where(Event.occurred_at <= until)
    rows = list((await session.execute(stmt)).scalars().all())
    from app.models import Memory

    keep_rows = list(
        (
            await session.execute(
                select(Memory)
                .where(
                    Memory.redacted.is_(False),
                    Memory.is_current.is_(True),
                    Memory.memory_type.in_(("observation", "fact")),
                )
                .order_by(Memory.event_time.desc())
                .limit(80)
            )
        ).scalars().all()
    )
    keep_hits: list[dict[str, Any]] = []
    other_hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in keep_rows:
        payload = row.payload or {}
        kind = str(payload.get("kind") or "")
        if kind not in {"visual", "visual_keep"} and row.memory_type != "observation":
            continue
        if kind not in {"visual", "visual_keep"} and str(row.text or "").lower().startswith("observed:"):
            continue
        blob = " ".join(
            part
            for part in (
                row.text,
                str(payload.get("keep_request") or ""),
                str(payload.get("value") or ""),
                str(payload.get("topic") or ""),
                str(payload.get("object") or ""),
                str(payload.get("recall") or ""),
                str(payload.get("printed") or payload.get("ocr_text") or ""),
            )
            if part
        )
        if is_memory_hedge_scene(row.text):
            continue
        if not visual_observation_matches(query, blob):
            continue
        memory_id = str(row.id)
        if memory_id in seen:
            continue
        seen.add(memory_id)
        recall = recall_spoken_from_keep(row.text, payload)
        item = {
            "id": memory_id,
            "source": "memory",
            "when": _iso_time(row.event_time),
            "text": recall or row.text,
            "kind": "memory",
            "memory_type": row.memory_type,
            "object": payload.get("object") or payload.get("topic") or "",
            "recall": recall,
            "score": 0.94 if kind == "visual_keep" or row.memory_type == "fact" else 0.9,
            "occurred_at": row.event_time,
            "parts": {"lexical": 0.6, "speaker": 0.95, "recency": 1.0, "phrase": 1.0},
            "reason": "visual_keep" if kind == "visual_keep" or row.memory_type == "fact" else "visual_observation",
        }
        if kind == "visual_keep" or row.memory_type == "fact":
            keep_hits.append(item)
        else:
            other_hits.append(item)
    hits: list[dict[str, Any]] = keep_hits + other_hits
    if len(hits) >= max(1, k):
        return hits[: max(1, k)]
    for event in rows:
        text = _event_text(event)
        if is_memory_hedge_scene(text):
            continue
        keep_asked = str((event.content or {}).get("keep_request") or "")
        haystack = f"{text} {keep_asked}".strip()
        if not visual_observation_matches(query, haystack):
            continue
        event_id = str(event.id)
        if event_id in seen:
            continue
        seen.add(event_id)
        hits.append(_observation_row(event, score=0.86, reason="visual_content"))
        if len(hits) >= max(1, k):
            break
    return hits


async def _recent_keep_request(
    session: AsyncSession,
    *,
    device_id: str | None = None,
) -> str:
    """Keep utterance from a recent empty look, so a later clear frame can store it."""

    cutoff = utcnow() - SPOKEN_SCENE_WINDOW
    stmt = (
        select(Event)
        .where(
            Event.event_type == VISUAL_EVENT_TYPE,
            Event.tombstoned_at.is_(None),
            Event.occurred_at >= cutoff,
        )
        .order_by(Event.occurred_at.desc())
        .limit(8)
    )
    if device_id:
        stmt = stmt.where((Event.device_id == device_id) | (Event.device_id.is_(None)))
    rows = (await session.execute(stmt)).scalars().all()
    for event in rows:
        content = dict(event.content or {})
        asked = " ".join(str(content.get("keep_request") or "").split()).strip()
        if not wants_keep_visible(asked):
            continue
        labels = [str(item) for item in (content.get("labels") or []) if item]
        ocr = str(content.get("ocr_text") or "").strip()
        scene = str(content.get("spoken") or "")
        if wants_keep_visible(asked) and not labels and not ocr:
            if (
                not scene
                or is_empty_visual_scene(scene)
                or is_memory_hedge_scene(scene)
            ):
                return asked[:400]
    return ""


async def _latest_user_text(session: AsyncSession) -> str:
    row = (
        await session.execute(
            select(Event)
            .where(
                Event.event_type == "message.user",
                Event.tombstoned_at.is_(None),
            )
            .order_by(Event.occurred_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return ""
    return str((row.content or {}).get("text") or "").strip()


def _keep_request_from_result(result: dict[str, Any]) -> str:
    """Owner utterance bound to this look — not a later message.user race."""

    for key in ("keep_request", "owner_request", "prompt"):
        text = " ".join(str(result.get(key) or "").split()).strip()
        if not text:
            continue
        if text.lower().startswith("describe visible people"):
            continue
        return text[:400]
    return ""


async def _pin_memory_ids(session: AsyncSession, memory_ids: list[str]) -> None:
    from uuid import UUID

    from app.models import Memory
    from sqlalchemy.orm.attributes import flag_modified

    for raw in memory_ids:
        try:
            memory = await session.get(Memory, UUID(str(raw)))
        except (TypeError, ValueError):
            continue
        if memory is None or not memory.is_current:
            continue
        extra = dict(memory.extra or {})
        extra["pinned"] = True
        extra["pinned_at"] = utcnow().isoformat()
        extra["visual_keep"] = True
        memory.extra = extra
        memory.importance = min(1.0, max(memory.importance, 0.95))
        flag_modified(memory, "extra")


async def _supersede_placeholder_visual_keeps(
    session: AsyncSession,
    *,
    keep_memory_id: str,
) -> None:
    """Drop empty keep facts once a later look stored a real identity."""

    from uuid import UUID

    from app.models import Memory
    from sqlalchemy.orm.attributes import flag_modified

    try:
        successor = UUID(str(keep_memory_id))
    except (TypeError, ValueError):
        return
    cutoff = utcnow() - SPOKEN_SCENE_WINDOW
    rows = list(
        (
            await session.execute(
                select(Memory).where(
                    Memory.redacted.is_(False),
                    Memory.is_current.is_(True),
                    Memory.memory_type == "fact",
                    Memory.event_time >= cutoff,
                    Memory.id != successor,
                )
            )
        ).scalars().all()
    )
    for memory in rows:
        payload = dict(memory.payload or {})
        if payload.get("kind") != "visual_keep":
            continue
        if payload.get("usable_scene"):
            continue
        memory.is_current = False
        memory.superseded_by_id = successor
        memory.valid_until = utcnow()
        extra = dict(memory.extra or {})
        extra["superseded_reason"] = "visual_identity"
        memory.extra = extra
        flag_modified(memory, "extra")
        flag_modified(memory, "payload")


async def persist_visual_observation(
    session: AsyncSession,
    result: dict[str, Any],
    *,
    actor: str = "owner",
    device_id: str | None = None,
) -> dict[str, Any] | None:
    """Write Event + Memory for a successful camera result. Never fails the look."""

    if not result.get("ok"):
        return None
    labels = [str(item) for item in (result.get("labels") or []) if item]
    colors = [str(item) for item in (result.get("colors") or []) if item]
    saved_path = str(result.get("saved_path") or "").strip() or None
    facts = str(result.get("visual_facts") or "").strip() or None
    ocr = str(result.get("ocr_text") or result.get("local_ocr") or "").strip() or None
    raw_spoken = str(result.get("spoken") or "")
    spoken = _usable_spoken(raw_spoken) or _scene_from_prompt(raw_spoken)
    people = result.get("person_count") or result.get("face_count")
    try:
        people_n = int(people) if people is not None else 0
    except (TypeError, ValueError):
        people_n = 0
    media_kind = str(result.get("media_kind") or "frame").strip().lower() or "frame"
    if result.get("observe") and media_kind == "frame":
        media_kind = "observe"
    keep_user = _keep_request_from_result(result)
    if not keep_user:
        keep_user = await _latest_user_text(session)
    scene_for_keep = spoken or raw_spoken
    empty_scene = (
        is_empty_visual_scene(scene_for_keep)
        or is_memory_hedge_scene(scene_for_keep)
        or is_clarity_hedge(scene_for_keep)
    )
    try:
        encoded_bytes = int(result.get("encoded_bytes") or 0)
    except (TypeError, ValueError):
        encoded_bytes = 0
    frame_ok = bool(result.get("attachment_id") or encoded_bytes > 0 or result.get("image_ready"))
    usable_scene = bool(ocr) or (
        not empty_scene and bool(spoken or labels or colors)
    )
    if empty_scene and frame_ok and wants_keep_visible(keep_user):
        scene_for_keep = ""
        usable_scene = bool(ocr or labels or colors)
    if not wants_keep_visible(keep_user) and usable_scene:
        pending = await _recent_keep_request(session, device_id=device_id)
        if pending:
            keep_user = pending
    identity = extract_visual_identity(
        scene=scene_for_keep if usable_scene and not empty_scene else None,
        ocr=ocr if (usable_scene or frame_ok) else None,
        labels=labels if (usable_scene or frame_ok) else [],
        colors=colors if (usable_scene or frame_ok) else [],
        keep_request=keep_user if wants_keep_visible(keep_user) else None,
    )
    keep_named = str(identity.get("object") or "").strip()
    if not keep_named:
        keep_named = keep_topic(keep_user) if wants_keep_visible(keep_user) else ""
    if keep_named in {"this", "that", "it", "you"}:
        keep_named = ""
    text = visual_observation_text(
        labels=labels,
        colors=colors,
        people=people_n,
        media_kind=media_kind,
        saved_path=saved_path,
        ocr_text=ocr,
        duration_s=result.get("duration_s"),
        visual_facts=facts,
        spoken=identity.get("scene") or raw_spoken,
        keep_named=keep_named or None,
    )
    payload = {
        "kind": "visual",
        "labels": labels,
        "colors": colors,
        "people": people_n or None,
        "saved_path": saved_path,
        "media_kind": media_kind,
        "attachment_id": result.get("attachment_id"),
        "request_id": result.get("request_id"),
        "object": keep_named or None,
        "topic": keep_named
        if keep_named
        else ((labels[0] if labels else None) or ("scene" if colors else "camera")),
        "duration_s": result.get("duration_s"),
        "spoken": spoken,
        "keep_request": keep_user or None,
        "recall": identity.get("recall") if wants_keep_visible(keep_user) else None,
    }
    try:
        event = await EventService(session, actor=actor).create(
            EventCreate(
                source="camera",
                event_type=VISUAL_EVENT_TYPE,
                text=text,
                content={
                    "text": text,
                    "labels": labels,
                    "colors": colors,
                    "saved_path": saved_path,
                    "media_kind": media_kind,
                    "visual_facts": facts,
                    "spoken": spoken,
                    "request_id": result.get("request_id"),
                    "attachment_id": result.get("attachment_id"),
                    "people": people_n or None,
                    "ocr_text": ocr,
                    "keep_request": keep_user or None,
                    "provenance": "phone_camera" if device_id else "camera",
                },
                metadata={"visual": True, "visor": True},
                device_id=device_id,
                privacy_level="normal",
            )
        )
        from app.embeddings import get_embedder
        from app.memory.writer import MemoryWriter

        entities: list[EntityRef] = []
        for name in labels[:6]:
            entities.append(EntityRef(name=name, entity_type="object", role="seen"))
        for name in colors[:4]:
            entities.append(EntityRef(name=name, entity_type="other", role="color"))
        if keep_named and keep_named.lower() not in {"this", "that", "it"}:
            seen_names = {item.name.lower() for item in entities}
            for name in keep_named.split()[:4]:
                if name.lower() not in seen_names:
                    entities.append(EntityRef(name=name, entity_type="object", role="shown"))
                    seen_names.add(name.lower())
        writer = MemoryWriter(session, embeddings=get_embedder())
        candidates = [
            MemoryCandidate(
                memory_type="observation",
                text=text,
                payload=payload,
                importance=0.9 if wants_keep_visible(keep_user) else 0.62,
                confidence=0.78 if labels or colors or spoken else 0.6,
                source_type="derived" if media_kind in {"frame", "observe"} else "explicit",
                privacy_level="normal",
                event_time=utcnow(),
                entities=entities,
            )
        ]
        if wants_keep_visible(keep_user):
            keep_text = keep_sight_text(
                user_text=keep_user,
                scene=(spoken or raw_spoken) if usable_scene and not empty_scene else None,
                ocr=ocr if (usable_scene or frame_ok) else None,
                labels=labels if (usable_scene or frame_ok) else None,
                colors=colors if (usable_scene or frame_ok) else None,
            )
            topic = keep_named or keep_topic(keep_user) or "this"
            candidates.append(
                MemoryCandidate(
                    memory_type="fact",
                    text=keep_text,
                    payload={
                        "subject": topic,
                        "property": "shown",
                        "value": keep_text,
                        "kind": "visual_keep",
                        "topic": topic,
                        "object": keep_named or None,
                        "colors": list(identity.get("colors") or []) if usable_scene else [],
                        "printed": identity.get("printed") if usable_scene else None,
                        "recall": keep_text,
                        "usable_scene": usable_scene,
                        "labels": labels if usable_scene else [],
                        "ocr_text": ocr if usable_scene else None,
                        "keep_request": keep_user[:400],
                    },
                    importance=0.96,
                    confidence=0.9 if usable_scene else 0.7,
                    source_type="explicit",
                    privacy_level="normal",
                    event_time=utcnow(),
                    entities=entities,
                )
            )
        written = await writer.write_all(event, candidates)
        await session.flush()
        if written:
            result["remembered"] = True
            result["memory_id"] = written[0].memory_id
            result["memory_text"] = written[-1].text
            if wants_keep_visible(keep_user):
                await _pin_memory_ids(session, [row.memory_id for row in written])
                result["kept"] = True
                if usable_scene or (frame_ok and keep_named):
                    keep_ids = [
                        row.memory_id
                        for row in written
                        if row.memory_type == "fact"
                    ]
                    await _supersede_placeholder_visual_keeps(
                        session,
                        keep_memory_id=keep_ids[-1] if keep_ids else written[-1].memory_id,
                    )
        return {
            "event_id": str(event.id),
            "memory_id": written[0].memory_id if written else None,
            "kept": bool(result.get("kept")),
        }
    except Exception:  # noqa: BLE001 - recall must never block seeing
        logger.warning("visual observation persist skipped", extra={"device_id": device_id}, exc_info=True)
        return None


async def remember_spoken_scene(
    session: AsyncSession,
    spoken: str,
    *,
    actor: str = "owner",
    device_id: str | None = None,
) -> dict[str, Any] | None:
    """Attach the live spoken description to the latest look.

    Events are immutable, so a richer observation is written beside the
    grounding row rather than rewriting it.
    """

    scene = _usable_spoken(spoken)
    if not scene or not looks_like_visual_description(scene):
        return None
    cutoff = utcnow() - SPOKEN_SCENE_WINDOW
    stmt = (
        select(Event)
        .where(
            Event.event_type == VISUAL_EVENT_TYPE,
            Event.tombstoned_at.is_(None),
            Event.occurred_at >= cutoff,
        )
        .order_by(Event.occurred_at.desc())
        .limit(1)
    )
    if device_id:
        stmt = stmt.where((Event.device_id == device_id) | (Event.device_id.is_(None)))
    event = (await session.execute(stmt)).scalars().first()
    if event is None:
        return None
    existing = _event_text(event)
    if scene.lower() in existing.lower():
        return None
    content = dict(event.content or {})
    labels = [str(item) for item in (content.get("labels") or []) if item]
    colors = [str(item) for item in (content.get("colors") or []) if item]
    people = content.get("people")
    try:
        people_n = int(people) if people is not None else 0
    except (TypeError, ValueError):
        people_n = 0
    result = {
        "ok": True,
        "labels": labels,
        "colors": colors,
        "person_count": people_n,
        "media_kind": content.get("media_kind") or "frame",
        "saved_path": content.get("saved_path"),
        "visual_facts": content.get("visual_facts"),
        "spoken": scene,
        "ocr_text": content.get("ocr_text"),
        "request_id": content.get("request_id"),
        "attachment_id": content.get("attachment_id"),
        "keep_request": content.get("keep_request"),
    }
    return await persist_visual_observation(
        session, result, actor=actor, device_id=device_id or event.device_id
    )


async def persist_keep_intent(
    session: AsyncSession,
    keep_request: str,
    *,
    actor: str = "owner",
    device_id: str | None = None,
    scene: str | None = None,
    ocr: str | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any] | None:
    """Write a durable keep fact when memorize-from-sight has no usable glance."""

    asked = " ".join(str(keep_request or "").split()).strip()
    if not wants_keep_visible(asked):
        return None
    return await persist_visual_observation(
        session,
        {
            "ok": True,
            "labels": [str(item).strip() for item in (labels or []) if str(item).strip()],
            "keep_request": asked[:400],
            "spoken": scene,
            "ocr_text": ocr,
            "media_kind": "frame",
        },
        actor=actor,
        device_id=device_id,
    )


async def attach_keep_to_latest_look(
    session: AsyncSession,
    keep_request: str,
    *,
    actor: str = "owner",
    device_id: str | None = None,
) -> dict[str, Any] | None:
    """If memorize arrives after a glance, pin keep onto that look instead of losing it."""

    asked = " ".join(str(keep_request or "").split()).strip()
    if not wants_keep_visible(asked):
        return None
    cutoff = utcnow() - SPOKEN_SCENE_WINDOW
    stmt = (
        select(Event)
        .where(
            Event.event_type == VISUAL_EVENT_TYPE,
            Event.tombstoned_at.is_(None),
            Event.occurred_at >= cutoff,
        )
        .order_by(Event.occurred_at.desc())
        .limit(1)
    )
    if device_id:
        stmt = stmt.where((Event.device_id == device_id) | (Event.device_id.is_(None)))
    event = (await session.execute(stmt)).scalars().first()
    if event is None:
        return None
    content = dict(event.content or {})
    labels = [str(item) for item in (content.get("labels") or []) if item]
    colors = [str(item) for item in (content.get("colors") or []) if item]
    ocr = str(content.get("ocr_text") or "").strip()
    scene = str(content.get("spoken") or content.get("text") or "")
    if is_empty_visual_scene(scene) and not labels and not ocr:
        return None
    people = content.get("people")
    try:
        people_n = int(people) if people is not None else 0
    except (TypeError, ValueError):
        people_n = 0
    result = {
        "ok": True,
        "labels": labels,
        "colors": colors,
        "person_count": people_n,
        "media_kind": content.get("media_kind") or "frame",
        "saved_path": content.get("saved_path"),
        "visual_facts": content.get("visual_facts"),
        "spoken": content.get("spoken"),
        "ocr_text": content.get("ocr_text"),
        "request_id": content.get("request_id"),
        "attachment_id": content.get("attachment_id"),
        "keep_request": asked[:400],
    }
    return await persist_visual_observation(
        session, result, actor=actor, device_id=device_id or event.device_id
    )
