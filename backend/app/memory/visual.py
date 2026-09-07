"""Persist Evie's camera observations as recallable memory.

Look/capture/record used to die in tool JSON, and the live model's spoken
description never made it into search. This writes searchable observations
(people, clothing, objects, colors, path) so later questions hit
camera.observation + Memory — not Apple Photos, and not question scaffolding.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import EntityRef, MemoryCandidate
from app.models import Event
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.utils.text import normalize_text, simple_tokens, utcnow

logger = logging.getLogger("ev.memory.visual")

VISUAL_EVENT_TYPE = "camera.observation"
SPOKEN_SCENE_WINDOW = timedelta(minutes=3)
# Mini's follow-up look must reload this JPEG, not capture a second keep.
KEEP_MINI_RELOAD_SECONDS = 15.0
# Reopen recall must finish before the live websocket ping timeout (~40s).
KEEP_RECALL_ENRICH_SECONDS = 28.0

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
    r"\bwhat(?:'s| is) in my hand\b|"
    r"\bin my hand\b|"
    r"\bi(?:'m| am) holding(?! (?:a |the )?(?:meeting|call|interview|session))\b|"
    r"\bthe (?:thing|item|object) i(?:'m| am) holding\b|"
    r"\bthis item i(?:'m| am) holding\b|"
    r"\bwhat i(?:'m| am) (?:holding|showing)\b|"
    r"\blook at (?:this|that|me|the (?:thing|item|object)|what i(?:'m| am))\b|"
    r"\bi want you to look at\b|"
    r"\btell me (?:more )?(?:info )?about (?:this|that) (?:item|thing|object)\b|"
    r"\bmore info about (?:this|that) (?:item|thing|object)\b|"
    r"\bwhat do you see\b|"
    r"\bwhat(?:'s| is) (?:this|that)\b|"
    r"\bwhat color is this\b|"
    r"\bcan you see (?:this|that|me)\b|"
    r"\b(?:use|open|point) (?:the |your )?camera\b"
    r")",
    re.IGNORECASE,
)

HELD_OBJECT_RE = re.compile(
    r"("
    r"\bwhat am i holding\b|"
    r"\bwhat(?:'s| is) in my hand\b|"
    r"\bin my hand\b|"
    r"\bi(?:'m| am) holding(?! (?:a |the )?(?:meeting|call|interview|session))\b|"
    r"\bthe (?:thing|item|object) i(?:'m| am) holding\b|"
    r"\bthis item i(?:'m| am) holding\b|"
    r"\bwhat i(?:'m| am) (?:holding|showing)\b|"
    r"\blook at (?:the (?:thing|item|object)|what i(?:'m| am))\b|"
    r"\btell me (?:more )?(?:info )?about (?:this|that) (?:item|thing|object)\b|"
    r"\bmore info about (?:this|that) (?:item|thing|object)\b"
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
    r"\bwhat (?:did )?i (?:just )?(?:ask|asked|tell|told) you to (?:remember|memorise|memorize|keep)\b|"
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
    r"i (?:do not|don't|didn'?t|did not|could not|couldn't) see(?: any| that| this| it| the| anything)?|"
    r"did not see anything|"
    r"camera did not return a frame|"
    r"no camera source is currently connected|"
    r"can't see a camera frame|"
    r"no (?:text|objects|people).{0,24}detected|"
    r"i (?:can't|cannot) see (?:anything|the (?:image|object|thing)|(?:this|that|it) clearly)|"
    r"perception completed|"
    r"no summary returned by the perception provider|"
    r"intelligence provider is unavailable"
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
    if blob.startswith("this is a current photo from the owner") or blob.startswith(
        "a current camera image is attached"
    ):
        return True
    if blob.startswith("start with a, an, or the"):
        return True
    if "do not read these instructions" in blob:
        return True
    if "concrete noun" in blob and "not container" in blob:
        return True
    return False


_KEEP_ACK_ONLY_RE = re.compile(
    r"^(?:"
    r"ok(?:ay)?|got it|sure|alright|all right|will do|done|"
    r"i(?:'ve| have) got it"
    r")"
    r"(?:[,.]?\s+(?:i(?:'ll| will) remember(?: that)?))?[\s.!?]*$",
    re.IGNORECASE,
)
_KEEP_ACK_REMEMBER_ONLY_RE = re.compile(
    r"^i(?:'ll| will) remember(?: that)?[\s.!?]*$",
    re.IGNORECASE,
)


def is_keep_ack_only(text: str | None) -> bool:
    """True when speech is only an acknowledgement, not a visual identity."""

    blob = " ".join(str(text or "").split()).strip()
    if not blob:
        return True
    return bool(_KEEP_ACK_ONLY_RE.match(blob) or _KEEP_ACK_REMEMBER_ONLY_RE.match(blob))


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
    "keys": frozenset({"key", "keys", "keychain"}),
    "key": frozenset({"key", "keys", "keychain"}),
    "charger": frozenset({"charger", "cable", "cord", "brick"}),
    "cable": frozenset({"charger", "cable", "cord"}),
    "wallet": frozenset({"wallet", "billfold"}),
    "airpods": frozenset({"airpods", "earbuds", "earphones", "headphones"}),
    "remote": frozenset({"remote", "clicker"}),
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
    "buttons",
    "in your hand",
    "you're holding",
    "that's a",
    "that's the",
    "it's a",
    "it is a",
    "with a",
    "made of",
)

# Life-archive / tool speech is not a look. "I can see Mummy as a contact"
# must not become the keep identity for whatever they were holding.
_NONVISUAL_KEEP_CUES = (
    "as a contact",
    "contacts app",
    "check your contacts",
    "last you talked",
    "i found a reference",
    "run failed",
    "pull request",
    "whatsapp thread",
    "don't have the phone",
    "do not have the phone",
    "don't have the number",
    "do not have the number",
    "hey there",
    "hi again",
    "nice to hear",
    "what's up",
    "whats up",
    "what's going on",
    "what would you like",
    "anything you need",
    "hanging out",
    "chat loop",
    "hope you're good",
    "hope you are good",
    "what's on your mind",
    "whats on your mind",
    "grocery list",
    "add items directly",
    "open the finder",
    "list all the files",
    "files appearing",
    "are you trying to add",
)
_NONVISUAL_KEEP_RE = re.compile(
    r"("
    r"\b(?:github|gitlab)\.com\b|"
    r"\[[\w.-]+/[\w.-]+\]|"
    r"\bci (?:run|failed|passing)\b|"
    r"\bactions run\b|"
    r"\b(?:can'?t|cannot)\b.{0,48}\bfrom here\b|"
    r"\bgrocery list tool\b"
    r")",
    re.IGNORECASE,
)


def wants_past_visual(message: str | None) -> bool:
    """True when the owner is asking about a prior look, not the live frame."""

    from app.memory.room import looks_like_object_locate

    text = (message or "").strip()
    if looks_like_object_locate(text):
        return True
    return bool(PAST_VISUAL_RE.search(text))


def is_keep_recall_query(message: str | None) -> bool:
    """True when they ask whether a prior keep-from-sight request was stored."""

    return bool(KEEP_RECALL_RE.search((message or "").strip()))


_KEEP_RECALL_ECHO_RE = re.compile(
    r"^(?:"
    r"(?:what )?(?:did )?i (?:just )?(?:ask|asked|tell|told) you to "
    r"(?:remember|memorise|memorize|keep)"
    r"(?: (?:or|and) (?:to )?(?:remember|memorise|memorize|keep))?"
    r"|(?:just )?ask(?:ed)? you to remember"
    r")$",
    re.IGNORECASE,
)


def is_keep_recall_echo(text: str | None) -> bool:
    """True when the line is the recall question itself, not the shown thing."""

    blob = " ".join(str(text or "").split()).strip()
    if not blob:
        return False
    if blob.endswith("?"):
        return True
    return bool(_KEEP_RECALL_ECHO_RE.match(blob.rstrip("?.!")))


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


def keep_perception_allow_raw(keep_request: str | None) -> bool:
    """Keep-from-sight must read the JPEG. Classifier labels are not an identity."""

    return wants_keep_visible(keep_request)


def is_nonvisual_keep_speech(spoken: str | None) -> bool:
    """True when speech is archive/tool talk, not a description of the shown thing."""

    blob = " ".join(str(spoken or "").split()).strip().lower()
    blob = blob.replace("’", "'").replace("‘", "'")
    if not blob:
        return False
    if any(cue in blob for cue in _NONVISUAL_KEEP_CUES):
        return True
    return bool(_NONVISUAL_KEEP_RE.search(blob))


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
        "accessory",
        "adult",
        "artifact",
        "background",
        "camera",
        "circle",
        "container",
        "cloth",
        "curtain",
        "cylinder",
        "device",
        "electronics",
        "equipment",
        "fabric",
        "finger",
        "furniture",
        "gadget",
        "goods",
        "hand",
        "hands",
        "household",
        "human",
        "image",
        "indoor",
        "item",
        "material",
        "object",
        "objects",
        "optical",
        "outdoor",
        "oval",
        "package",
        "packaging",
        "people",
        "person",
        "photo",
        "product",
        "rectangle",
        "room",
        "scene",
        "setting",
        "shape",
        "shaped",
        "sign",
        "something",
        "square",
        "still",
        "structure",
        "stuff",
        "thing",
        "vehicle",
    }
)
# Named but not reusable on their own — need a detail, color+mark, or printed text.
_VAGUE_CLASS = frozenset(
    {
        "bag",
        "bags",
        "book",
        "books",
        "bottle",
        "bottles",
        "box",
        "boxes",
        "cup",
        "cups",
        "mug",
        "mugs",
        "phone",
        "phones",
        "remote",
        "remotes",
        "smartphone",
        "smartphones",
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
_NOUN_LEAD_RE = re.compile(
    r"^(?:oh[,.]?\s+|so[,.]?\s+)?"
    r"(?:a|an|the)\s+(?P<object>[a-z0-9][a-z0-9' -]{1,50})",
    re.IGNORECASE,
)
_KEEP_HEADER_RE = re.compile(
    r"^(?:you asked me to remember|owner asked evie to remember) [^.!?\n]+[.!?]?\s*",
    re.IGNORECASE,
)
_KEEP_SHOWN_RE = re.compile(
    r"^(?:what you showed|what they showed|what you were showing)\.?\s*",
    re.IGNORECASE,
)
_SHAPE_HEDGE_RE = re.compile(
    r"\b(?:container|object|item|device|product|electronics|shape|package|thing)"
    r"[- ]shaped(?:\s+(?:thing|object|item|device))?\b"
    r"|\bshaped\s+(?:thing|object|item|container)\b",
    re.IGNORECASE,
)
_GENERIC_SCENE_LEAD_RE = re.compile(
    r"^(?:oh[,.]?\s+|so[,.]?\s+)?"
    r"(?:i can see|visible:|that(?:'s| is)|it(?:'s| is)|"
    r"you(?:'re| are) holding|holding)\s+",
    re.IGNORECASE,
)
_LOCATION_NOISE = frozenset(
    {
        "background",
        "camera",
        "counter",
        "desk",
        "floor",
        "hand",
        "hands",
        "indoor",
        "outdoor",
        "room",
        "shelf",
        "table",
        "wall",
    }
)
_IDENTITY_SKIP = frozenset(
    {
        "a",
        "an",
        "and",
        "can",
        "held",
        "holding",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "see",
        "the",
        "this",
        "that",
        "was",
        "with",
        "you",
        "your",
        "im",
        "ill",
        "its",
        "thats",
        "theyre",
        "youre",
        "youve",
        "asked",
        "ask",
        "remember",
        "memorize",
        "memorise",
        "showed",
        "shown",
        "showing",
        "what",
    }
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
        if token in _COLORS and not words:
            continue
        if len(token) < 2:
            continue
        words.append(token)
        if len(words) >= 4:
            break
    return " ".join(words[:4])


def _useful_object(phrase: str | None) -> str:
    trimmed = _trim_object_phrase(phrase or "")
    if not trimmed:
        return ""
    first = trimmed.split()[0]
    if first in _JUNK_OBJECT or first in {"this", "that", "it", "you"}:
        return ""
    if _SHAPE_HEDGE_RE.search(phrase or "") or _SHAPE_HEDGE_RE.search(trimmed):
        return ""
    return trimmed


def _remainder_has_identity(text: str | None) -> bool:
    """True when leftover wording still names a concrete thing."""

    for token in re.findall(r"[a-z0-9']+", str(text or "").lower()):
        token = token.replace("'", "")
        if (
            token in _JUNK_OBJECT
            or token in _VAGUE_CLASS
            or token in _LOCATION_NOISE
            or token in _COLORS
            or token in _IDENTITY_SKIP
        ):
            continue
        if len(token) < 3:
            continue
        return True
    return False


def is_generic_label_scene(text: str | None) -> bool:
    """True when the 'scene' is only classifier labels, not what they showed."""

    blob = " ".join(str(text or "").split()).strip().lower()
    blob = re.sub(r"\bi(?:'ll| will) remember that[.!?]*$", "", blob).strip()
    blob = _KEEP_HEADER_RE.sub("", blob, count=1).strip(" .")
    blob = re.sub(
        r"^owner asked evie to remember[^.!?\n]*[.!]?\s*",
        "",
        blob,
    ).strip(" .")
    blob = _KEEP_SHOWN_RE.sub("", blob).strip(" .")
    if not blob:
        return True
    if _SHAPE_HEDGE_RE.search(blob) and not _remainder_has_identity(
        _SHAPE_HEDGE_RE.sub(" ", blob)
    ):
        return True
    class_lead = re.match(
        r"^(?:oh[,.]?\s+|so[,.]?\s+)?"
        r"(?:that(?:'s| is)|it(?:'s| is)|you(?:'re| are) holding|holding)\s+"
        r"(?:a|an|the)\s+(?P<obj>[a-z]+)\b",
        blob,
        re.IGNORECASE,
    )
    if class_lead and class_lead.group("obj").lower() in (_JUNK_OBJECT | _VAGUE_CLASS):
        rest = blob[class_lead.end() :].strip(" .,")
        if not rest or re.match(
            r"^(?:it reads|printed text:?|text:?)\s+\S",
            rest,
            re.IGNORECASE,
        ):
            return True
    match = re.match(r"^(?:i can see|visible:)\s+(.+)$", blob)
    if match:
        parts = [part.strip() for part in re.split(r"[,;]", match.group(1)) if part.strip()]
        if not parts or (
            all(not _useful_object(part) for part in parts)
            and not _remainder_has_identity(match.group(1))
        ):
            return True
    stripped = _GENERIC_SCENE_LEAD_RE.sub("", blob).strip(" .")
    if stripped != blob or re.match(r"^(?:a|an|the)\s+", stripped):
        if not _remainder_has_identity(stripped):
            return True
    return not _remainder_has_identity(blob)


def _object_from_see_list(text: str) -> str:
    match = re.search(r"\bi can see\s+(.+)$", text or "", re.IGNORECASE)
    if not match:
        return ""
    parts = [part.strip() for part in re.split(r"[,;]", match.group(1)) if part.strip()]
    for part in reversed(parts):
        useful = _useful_object(part)
        if useful:
            return useful
    return ""


def _object_from_utterance(text: str) -> str:
    blob = " ".join(str(text or "").split())
    if not blob:
        return ""
    named = _THIS_IS_MY_RE.search(blob)
    if named:
        useful = _useful_object(named.group("object"))
        if useful:
            return useful
    for pattern in (_HOLDING_OBJECT_RE, _SEE_OBJECT_RE):
        match = pattern.search(blob)
        if match:
            useful = _useful_object(match.group("object"))
            if useful:
                return useful
    for sentence in re.split(r"(?<=[.!?])\s+", blob):
        lead = _NOUN_LEAD_RE.match(sentence.strip())
        if lead:
            useful = _useful_object(lead.group("object"))
            if useful:
                return useful
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
    if is_generic_label_scene(text):
        return ""
    return text[:700]


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
    seed = _usable_spoken(scene) or _scene_from_prompt(scene)
    if not seed and not is_nonvisual_keep_speech(scene):
        seed = scene
    usable = clean_visual_scene(
        seed,
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
    if topic and not _useful_object(topic):
        topic = ""
    label_obj = ""
    for name in names:
        useful = _useful_object(name)
        if useful:
            label_obj = useful
            break
    obj = (
        topic
        or _object_from_utterance(asked)
        or _object_from_utterance(usable)
        or (label_obj if usable else "")
    )
    obj = _useful_object(obj)
    if not usable:
        if printed:
            spoken = f"It reads {printed[:160].rstrip('.')}."
            if "remember" not in spoken.lower():
                spoken = spoken.rstrip(".") + ". I'll remember that."
            recall = (
                f"You asked me to remember {_named_object_phrase(obj)}."
                if obj
                else "You asked me to remember what you showed."
            )
            if printed.lower() not in recall.lower():
                recall = (
                    recall.rstrip(".")
                    + ". It reads "
                    + printed[:160].rstrip(".")
                    + "."
                )
            return {
                "object": obj,
                "colors": color_names[:4],
                "printed": printed,
                "scene": f"It reads {printed[:160].rstrip('.')}.",
                "usable": True,
                "recall": recall[:800],
                "spoken": spoken[:800],
            }
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
            "recall": recall[:800],
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
    description = usable[:700]
    if not description.endswith((".", "!", "?")):
        description += "."
    if obj:
        recall = f"You asked me to remember {_named_object_phrase(obj)}."
    else:
        recall = "You asked me to remember what you showed."
    if description.lower() not in recall.lower():
        recall = recall.rstrip(".") + ". " + description[0].upper() + description[1:]
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
        "recall": recall[:800],
        "spoken": spoken[:800],
    }


def _owner_keep_description(payload: dict[str, Any], text: str | None = None) -> str:
    """What to speak on later recall: the thing itself, not the keep header."""

    description = " ".join(str(payload.get("description") or "").split()).strip()
    if (
        description
        and "they said:" not in description.lower()
        and "asked evie" not in description.lower()
        and _keep_line_is_identity(description)
    ):
        if not description.endswith((".", "!", "?")):
            description += "."
        return description[:800]
    stored = " ".join(str(payload.get("recall") or text or "").split()).strip()
    rest = _KEEP_HEADER_RE.sub("", stored, count=1).strip()
    if (
        rest
        and rest.lower() != stored.lower()
        and "they said:" not in rest.lower()
        and _keep_line_is_identity(rest)
    ):
        if not rest.endswith((".", "!", "?")):
            rest += "."
        return rest[:800]
    return ""


def _spoken_named_object(payload: dict[str, Any], text: str | None = None) -> str:
    """Owner line from a named keep when the stored scene is only a class or color."""

    obj = _useful_object(str(payload.get("object") or payload.get("topic") or ""))
    color_names = [
        str(item).strip().lower()
        for item in (payload.get("colors") or [])
        if str(item).strip()
    ]
    asked = str(payload.get("keep_request") or "") or None
    if not obj:
        identity = extract_visual_identity(
            scene=text,
            ocr=str(payload.get("ocr_text") or payload.get("printed") or "") or None,
            labels=list(payload.get("labels") or []),
            colors=color_names,
            keep_request=asked,
        )
        obj = _useful_object(str(identity.get("object") or ""))
        if not color_names:
            color_names = [str(item) for item in (identity.get("colors") or []) if item]
    if not obj:
        return ""
    if _SHAPE_HEDGE_RE.search(obj):
        return ""
    named = _named_object_phrase(obj)
    if color_names and color_names[0] not in named.lower():
        named = f"a {color_names[0]} {obj}"
    line = f"That's {named}."[:800]
    if not _keep_line_is_identity(line):
        return ""
    return line


def recall_spoken_from_keep(
    text: str | None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Owner-facing recall line from a keep fact, including older request blobs."""

    data = payload or {}
    spoken = _owner_keep_description(data, text)
    if spoken:
        return spoken
    raw = " ".join(str(text or data.get("recall") or data.get("description") or "").split()).strip()
    if is_nonvisual_keep_speech(raw) or is_nonvisual_keep_speech(data.get("description")):
        return ""
    identity = extract_visual_identity(
        scene=raw,
        ocr=str(data.get("ocr_text") or data.get("printed") or "") or None,
        labels=list(data.get("labels") or []),
        colors=list(data.get("colors") or []),
        keep_request=str(data.get("keep_request") or "") or None,
    )
    scene = str(identity.get("scene") or "").strip()
    if _keep_line_is_identity(scene):
        if not scene.endswith((".", "!", "?")):
            scene += "."
        return scene[:800]
    named = _spoken_named_object(data, raw) or _spoken_named_object(
        {
            "object": identity.get("object"),
            "colors": identity.get("colors") or data.get("colors"),
            "keep_request": data.get("keep_request"),
        },
        raw,
    )
    if _keep_line_is_identity(named):
        return named
    stored = " ".join(str(data.get("recall") or "").split()).strip()
    rest = _KEEP_HEADER_RE.sub("", stored or raw, count=1).strip()
    rest = _KEEP_SHOWN_RE.sub("", rest).strip(" .")
    if _keep_line_is_identity(rest):
        if not rest.endswith((".", "!", "?")):
            rest += "."
        return rest[:800]
    return ""


