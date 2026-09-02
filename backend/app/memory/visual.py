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
    r"what did i ask you to (?:remember|memorise|memorize|keep)|"
    r"what i asked you to (?:remember|memorise|memorize|keep)|"
    r"what were you supposed to remember|"
    r"what did i (?:just )?(?:show|tell) you to remember|"
    r"what was i showing|"
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
    r"\bkeep this in (?:mind|memory)\b|"
    r"\b(?:don't|do not) forget (?:this|that|it)\b"
    r")",
    re.IGNORECASE,
)

KEEP_RECALL_RE = re.compile(
    r"("
    r"\bdid you (?:already |ever )?(?:memorise|memorize|remember)\b|"
    r"\bhave you (?:already |ever )?(?:memorised|memorized|remembered)\b|"
    r"\bdid you (?:already )?keep (?:the|this|that|it|what)\b|"
    r"\bdo you remember (?:the|this|that) (?!i\b|i['’]m\b|we\b)|"
    r"\bdo you remember what i (?:asked|showed|told you to remember)\b|"
    r"\bdid you remember\b|"
    r"\b(?:were you able to|did you get to) (?:memorise|memorize|remember)\b"
    r")",
    re.IGNORECASE,
)

_KEEP_OBJECT_RE = re.compile(
    r"\b(?:memorise|memorize|remember|keep|(?:don't|do not) forget)\s+"
    r"(?:(?:this|that|the|a|an|it)\s+)?"
    r"(?P<object>.*?)$",
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


def keep_topic(message: str | None) -> str:
    """Object words from the keep request, not a fixed list of things."""

    text = (message or "").strip()
    match = _KEEP_OBJECT_RE.search(text)
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
        if token not in _VISUAL_SCAFFOLD and len(token) >= 2
    ]
    if words:
        return " ".join(words[:6])
    return "this"


def keep_sight_text(
    *,
    user_text: str,
    scene: str | None = None,
    ocr: str | None = None,
    labels: list[str] | None = None,
) -> str:
    """Durable sentence for a show-and-remember request."""

    parts = ["Owner asked Evie to remember what they showed"]
    asked = " ".join(str(user_text or "").split())
    if asked:
        parts.append("They said: " + asked[:200])
    printed = " ".join(str(ocr or "").split())
    if printed:
        parts.append("Printed text: " + printed[:200])
    seen = _usable_spoken(scene) or _scene_from_prompt(scene)
    if seen:
        parts.append(seen)
    else:
        names = [str(item).strip() for item in (labels or []) if str(item).strip()]
        if names:
            parts.append("Visible: " + ", ".join(names[:6]))
    text = ". ".join(part.rstrip(".") for part in parts if part).strip()
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


def _usable_spoken(spoken: str | None) -> str | None:
    text = " ".join(str(spoken or "").split()).strip()
    if not text:
        return None
    lowered = text.lower()
    if any(lowered.startswith(prefix) for prefix in _BOILERPLATE_PREFIXES):
        return None
    if _HEDGE_OPENING.search(text):
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
    if _HEDGE_OPENING.search(text):
        return False
    if any(cue in lowered for cue in _SCENE_CUES):
        return True
    if simple_tokens(text) & (_COLORS | _CLOTHING):
        return True
    return len(text.split()) >= 8


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
    wanted = visual_content_tokens(query)
    have = visual_index_tokens(blob)
    if wanted and (_stems(wanted) & _stems(have)):
        return True
    if wanted:
        return False
    if not is_visual_recall_query(query):
        return False
    lowered = blob.lower()
    if simple_tokens(query) & _CLOTHING:
        return bool(have & (_CLOTHING | _COLORS | {"person", "people"})) or "i looked" in lowered
    return "i looked" in lowered or "i recorded" in lowered or "i took a photo" in lowered or "i watched" in lowered


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


def _event_text(event: Event) -> str:
    return str((event.content or {}).get("text") or "").strip()


def _observation_row(event: Event, *, score: float, reason: str) -> dict[str, Any]:
    text = _event_text(event)
    when = event.occurred_at.isoformat() if event.occurred_at else None
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
    hits: list[dict[str, Any]] = []
    for event in rows:
        text = _event_text(event)
        if not visual_observation_matches(query, text):
            continue
        hits.append(_observation_row(event, score=0.86, reason="visual_content"))
        if len(hits) >= max(1, k):
            break
    return hits


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
    keep_named = keep_topic(keep_user) if wants_keep_visible(keep_user) else ""
    text = visual_observation_text(
        labels=labels,
        colors=colors,
        people=people_n,
        media_kind=media_kind,
        saved_path=saved_path,
        ocr_text=ocr,
        duration_s=result.get("duration_s"),
        visual_facts=facts,
        spoken=raw_spoken,
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
        "topic": keep_named
        if keep_named and keep_named not in {"this", "that", "it"}
        else ((labels[0] if labels else None) or ("scene" if colors else "camera")),
        "duration_s": result.get("duration_s"),
        "spoken": spoken,
        "keep_request": keep_user or None,
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
                scene=spoken or raw_spoken,
                ocr=ocr,
                labels=labels,
            )
            topic = keep_topic(keep_user)
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
                        "labels": labels,
                        "ocr_text": ocr,
                    },
                    importance=0.96,
                    confidence=0.9 if ocr or spoken else 0.75,
                    source_type="explicit",
                    privacy_level="normal",
                    event_time=utcnow(),
                    entities=entities,
                )
            )
        written = await writer.write_all(event, candidates)
        if written:
            result["remembered"] = True
            result["memory_id"] = written[0].memory_id
            result["memory_text"] = written[-1].text
            if wants_keep_visible(keep_user):
                await _pin_memory_ids(session, [row.memory_id for row in written])
                result["kept"] = True
        return {"event_id": str(event.id), "memory_id": written[0].memory_id if written else None}
    except Exception:  # noqa: BLE001 - recall must never block seeing
        logger.info("visual observation persist skipped", exc_info=True)
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