def owner_memory_hit_text(text: str | None, payload: dict[str, Any] | None = None) -> str:
    """Live Mini must see the shown thing, not the keep-request header."""

    data = payload if isinstance(payload, dict) else {}
    raw = " ".join(str(text or data.get("text") or "").split()).strip()
    if not raw:
        raw = " ".join(
            str(data.get("description") or data.get("recall") or "").split()
        ).strip()
    if not raw:
        return ""
    blob = raw.lower()
    keepish = (
        "asked evie to remember" in blob
        or "you asked me to remember" in blob
        or str(data.get("kind") or "") == "visual_keep"
        or str(data.get("reason") or "") == "visual_keep"
        or bool(data.get("description"))
        or bool(data.get("keep_request"))
    )
    if not keepish:
        return raw[:800]
    spoken = recall_spoken_from_keep(raw, data)
    if spoken:
        return spoken[:800]
    return ""


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
        return spoken[:800]
    if frame_ok:
        obj = str(identity.get("object") or "").strip()
        color_names = [str(item) for item in (identity.get("colors") or []) if item]
        if obj:
            named = _named_object_phrase(obj)
            if color_names and color_names[0] not in named.lower():
                named = f"a {color_names[0]} {obj}"
            line = f"That's {named}. I'll remember that."
            return line[:800]
        scene = str(identity.get("scene") or "").strip()
        if scene:
            return scene[:800]
        if color_names:
            return f"I can see it — {', '.join(color_names[:3])}. I'll remember that."[:800]
    return spoken[:800] or "Hold it in the camera so I can see it."


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
    lowered = text.lower()
    if "look up" in lowered or "look this up" in lowered or "look it up" in lowered:
        return False
    return bool(CURRENT_VISUAL_RE.search(text))


def wants_held_object_look(message: str | None) -> bool:
    """True when they asked the camera to see what they are holding or showing now."""

    text = (message or "").strip()
    if not text or not wants_current_visual(text):
        return False
    return bool(HELD_OBJECT_RE.search(text))


def is_visual_recall_query(message: str | None) -> bool:
    """True when the owner is asking about something she already saw or saved."""

    from app.memory.room import looks_like_object_locate

    text = (message or "").strip()
    if not text:
        return False
    if wants_keep_visible(text):
        return False
    if is_keep_recall_query(text):
        return True
    if looks_like_object_locate(text):
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
    if is_camera_prompt_echo(text):
        return None
    if is_nonvisual_keep_speech(text):
        return None
    if (
        is_keep_ack_only(text)
        or is_memory_hedge_scene(text)
        or is_clarity_hedge(text)
        or is_empty_visual_scene(text)
        or is_generic_label_scene(text)
        or "hold it in the camera" in lowered
    ):
        return None
    return text[:700]


def _scene_from_prompt(spoken: str | None) -> str | None:
    """Pull Grounding labels/colors out of the live-model prompt."""

    text = str(spoken or "").strip()
    if not text:
        return None
    match = _GROUNDING_RE.search(text)
    if not match:
        return None
    facts = " ".join(match.group(1).split()).strip(" .;")
    if not facts or is_generic_label_scene(facts):
        return None
    return facts[:300] or None


def looks_like_visual_description(spoken: str | None) -> bool:
    """True when assistant speech is a scene, not a memory hedge or a prompt."""

    text = " ".join(str(spoken or "").split()).strip()
    if len(text) < 12:
        return False
    lowered = text.lower()
    if any(lowered.startswith(prefix) for prefix in _BOILERPLATE_PREFIXES):
        return False
    if is_camera_prompt_echo(text) or "hold it in the camera" in lowered:
        return False
    if is_nonvisual_keep_speech(text):
        return False
    if _HEDGE_OPENING.search(text) or is_memory_hedge_scene(text) or is_clarity_hedge(text):
        return False
    if is_generic_label_scene(text) or is_empty_visual_scene(text):
        return False
    if is_keep_ack_only(text):
        return False
    if any(cue in lowered for cue in _SCENE_CUES):
        return True
    if _NOUN_LEAD_RE.match(text) or _HOLDING_OBJECT_RE.search(text):
        return True
    if simple_tokens(text) & (_COLORS | _CLOTHING):
        return True
    return False


def is_keep_identity_speech(spoken: str | None) -> bool:
    """True when Mini named the shown thing, not an ack or a camera prompt."""

    text = " ".join(str(spoken or "").split()).strip()
    if len(text) < 12:
        return False
    if is_camera_prompt_echo(text) or is_keep_ack_only(text):
        return False
    if is_keep_recall_echo(text):
        return False
    if is_nonvisual_keep_speech(text):
        return False
    if _keep_is_thin({"description": text, "usable_scene": True}, text):
        return False
    if _PRINTED_RE.search(text) and _remainder_has_identity(text):
        return True
    return looks_like_visual_description(text)


def is_keep_injection_spoken(text: str | None) -> bool:
    """True when look spoken is not yet a reusable visual identity.

    Camera prompts, classifier labels, and vague class names must not be
    stored or spoken as the keep. Mini (or a JPEG reread) has to name the
    pixels first.
    """

    spoken = " ".join(str(text or "").split()).strip()
    if not spoken:
        return True
    if (
        is_camera_prompt_echo(spoken)
        or is_generic_label_scene(spoken)
        or is_clarity_hedge(spoken)
        or is_keep_ack_only(spoken)
        or is_empty_visual_scene(spoken)
    ):
        return True
    return not is_keep_identity_speech(spoken)


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

    from app.memory.room import LOCATE_SCAFFOLD, looks_like_object_locate

    tokens = simple_tokens(query or "")
    if "shirt" in tokens or "tshirt" in tokens:
        tokens.update({"shirt", "tshirt"})
    drop = set(_VISUAL_SCAFFOLD)
    if looks_like_object_locate(query):
        drop |= LOCATE_SCAFFOLD
    tokens = {token for token in tokens if len(token) >= 3 and token not in drop}
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
        if not raw or raw.lower() in seen or raw.lower() in _JUNK_OBJECT:
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
    if (
        named
        and named.lower() not in {"this", "that", "it"}
        and named.lower() not in _JUNK_OBJECT
    ):
        blob = " ".join(parts).lower()
        if named.lower() not in blob:
            parts.append("They asked Evie to remember the " + named)
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
    content = dict(event.content or {})
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
        "object": content.get("object") or "",
        "surface": content.get("surface") or "",
        "placement": content.get("placement") or "",
        "parts": {"lexical": 0.5, "speaker": 0.92, "recency": 1.0, "phrase": 1.0},
        "reason": reason,
    }


async def search_visual_observations(
    session: AsyncSession,
    query: str,
    *,
    k: int = 6,
    until=None,
    enrich: bool = True,
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
                .order_by(Memory.event_time.desc(), Memory.id.desc())
                .limit(80)
            )
        ).scalars().all()
    )
    keep_hits: list[dict[str, Any]] = []
    other_hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    recency_first = is_keep_recall_query(query) and keep_topic(query) in {
        "",
        "this",
        "that",
        "it",
        "you",
    }
    for row in keep_rows:
        payload = row.payload or {}
        kind = str(payload.get("kind") or "")
        if kind not in {"visual", "visual_keep", "object_placement"} and row.memory_type != "observation":
            continue
        if (
            kind not in {"visual", "visual_keep", "object_placement"}
            and str(row.text or "").lower().startswith("observed:")
        ):
            continue
        blob = " ".join(
            part
            for part in (
                row.text,
                str(payload.get("keep_request") or ""),
                str(payload.get("value") or ""),
                str(payload.get("topic") or ""),
                str(payload.get("object") or ""),
                str(payload.get("placement") or ""),
                str(payload.get("surface") or ""),
                " ".join(str(item) for item in (payload.get("objects") or []) if item),
                str(payload.get("recall") or ""),
                str(payload.get("description") or ""),
                str(payload.get("printed") or payload.get("ocr_text") or ""),
            )
            if part
        )
        recency_keep = recency_first and kind == "visual_keep"
        if is_memory_hedge_scene(row.text) and not recency_keep:
            continue
        if not recency_keep and not visual_observation_matches(query, blob):
            continue
        memory_id = str(row.id)
        if memory_id in seen:
            continue
        seen.add(memory_id)
        recall = recall_spoken_from_keep(row.text, payload)
        keepish = kind == "visual_keep" or (
            row.memory_type == "fact" and str(payload.get("kind") or "") == "visual_keep"
        )
        item = {
            "id": memory_id,
            "source": "memory",
            "when": _iso_time(row.event_time),
            "text": (recall if keepish else (recall or row.text))[:800],
            "kind": "memory",
            "memory_type": row.memory_type,
            "object": payload.get("object") or payload.get("topic") or "",
            "surface": payload.get("surface") or "",
            "placement": payload.get("placement") or "",
            "recall": recall,
            "description": payload.get("description") or None,
            "attachment_id": payload.get("attachment_id"),
            "ocr_text": payload.get("ocr_text") or payload.get("printed") or None,
            "printed": payload.get("printed") or None,
            "labels": list(payload.get("labels") or []),
            "colors": list(payload.get("colors") or []),
            "keep_request": payload.get("keep_request") or None,
            "score": 0.94 if kind == "visual_keep" else 0.9,
            "occurred_at": row.event_time,
            "parts": {"lexical": 0.6, "speaker": 0.95, "recency": 1.0, "phrase": 1.0},
            "reason": "visual_keep" if kind == "visual_keep" else "visual_observation",
        }
        item["_thin"] = _keep_is_thin(payload, row.text)
        if kind == "visual_keep":
            keep_hits.append(item)
        else:
            other_hits.append(item)
    if is_keep_recall_query(query):
        topic = keep_topic(query)
        recency_first = topic in {"", "this", "that", "it", "you"}
        if recency_first and keep_hits:
            # "What did I just ask you to remember?" is the newest look,
            # not last week's thicker keep.
            keep_hits = keep_hits[:1]
            other_hits = []
        else:
            usable_keeps = [item for item in keep_hits if not item.get("_thin")]
            if usable_keeps:
                keep_hits = usable_keeps
                other_hits = [
                    item
                    for item in other_hits
                    if _keep_hit_has_identity(item)
                ]
    needs_identity = bool(
        is_keep_recall_query(query)
        and keep_hits
        and not _keep_stored_identity_line(keep_hits[0])
    )
    needs_jpeg = bool(
        needs_identity and _attachment_uuid(keep_hits[0].get("attachment_id"))
    )
    for item in keep_hits:
        item.pop("_thin", None)
    for item in other_hits:
        item.pop("_thin", None)
    hits: list[dict[str, Any]] = keep_hits + other_hits
    if enrich and needs_identity:
        upgraded = None
        needle = _attachment_uuid(keep_hits[0].get("attachment_id"))
        if needs_jpeg and needle:
            await _await_keep_reread(needle, timeout=KEEP_RECALL_ENRICH_SECONDS)
            existing = await _current_keep_for_attachment(session, needle)
            if existing and not _keep_needs_enrichment(
                {
                    "attachment_id": needle,
                    "description": existing.get("description"),
                    "recall": existing.get("recall"),
                    "text": existing.get("_text"),
                    "object": existing.get("object"),
                    "printed": existing.get("printed") or existing.get("ocr_text"),
                    "ocr_text": existing.get("ocr_text"),
                }
            ):
                return await search_visual_observations(
                    session, query, k=k, until=until, enrich=False
                )
            upgraded = await _enrich_keep_from_attachment(
                session,
                keep_hits[0],
                timeout=KEEP_RECALL_ENRICH_SECONDS,
            )
        if not (upgraded and (upgraded.get("kept") or upgraded.get("skipped"))):
            since = None
            if recency_first:
                since = _keep_speech_since(
                    keep_hits[0].get("occurred_at") or keep_hits[0].get("when")
                )
            upgraded = await adopt_recent_spoken_keep(
                session, actor="owner", since=since
            )
        if upgraded and (upgraded.get("kept") or upgraded.get("skipped")):
            return await search_visual_observations(
                session, query, k=k, until=until, enrich=False
            )
    if (
        is_keep_recall_query(query)
        and keep_hits
        and not any(_keep_hit_has_identity(item) for item in keep_hits)
    ):
        topic = keep_topic(query)
        recency_first = topic in {"", "this", "that", "it", "you"}
        if recency_first:
            hits = keep_hits
        else:
            identity_others = [
                item for item in other_hits if _keep_hit_has_identity(item)
            ]
            if identity_others:
                hits = identity_others
    if recency_first:
        return hits[:1]
    if len(hits) >= max(1, k):
        return hits[: max(1, k)]
    for event in rows:
        text = _event_text(event)
        if is_memory_hedge_scene(text):
            continue
        keep_asked = str((event.content or {}).get("keep_request") or "")
        extra = " ".join(
            str(part)
            for part in (
                (event.content or {}).get("object"),
                (event.content or {}).get("placement"),
                (event.content or {}).get("surface"),
                " ".join(str(item) for item in ((event.content or {}).get("objects") or []) if item),
            )
            if part
        )
        haystack = f"{text} {keep_asked} {extra}".strip()
        if not visual_observation_matches(query, haystack):
            continue
        if is_keep_recall_query(query) and keep_hits and not _keep_hit_has_identity(
            {"text": text, "keep_request": keep_asked}
        ):
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
    require_empty: bool = True,
) -> str:
    """Keep utterance from a recent look.

    ``require_empty`` is for a later clear frame filling a blank memorize.
    Mini's first-look description must bind even when that look already stored
    classifier labels.
    """

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
        if not require_empty:
            return asked[:400]
        labels = [str(item) for item in (content.get("labels") or []) if item]
        ocr = str(content.get("ocr_text") or "").strip()
        scene = str(content.get("spoken") or "")
        if not labels and not ocr:
            if (
                not scene
                or is_empty_visual_scene(scene)
                or is_memory_hedge_scene(scene)
            ):
                return asked[:400]
    if device_id:
        return await _recent_keep_request(
            session, device_id=None, require_empty=require_empty
        )
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


def _keep_stored_identity_line(item: dict[str, Any] | None) -> str:
    """Reusable first-look line already stored on a keep fact, if any."""

    data = item or {}
    for raw in (
        data.get("description"),
        data.get("recall"),
        data.get("text"),
    ):
        blob = " ".join(str(raw or "").split()).strip()
        blob = _KEEP_HEADER_RE.sub("", blob, count=1).strip()
        blob = _KEEP_SHOWN_RE.sub("", blob).strip(" .")
        if _keep_line_is_identity(blob):
            return blob
    return ""


def _keep_needs_enrichment(item: dict[str, Any]) -> bool:
    """True when a stored keep cannot describe the shown thing on its own."""

    if not item.get("attachment_id"):
        return False
    if _keep_stored_identity_line(item):
        return False
    return True


def _attachment_uuid(value: Any) -> str:
    from uuid import UUID

    try:
        return str(UUID(str(value or "").strip()))
    except (TypeError, ValueError):
        return ""


def _keep_has_pixels(keep: dict[str, Any] | None) -> bool:
    """True when a keep row is tied to a stored frame, not later chat."""

    if not keep:
        return False
    if str(keep.get("attachment_id") or "").strip():
        return True
    if keep.get("image_ready"):
        return True
    try:
        return int(keep.get("encoded_bytes") or 0) > 0
    except (TypeError, ValueError):
        return False


_KEEP_REREAD_IN_FLIGHT: set[str] = set()


def schedule_keep_identity_reread(
    attachment_id: str,
    keep_request: str,
    *,
    actor: str = "owner",
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Reread the keep JPEG on a fresh session after the look commits.

    The sidecar keeps running after EV.app quits, so identity can land
    before reopen even if Mini never named the frame.
    """

    import sys

    if "pytest" in sys.modules:
        return
    needle = _attachment_uuid(attachment_id)
    asked = " ".join(str(keep_request or "").split()).strip()[:400]
    if not asked:
        asked = "memorize this"
    if not needle:
        return
    if needle in _KEEP_REREAD_IN_FLIGHT:
        return
    running = loop
    if running is None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return
    _KEEP_REREAD_IN_FLIGHT.add(needle)
    from app.ev.camera_runtime import log_camera

    log_camera(
        "keep.identity_reread_scheduled",
        request_id=needle,
    )

    async def _run() -> None:
        from uuid import UUID

        from app.db import SessionLocal
        from app.models import Attachment

        upgraded = None
        try:
            attached = False
            for delay in (0.0, 0.4, 1.2, 2.5, 5.0):
                if delay:
                    await asyncio.sleep(delay)
                async with SessionLocal() as session:
                    attached = await session.get(Attachment, UUID(needle)) is not None
                if attached:
                    break
            if not attached:
                logger.warning(
                    "keep identity reread missing attachment=%s", needle[:8]
                )
                return
            from app.ev.look import KEEP_LOOK_PROMPT, KEEP_REREAD_PROMPT

            for attempt, prompt in enumerate((KEEP_LOOK_PROMPT, KEEP_REREAD_PROMPT), start=1):
                async with SessionLocal() as session:
                    upgraded = await reread_keep_identity_from_attachment(
                        session,
                        needle,
                        asked,
                        actor=actor,
                        prompt=prompt,
                    )
                    await session.commit()
                if upgraded and upgraded.get("kept"):
                    break
                if attempt == 1:
                    await asyncio.sleep(2.0)
            log_camera(
                "keep.identity_reread_done",
                request_id=needle,
                extra={"kept": bool(upgraded and upgraded.get("kept"))},
            )
        except Exception:  # noqa: BLE001 - recall can still try later
            logger.warning("keep identity reread skipped", extra={"attachment": needle[:8]}, exc_info=True)
        finally:
            _KEEP_REREAD_IN_FLIGHT.discard(needle)

    running.create_task(_run())


async def _await_keep_reread(attachment_id: str, *, timeout: float | None = None) -> None:
    """Wait for a background JPEG reread so reopen recall can use identity."""

    needle = _attachment_uuid(attachment_id)
    if not needle or needle not in _KEEP_REREAD_IN_FLIGHT:
        return
    if timeout is None:
        from app.ev.look import keep_reread_timeout_seconds

        budget = keep_reread_timeout_seconds() + 20.0
    else:
        budget = timeout
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.05, budget)
    while needle in _KEEP_REREAD_IN_FLIGHT and loop.time() < deadline:
        await asyncio.sleep(0.25)


def _schedule_keep_reread_after_commit(
    session: AsyncSession,
    *,
    attachment_id: str,
    keep_request: str,
    actor: str,
) -> None:
    from sqlalchemy import event as sa_event

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    def _on_commit(_sync_session) -> None:
        def _kick() -> None:
            schedule_keep_identity_reread(
                attachment_id, keep_request, actor=actor, loop=loop
            )

        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(_kick)
        else:
            _kick()

    sa_event.listen(session.sync_session, "after_commit", _on_commit, once=True)


def keep_reread_look_target(
    body: dict[str, Any] | None,
    *,
    arguments: dict[str, Any] | None = None,
    transcript: str | None = None,
) -> tuple[str, str] | None:
    """Attachment + memorize phrase from a committed look tool result."""

    payload = body or {}
    args = arguments or {}
    prompt = str(args.get("prompt") or "").strip()
    if is_camera_prompt_echo(prompt):
        prompt = ""
    asked = ""
    for part in (
        str(payload.get("keep_request") or "").strip(),
        str(transcript or "").strip(),
        prompt,
    ):
        if part and wants_keep_visible(part):
            asked = part[:400]
            break
    needle = _attachment_uuid(payload.get("attachment_id"))
    if not needle:
        return None
    if not (asked or bool(payload.get("kept"))):
        return None
    return needle, asked or "memorize this"


def kick_keep_identity_reread_from_look(
    body: dict[str, Any] | None,
    *,
    arguments: dict[str, Any] | None = None,
    transcript: str | None = None,
    actor: str = "owner",
) -> None:
    """Start JPEG reread after the look session has committed."""

    target = keep_reread_look_target(
        body, arguments=arguments, transcript=transcript
    )
    if target is None:
        return
    needle, asked = target
    schedule_keep_identity_reread(needle, asked, actor=actor)


async def reread_keep_identity_from_attachment(
    session: AsyncSession,
    attachment_id: str,
    keep_request: str,
    *,
    actor: str = "owner",
    prompt: str | None = None,
) -> dict[str, Any] | None:
    """Upgrade a thin keep by reading pixels from the stored frame."""

    return await _enrich_keep_from_attachment(
        session,
        {"attachment_id": attachment_id, "keep_request": keep_request},
        actor=actor,
        prompt=prompt,
    )


async def _current_keep_for_attachment(
    session: AsyncSession,
    attachment_id: str,
) -> dict[str, Any] | None:
    """Latest current keep fact for this stored JPEG, if any."""

    from app.models import Memory

    needle = _attachment_uuid(attachment_id)
    if not needle:
        return None
    rows = list(
        (
            await session.execute(
                select(Memory)
                .where(
                    Memory.redacted.is_(False),
                    Memory.is_current.is_(True),
                    Memory.memory_type == "fact",
                )
                .order_by(Memory.event_time.desc())
            )
        ).scalars().all()
    )
    chosen: dict[str, Any] | None = None
    chosen_thin = True
    for memory in rows:
        payload = dict(memory.payload or {})
        if str(payload.get("kind") or "") != "visual_keep":
            continue
        if _attachment_uuid(payload.get("attachment_id")) != needle:
            continue
        payload["_text"] = memory.text
        thin = _keep_is_thin(payload, memory.text)
        if chosen is None or (chosen_thin and not thin):
            chosen = payload
            chosen_thin = thin
            if not thin:
                break
    return chosen


async def _newest_visual_keep(session: AsyncSession) -> dict[str, Any] | None:
    """Newest current keep-from-sight fact, even when the JPEG id was never stored."""

    from app.models import Memory

    rows = list(
        (
            await session.execute(
                select(Memory)
                .where(
                    Memory.redacted.is_(False),
                    Memory.is_current.is_(True),
                    Memory.memory_type == "fact",
                )
                .order_by(Memory.event_time.desc(), Memory.id.desc())
            )
        ).scalars().all()
    )
    for memory in rows:
        payload = dict(memory.payload or {})
        if str(payload.get("kind") or "") != "visual_keep":
            continue
        payload["_text"] = memory.text
        return payload
    return None


async def _enrich_keep_from_attachment(
    session: AsyncSession,
    item: dict[str, Any],
    *,
    actor: str = "owner",
    timeout: float | None = None,
    prompt: str | None = None,
) -> dict[str, Any] | None:
    """Re-read the stored keep JPEG so later recall has a real identity."""

    from uuid import UUID

    from app.ev.look import KEEP_LOOK_PROMPT, keep_reread_timeout_seconds
    from app.ev.vision import analyze_attachment

    try:
        attachment_id = UUID(str(item.get("attachment_id") or "").strip())
    except (TypeError, ValueError):
        return None
    asked = str(item.get("keep_request") or "memorize this").strip()[:400]
    if not wants_keep_visible(asked):
        asked = "memorize this"
    existing = await _current_keep_for_attachment(session, str(attachment_id))
    if existing and not _keep_needs_enrichment(
        {
            "attachment_id": str(attachment_id),
            "description": existing.get("description"),
            "recall": existing.get("recall"),
            "text": existing.get("_text"),
        }
    ):
        logger.warning(
            "keep identity reread skipped thick identity already stored attachment=%s",
            str(attachment_id)[:8],
        )
        return {"kept": True, "skipped": True}
    limit = keep_reread_timeout_seconds() if timeout is None else timeout
    try:
        perception = await asyncio.wait_for(
            analyze_attachment(
                session,
                attachment_id,
                actor=actor,
                permission=True,
                allow_raw=keep_perception_allow_raw(asked),
                prompt=prompt or KEEP_LOOK_PROMPT,
            ),
            timeout=limit,
        )
    except Exception:  # noqa: BLE001 - recall must still speak whatever we have
        logger.info("keep attachment reread skipped", exc_info=True)
        return None
    payload = dict(getattr(perception, "payload", None) or {})
    labels = [
        str(entry.get("label") or entry).strip()
        for entry in (payload.get("labels") or [])
        if str(entry.get("label") or entry).strip()
    ]
    colors = [
        str(entry).strip()
        for entry in (payload.get("colors") or [])
        if str(entry).strip()
    ]
    summary = " ".join(str(payload.get("summary") or "").split()).strip()
    ocr = str(payload.get("ocr_text") or "").strip() or None
    spoken = summary[:800]
    if not is_keep_identity_speech(spoken) and ocr:
        printed = f"It reads {ocr[:160].rstrip('.')}."
        if is_keep_identity_speech(printed) or (
            _remainder_has_identity(ocr) and not is_generic_label_scene(ocr)
        ):
            spoken = printed
    if spoken and "remember" not in spoken.lower() and is_keep_identity_speech(spoken):
        spoken = spoken.rstrip(".") + ". I'll remember that."
    logger.warning(
        "keep identity reread summary_chars=%s spoken_chars=%s generic=%s",
        len(summary),
        len(spoken or ""),
        is_generic_label_scene(spoken),
    )
    if (
        is_generic_label_scene(spoken)
        or is_clarity_hedge(spoken)
        or is_empty_visual_scene(spoken)
        or "hold it in the camera" in spoken.lower()
        or not is_keep_identity_speech(spoken)
    ):
        return None
    existing = await _current_keep_for_attachment(session, str(attachment_id))
    if existing and not _keep_needs_enrichment(
        {
            "attachment_id": str(attachment_id),
            "description": existing.get("description"),
            "recall": existing.get("recall"),
            "text": existing.get("_text"),
            "object": existing.get("object"),
            "printed": existing.get("printed") or existing.get("ocr_text"),
            "ocr_text": existing.get("ocr_text"),
        }
    ):
        logger.warning(
            "keep identity reread skipped first look already stored attachment=%s",
            str(attachment_id)[:8],
        )
        return {"kept": True, "skipped": True}
    return await persist_visual_observation(
        session,
        {
            "ok": True,
            "spoken": spoken,
            "summary": spoken,
            "labels": labels,
            "colors": colors,
            "ocr_text": ocr,
            "attachment_id": str(attachment_id),
            "keep_request": asked,
            "media_kind": "frame",
            "image_ready": True,
            "encoded_bytes": 1,
            "identity_source": "reread",
        },
        actor=actor,
        adopt_spoken=False,
    )


def visual_keep_semantic_key(payload: dict[str, Any] | None) -> tuple | None:
    """Version key for a keep-from-sight fact.

    Camera frames must not share the generic ``this / shown`` fact key, or a
    later label stub overwrites the first-look identity.
    """

    data = payload or {}
    if str(data.get("kind") or "") != "visual_keep":
        return None
    attachment = str(data.get("attachment_id") or "").strip()
    if attachment:
        return ("visual_keep", attachment)
    subject = normalize_text(str(data.get("subject") or data.get("topic") or "")[:80]) or "this"
    return ("visual_keep", subject, "shown")


def _keep_identity_rank(payload: dict[str, Any], text: str | None = None) -> tuple[int, int, int]:
    """Richer first-look descriptions outrank later shape or class stubs."""

    if _keep_is_thin(payload, text):
        return (0, 0, 0)
    blob = " ".join(
        str(part)
        for part in (
            payload.get("description"),
            payload.get("recall"),
            text,
        )
        if part
    ).strip()
    tokens = [
        token
        for token in re.findall(r"[a-z0-9']+", blob.lower())
        if (
            token.replace("'", "") not in _JUNK_OBJECT
            and token.replace("'", "") not in _LOCATION_NOISE
            and token.replace("'", "") not in _IDENTITY_SKIP
            and len(token) >= 3
        )
    ]
    return (1, len(blob), len(tokens))


def _keep_source_rank(payload: dict[str, Any], text: str | None = None) -> int:
    """Live Mini first-look outranks a later JPEG reread of the same frame."""

    if _keep_is_thin(payload, text):
        return 0
    src = str(payload.get("identity_source") or "").strip().lower()
    if src == "reread":
        return 2
    return 3


def retain_visual_keep_identity(
    prev_payload: dict[str, Any] | None,
    prev_text: str | None,
    cand_payload: dict[str, Any] | None,
    cand_text: str | None,
) -> bool:
    """Keep the richer reusable identity. First look wins ties."""

    prev = prev_payload or {}
    cand = cand_payload or {}
    if str(prev.get("kind") or "") != "visual_keep":
        return False
    if str(cand.get("kind") or "") != "visual_keep":
        return False
    prev_src = _keep_source_rank(prev, prev_text)
    cand_src = _keep_source_rank(cand, cand_text)
    if prev_src != cand_src:
        return prev_src > cand_src
    return _keep_identity_rank(prev, prev_text) >= _keep_identity_rank(cand, cand_text)


def _keep_hit_has_identity(item: dict[str, Any] | None) -> bool:
    data = item or {}
    blob = " ".join(
        str(part)
        for part in (
            data.get("description"),
            data.get("recall"),
            data.get("text"),
            data.get("object"),
        )
        if part
    ).strip()
    if not blob:
        return False
    if is_generic_label_scene(blob) or is_empty_visual_scene(blob) or is_clarity_hedge(blob):
        return False
    if is_nonvisual_keep_speech(blob):
        return False
    return _remainder_has_identity(blob)


def _keep_has_distinctive_detail(text: str | None, printed: str | None = None) -> bool:
    """True when wording names more than a vague class (phone, bottle, box)."""

    ink = " ".join(str(printed or "").split()).strip()
    if (
        ink
        and not is_generic_label_scene(ink)
        and not is_empty_visual_scene(ink)
        and _remainder_has_identity(ink)
    ):
        return True
    blob = " ".join(str(text or "").split()).strip().lower()
    if _PRINTED_RE.search(blob):
        return True
    tokens = {
        token.replace("'", "")
        for token in re.findall(r"[a-z0-9']+", blob)
        if len(token) >= 3
    }
    tokens -= (
        _VAGUE_CLASS
        | _COLORS
        | _IDENTITY_SKIP
        | _JUNK_OBJECT
        | _LOCATION_NOISE
        | _OBJECT_STOP
        | {
            "screen",
            "screens",
            "display",
            "displays",
            "button",
            "buttons",
            "keypad",
            "body",
            "case",
            "cover",
        }
    )
    return bool(tokens)


def _keep_is_thin(payload: dict[str, Any], text: str | None = None) -> bool:
    """True when a keep fact is a label stub, not a reusable visual identity."""

    return not bool(
        _keep_stored_identity_line(
            {
                "description": payload.get("description"),
                "recall": payload.get("recall"),
                "text": text,
            }
        )
    )


def _keep_line_is_identity(text: str | None) -> bool:
    """True when a line can be spoken later as the shown thing itself."""

    blob = " ".join(str(text or "").split()).strip()
    if not blob:
        return False
    rest = _KEEP_HEADER_RE.sub("", blob, count=1).strip()
    rest = re.sub(
        r"^owner asked evie to remember[^.!?\n]*[.!]?\s*",
        "",
        rest,
        flags=re.IGNORECASE,
    )
    rest = _KEEP_SHOWN_RE.sub("", rest).strip(" .")
    if rest:
        blob = rest
    elif blob.lower().startswith(("you asked me to remember", "owner asked evie")):
        return False
    lowered = blob.lower()
    if "they said:" in lowered:
        return False
    if blob.endswith("?") or is_keep_recall_echo(blob):
        return False
    if is_nonvisual_keep_speech(blob):
        return False
    if (
        is_generic_label_scene(blob)
        or is_empty_visual_scene(blob)
        or is_clarity_hedge(blob)
        or is_camera_prompt_echo(blob)
        or is_keep_ack_only(blob)
    ):
        return False
    if re.match(r"^it reads\s+\S+(?:\s+\S+){0,3}\.?$", blob, re.IGNORECASE):
        return False
    if not _remainder_has_identity(blob):
        return False
    if not (
        looks_like_visual_description(blob)
        or (_PRINTED_RE.search(blob) and _remainder_has_identity(blob))
    ):
        return False
    return _keep_has_distinctive_detail(blob)


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
        if not _keep_is_thin(payload, memory.text):
            continue
        memory.is_current = False
        memory.superseded_by_id = successor
        memory.valid_until = utcnow()
        extra = dict(memory.extra or {})
        extra["superseded_reason"] = "visual_identity"
        memory.extra = extra
        flag_modified(memory, "extra")
        flag_modified(memory, "payload")


async def _thick_keep_for_attachment(
    session: AsyncSession,
    attachment_id: str,
) -> bool:
    """True when this frame already has a reusable keep identity."""

    needle = str(attachment_id or "").strip()
    if not needle:
        return False
    return await _thick_keep_matching(
        session,
        {"kind": "visual_keep", "attachment_id": needle, "subject": "this"},
    )


async def _foreign_keep_attachment_exists(
    session: AsyncSession,
    payload: dict[str, Any],
) -> bool:
    """True when another keep JPEG is already current in the spoken-scene window."""

    from app.models import Memory

    this = _attachment_uuid(payload.get("attachment_id"))
    if not this:
        return False
    cutoff = utcnow() - SPOKEN_SCENE_WINDOW
    rows = list(
        (
            await session.execute(
                select(Memory).where(
                    Memory.redacted.is_(False),
                    Memory.is_current.is_(True),
                    Memory.memory_type == "fact",
                    Memory.event_time >= cutoff,
                )
            )
        ).scalars().all()
    )
    for memory in rows:
        existing = dict(memory.payload or {})
        if str(existing.get("kind") or "") != "visual_keep":
            continue
        other = _attachment_uuid(existing.get("attachment_id"))
        if other and other != this:
            return True
    return False


async def _thick_keep_matching(
    session: AsyncSession,
    payload: dict[str, Any],
) -> bool:
    """True when a current keep already stores identity for this version key."""

    from app.models import Memory

    key = visual_keep_semantic_key(payload)
    if key is None:
        return False
    cutoff = utcnow() - SPOKEN_SCENE_WINDOW
    rows = list(
        (
            await session.execute(
                select(Memory).where(
                    Memory.redacted.is_(False),
                    Memory.is_current.is_(True),
                    Memory.memory_type == "fact",
                    Memory.event_time >= cutoff,
                )
            )
        ).scalars().all()
    )
    for memory in rows:
        existing = dict(memory.payload or {})
        if visual_keep_semantic_key(existing) != key:
            continue
        if not _keep_is_thin(existing, memory.text):
            return True
    return False


def _keep_speech_since(stamp: Any) -> datetime:
    """Same-turn Mini speech after this keep, not last week's identity."""

    if isinstance(stamp, datetime):
        return _as_comparable_time(stamp)
    raw = str(stamp or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            return _as_comparable_time(parsed)
    return _as_comparable_time(utcnow() - SPOKEN_SCENE_WINDOW)


def _as_comparable_time(stamp: datetime) -> datetime:
    if stamp.tzinfo is None:
        from datetime import UTC

        return stamp.replace(tzinfo=UTC)
    return stamp


async def adopt_recent_spoken_keep(
    session: AsyncSession,
    *,
    actor: str = "owner",
    device_id: str | None = None,
    since: datetime | None = None,
) -> dict[str, Any] | None:
    """Store the first-look description Mini already spoke onto this keep."""

    cutoff = since if since is not None else utcnow() - SPOKEN_SCENE_WINDOW
    stmt = (
        select(Event)
        .where(
            Event.event_type == "message.assistant",
            Event.tombstoned_at.is_(None),
            Event.occurred_at >= cutoff,
        )
        .order_by(Event.occurred_at.desc())
        .limit(8)
    )
    if device_id:
        stmt = stmt.where((Event.device_id == device_id) | (Event.device_id.is_(None)))
    rows = list((await session.execute(stmt)).scalars().all())
    for event in rows:
        spoken = " ".join(
            str(
                (event.content or {}).get("text")
                or getattr(event, "text", None)
                or ""
            ).split()
        ).strip()
        if not spoken:
            continue
        if is_camera_prompt_echo(spoken) or is_clarity_hedge(spoken):
            continue
        if is_generic_label_scene(spoken) or is_empty_visual_scene(spoken):
            continue
        if is_nonvisual_keep_speech(spoken):
            continue
        if not looks_like_visual_description(spoken) and not is_keep_identity_speech(spoken):
            continue
        written = await remember_spoken_scene(
            session, spoken, actor=actor, device_id=device_id
        )
        if written and written.get("kept"):
            return written
    return None


async def persist_visual_observation(
    session: AsyncSession,
    result: dict[str, Any],
    *,
    actor: str = "owner",
    device_id: str | None = None,
    adopt_spoken: bool = True,
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
        or is_generic_label_scene(scene_for_keep)
        or is_nonvisual_keep_speech(scene_for_keep)
        or is_nonvisual_keep_speech(raw_spoken)
    )
    if is_nonvisual_keep_speech(raw_spoken) or is_nonvisual_keep_speech(spoken):
        spoken = None
        scene_for_keep = ""
        empty_scene = True
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
    if not identity.get("usable"):
        first = (keep_named.split() or [""])[0].lower()
        if first in _VAGUE_CLASS or first in _JUNK_OBJECT:
            keep_named = ""
    from app.memory.room import extract_placement, placement_fact_text

    placement = extract_placement(
        scene=scene_for_keep if usable_scene else spoken,
        labels=labels,
        named=keep_named or None,
    )
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
    place_phrase = str(placement.get("phrase") or "").strip()
    if place_phrase and place_phrase.lower() not in text.lower():
        text = f"{text.rstrip('.')} Last seen: {place_phrase}."[:800]
    seen_object = keep_named or (placement["objects"][0] if placement.get("objects") else None)
    payload = {
        "kind": "visual",
        "labels": labels,
        "colors": colors,
        "people": people_n or None,
        "saved_path": saved_path,
        "media_kind": media_kind,
        "attachment_id": result.get("attachment_id"),
        "request_id": result.get("request_id"),
        "object": seen_object,
        "objects": list(placement.get("objects") or []),
        "surface": placement.get("surface"),
        "placement": place_phrase or None,
        "topic": keep_named
        if keep_named
        else (
            seen_object
            or next(
                (
                    name
                    for name in labels
                    if str(name).strip().lower() not in _JUNK_OBJECT
                ),
                None,
            )
            or ("scene" if colors else "camera")
        ),
        "duration_s": result.get("duration_s"),
        "spoken": spoken,
        "description": identity.get("scene") or spoken,
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
                    "object": seen_object,
                    "objects": list(placement.get("objects") or []),
                    "surface": placement.get("surface"),
                    "placement": place_phrase or None,
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
        place_text = placement_fact_text(placement)
        if place_text and placement.get("objects"):
            topic = str(placement["objects"][0])
            candidates.append(
                MemoryCandidate(
                    memory_type="fact",
                    text=place_text,
                    payload={
                        "subject": topic,
                        "property": "last_seen",
                        "value": place_text,
                        "kind": "object_placement",
                        "topic": topic,
                        "object": topic,
                        "surface": placement.get("surface"),
                        "placement": place_phrase or None,
                        "labels": labels,
                        "recall": place_text,
                    },
                    importance=0.88,
                    confidence=0.8 if placement.get("surface") else 0.7,
                    source_type="derived",
                    privacy_level="normal",
                    event_time=utcnow(),
                    entities=entities,
                )
            )
        if wants_keep_visible(keep_user):
            keep_text = keep_sight_text(
                user_text=keep_user,
                scene=(spoken or raw_spoken) if usable_scene and not empty_scene else None,
                ocr=ocr if (usable_scene or frame_ok) else None,
                labels=labels if (usable_scene or frame_ok) else None,
                colors=colors if (usable_scene or frame_ok) else None,
            )
            topic = keep_named or keep_topic(keep_user) or "this"
            owner_scene = str(identity.get("scene") or "").strip()
            if not _keep_line_is_identity(owner_scene):
                for candidate in (spoken, raw_spoken, scene_for_keep):
                    blob = " ".join(str(candidate or "").split()).strip()
                    if _keep_line_is_identity(blob):
                        owner_scene = blob
                        break
            if _keep_line_is_identity(owner_scene):
                owner_recall = owner_scene
                if not owner_recall.endswith((".", "!", "?")):
                    owner_recall += "."
                owner_description = owner_scene
            else:
                owner_recall = ""
                owner_description = ""
            keep_payload = {
                "subject": topic,
                "property": "shown",
                "value": keep_text,
                "kind": "visual_keep",
                "topic": topic,
                "object": keep_named or None,
                "colors": list(identity.get("colors") or []) if (usable_scene or frame_ok) else [],
                "printed": identity.get("printed") if usable_scene else None,
                "description": owner_description or None,
                "recall": owner_recall,
                "usable_scene": bool(identity.get("usable"))
                or bool(owner_description),
                "labels": labels if (usable_scene or frame_ok) else [],
                "ocr_text": ocr if (usable_scene or frame_ok) else None,
                "keep_request": keep_user[:400],
                "attachment_id": result.get("attachment_id"),
            }
            if result.get("image_ready"):
                keep_payload["image_ready"] = True
            try:
                encoded = int(result.get("encoded_bytes") or 0)
            except (TypeError, ValueError):
                encoded = 0
            if encoded > 0:
                keep_payload["encoded_bytes"] = encoded
            source = str(result.get("identity_source") or "").strip().lower()
            if source in {"live", "reread"}:
                keep_payload["identity_source"] = source
            skip_stub = False
            if _keep_is_thin(keep_payload, keep_text):
                keep_aid = _attachment_uuid(result.get("attachment_id"))
                if keep_aid:
                    # Same JPEG already has a row. A brand-new frame uses a
                    # unique attachment key and must still write.
                    skip_stub = (
                        await _current_keep_for_attachment(session, keep_aid)
                    ) is not None
                else:
                    newest = await _newest_visual_keep(session)
                    if (
                        newest
                        and _keep_has_pixels(newest)
                        and not _keep_is_thin(newest, newest.get("_text"))
                    ):
                        # Do not recency-pin a no-camera stub over a named look.
                        skip_stub = True
                    else:
                        skip_stub = await _thick_keep_matching(session, keep_payload)
            if not skip_stub:
                candidates.append(
                    MemoryCandidate(
                        memory_type="fact",
                        text=keep_text,
                        payload=keep_payload,
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
                keep_ids = [
                    row.memory_id
                    for row in written
                    if row.memory_type == "fact"
                ]
                if keep_ids and (usable_scene or (frame_ok and keep_named)):
                    await _supersede_placeholder_visual_keeps(
                        session,
                        keep_memory_id=keep_ids[-1],
                    )
                if adopt_spoken and (
                    not keep_ids or _keep_is_thin(keep_payload, keep_text)
                ):
                    if await _foreign_keep_attachment_exists(session, keep_payload):
                        adopted = None
                    else:
                        adopted = await adopt_recent_spoken_keep(
                            session, actor=actor, device_id=device_id
                        )
                else:
                    adopted = None
                still_thin = (
                    not skip_stub
                    and _keep_is_thin(keep_payload, keep_text)
                    and not adopted
                )
                attachment_uuid = _attachment_uuid(result.get("attachment_id"))
                if still_thin and attachment_uuid:
                    _schedule_keep_reread_after_commit(
                        session,
                        attachment_id=attachment_uuid,
                        keep_request=keep_user,
                        actor=actor,
                    )
        return {
            "event_id": str(event.id),
            "memory_id": written[0].memory_id if written else None,
            "kept": bool(result.get("kept")),
        }
    except Exception:  # noqa: BLE001 - recall must never block seeing
        logger.warning("visual observation persist skipped", extra={"device_id": device_id}, exc_info=True)
        return None


async def _latest_keep_attachment_id(session: AsyncSession) -> str | None:
    """Newest keep JPEG id in the spoken-scene window, any device."""

    from app.models import Memory

    cutoff = utcnow() - SPOKEN_SCENE_WINDOW
    rows = list(
        (
            await session.execute(
                select(Memory)
                .where(
                    Memory.redacted.is_(False),
                    Memory.is_current.is_(True),
                    Memory.memory_type == "fact",
                    Memory.event_time >= cutoff,
                )
                .order_by(Memory.event_time.desc(), Memory.id.desc())
            )
        ).scalars().all()
    )
    for memory in rows:
        payload = dict(memory.payload or {})
        if str(payload.get("kind") or "") != "visual_keep":
            continue
        needle = _attachment_uuid(payload.get("attachment_id"))
        if needle:
            return needle
    return None


async def recent_keep_attachment_id(
    session: AsyncSession,
    *,
    max_age_s: float = KEEP_MINI_RELOAD_SECONDS,
) -> str | None:
    """Newest keep JPEG stored just now — Mini must name these pixels, not a new capture."""

    from app.models import Memory

    newest = None
    cutoff = utcnow() - SPOKEN_SCENE_WINDOW
    rows = list(
        (
            await session.execute(
                select(Memory)
                .where(
                    Memory.redacted.is_(False),
                    Memory.is_current.is_(True),
                    Memory.memory_type == "fact",
                    Memory.event_time >= cutoff,
                )
                .order_by(Memory.event_time.desc(), Memory.id.desc())
            )
        ).scalars().all()
    )
    for memory in rows:
        payload = dict(memory.payload or {})
        if str(payload.get("kind") or "") != "visual_keep":
            continue
        needle = _attachment_uuid(payload.get("attachment_id"))
        if not needle:
            continue
        newest = memory
        break
    if newest is None:
        return None
    stamp = newest.event_time
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        age = (utcnow().replace(tzinfo=None) - stamp).total_seconds()
    else:
        age = (utcnow() - stamp).total_seconds()
    if age > max(1.0, float(max_age_s)):
        return None
    return _attachment_uuid((newest.payload or {}).get("attachment_id"))


def _prefer_keep_event(rows: list[Any]) -> Any | None:
    for row in rows:
        asked = " ".join(str((row.content or {}).get("keep_request") or "").split()).strip()
        if wants_keep_visible(asked):
            return row
    return rows[0] if rows else None


async def _recent_visual_events(
    session: AsyncSession,
    *,
    device_id: str | None = None,
) -> list[Any]:
    stmt = (
        select(Event)
        .where(
            Event.event_type == VISUAL_EVENT_TYPE,
            Event.tombstoned_at.is_(None),
            Event.occurred_at >= utcnow() - SPOKEN_SCENE_WINDOW,
        )
        .order_by(Event.occurred_at.desc())
        .limit(24)
    )
    if device_id:
        stmt = stmt.where((Event.device_id == device_id) | (Event.device_id.is_(None)))
    return list((await session.execute(stmt)).scalars().all())


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
    if not scene or is_clarity_hedge(scene) or is_generic_label_scene(scene):
        return None
    if not is_keep_identity_speech(scene):
        return None
    newest_keep = await _newest_visual_keep(session)
    if newest_keep is None or not _keep_has_pixels(newest_keep):
        # Later Mini talk (grocery, files, weather) must not become identity
        # for a memorize that never stored pixels.
        return None
    rows = await _recent_visual_events(session, device_id=device_id)
    event = _prefer_keep_event(rows)
    if event is None or not wants_keep_visible(
        str((event.content or {}).get("keep_request") or "")
    ):
        # Keep-from-sight is owner memory. A Mac look row must bind even
        # when Mini persist uses a different device id.
        rows = await _recent_visual_events(session, device_id=None)
        event = _prefer_keep_event(rows)
    keep_user = ""
    content: dict[str, Any] = dict(event.content or {}) if event is not None else {}
    if event is not None:
        existing = _event_text(event)
        if scene.lower() in existing.lower() and (
            newest_keep is None
            or _keep_stored_identity_line(
                {
                    "description": newest_keep.get("description"),
                    "recall": newest_keep.get("recall"),
                    "text": newest_keep.get("_text"),
                }
            )
        ):
            return None
        keep_user = str(content.get("keep_request") or "").strip()
    if not wants_keep_visible(keep_user):
        keep_user = await _recent_keep_request(
            session, device_id=device_id, require_empty=False
        )
    if (
        not wants_keep_visible(keep_user)
        and newest_keep is not None
        and not _keep_stored_identity_line(
            {
                "description": newest_keep.get("description"),
                "recall": newest_keep.get("recall"),
                "text": newest_keep.get("_text"),
            }
        )
    ):
        keep_user = str(newest_keep.get("keep_request") or "").strip() or "memorize this"
    if not wants_keep_visible(keep_user) and event is None:
        return None
    labels = [str(item) for item in (content.get("labels") or []) if item]
    colors = [str(item) for item in (content.get("colors") or []) if item]
    people = content.get("people")
    try:
        people_n = int(people) if people is not None else 0
    except (TypeError, ValueError):
        people_n = 0
    newest = await _latest_keep_attachment_id(session)
    if newest:
        matched = None
        for row in rows:
            if _attachment_uuid((row.content or {}).get("attachment_id")) == newest:
                matched = row
                break
        if matched is None:
            wider = await _recent_visual_events(session, device_id=None)
            for row in wider:
                if _attachment_uuid((row.content or {}).get("attachment_id")) == newest:
                    matched = row
                    break
        if matched is not None:
            event = matched
            content = dict(event.content or {})
            if not keep_user:
                keep_user = str(content.get("keep_request") or "").strip()
    attachment_id = newest or content.get("attachment_id")
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
        "attachment_id": attachment_id,
        "keep_request": (keep_user or content.get("keep_request") or "")[:400] or None,
        "image_ready": bool(attachment_id),
        "encoded_bytes": 1 if attachment_id else 0,
        "identity_source": "live",
    }
    return await persist_visual_observation(
        session,
        result,
        actor=actor,
        device_id=device_id or (event.device_id if event is not None else None),
        adopt_spoken=False,
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
