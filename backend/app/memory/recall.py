"""Explicit-recall fusion: raw Events are the authority; memories accelerate.

Fresh implicit retrieval stays elsewhere. This module is for search_memory
and other explicit history questions — never for injecting old topics into
unrelated turns.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import math
import re
import time
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.continuity import classify_memory_intent, wants_historical_truth
from app.memory.episodes import recent_episodes
from app.memory.life_archive.locate import MAX_HITS as MAX_ARCHIVE_HITS
from app.memory.life_archive.locate import (
    is_owner_history_query,
    locate_archive,
    resolve_shelf,
)
from app.memory.observe import log_memory
from app.memory.retrieval import Retriever
from app.memory.visual import (
    VISUAL_EVENT_TYPE,
    is_camera_prompt_echo,
    is_keep_recall_query,
    is_memory_hedge_scene,
    is_visual_recall_query,
    search_visual_observations,
    visual_observation_matches,
)
from app.models import Entity, Event, Memory
from app.utils.text import simple_tokens

logger = logging.getLogger("ev.memory.recall")

_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "did",
        "do",
        "for",
        "give",
        "given",
        "had",
        "have",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "which",
        "you",
    }
)
_NAMING_QUERY = re.compile(
    r"\b(name|named|call|called|calling|give|gave|label|title)\b",
    re.IGNORECASE,
)
_THREAD_HEAD = re.compile(r"WhatsApp thread:\s*([^.]+)", re.IGNORECASE)
_THREAD_META = re.compile(
    r"WhatsApp thread:\s*([^.]+)\.\s*(\d+)\s+messages(?:\s*\(([^)]+)\))?",
    re.IGNORECASE,
)
_PERSON_HEAD = re.compile(r"Person:\s*([^(.\n]+)", re.IGNORECASE)
_CHAT_EXCERPT = re.compile(
    r"^WhatsApp with\s+(.+?)\s+—\s+(Owner|.+?):\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_LIVE_CHAT_LINE = re.compile(r"^(You|.+?):\s+(.+)$")
_LAST_CHAT_ASK = re.compile(
    r"\b(last time|last chat|last talk|recent|latest|catch me up|up to speed|any word)\b",
    re.IGNORECASE,
)
_NAMING_LANG = re.compile(
    r"\b(i(?:'m| am) calling|called(?:\s+this|\s+it)?|the name is|"
    r"remember that|named|name is|is called|call (?:this|it))\b",
    re.IGNORECASE,
)
_PROPER = re.compile(
    r"\b(?:Project\s+[A-Z][A-Za-z0-9'-]+(?:\s+[A-Z][A-Za-z0-9'-]+)*|"
    r"[A-Z][a-zA-Z0-9']+(?:\s+[A-Z][a-zA-Z0-9']+){1,4})\b"
)
_CURRENT_TRUTH = re.compile(
    r"\b(now|currently|these days|called now|what(?:'s| is) it called now)\b",
    re.IGNORECASE,
)
_EXPAND_ARMS = (
    "calling this experiment",
    "calling this project",
    "the name is",
    "is called",
    "remember that",
    "project",
    "experiment",
    "named",
)


def expand_recall_queries(query: str) -> list[str]:
    """Search-term reformulation only. Does not invent owner facts."""

    original = (query or "").strip()
    if not original:
        return []
    out: list[str] = [original]
    lowered = original.lower()
    tokens = simple_tokens(original)
    if "experiment" in tokens:
        out.extend(["experiment", "calling this experiment", "project experiment"])
    if "project" in tokens:
        out.extend(["project", "calling this project"])
    if _NAMING_QUERY.search(original):
        out.extend(_EXPAND_ARMS)
        if "thing" in tokens or "it" in lowered.split():
            out.extend(["called", "named", "calling this"])
    seen: set[str] = set()
    unique: list[str] = []
    for arm in out:
        key = arm.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(arm.strip())
    return unique[:8]


def _query_fp(query: str) -> str:
    return hashlib.sha256((query or "").encode("utf-8")).hexdigest()[:12]


def _event_text(event: Event) -> str:
    return str((event.content or {}).get("text") or "").strip()


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _named_value_query(query: str) -> bool:
    return bool(_NAMING_QUERY.search(query or ""))


def _wants_current(query: str) -> bool:
    return bool(_CURRENT_TRUTH.search(query or "")) and not wants_historical_truth(query)


def _idf(token: str, df: dict[str, int], n: int) -> float:
    return math.log((n + 1) / (df.get(token, 0) + 1))


def _score_event(
    *,
    query: str,
    expanded: list[str],
    text: str,
    event_type: str,
    occurred_at: datetime | None,
    df: dict[str, int],
    n_docs: int,
) -> tuple[float, dict[str, float], str]:
    query_tokens = simple_tokens(" ".join(expanded)) - _STOP
    text_tokens = simple_tokens(text)
    content_tokens = text_tokens - _STOP
    union = query_tokens | content_tokens
    lexical = (len(query_tokens & content_tokens) / len(union)) if union else 0.0
    rare = 0.0
    for token in query_tokens & content_tokens:
        rare += _idf(token, df, n_docs)
    rare_norm = min(1.0, rare / 6.0)
    naming = 1.0 if _NAMING_LANG.search(text) else 0.0
    proper = 1.0 if _PROPER.search(text) else 0.0
    if _named_value_query(query) and naming and proper:
        proper = 1.0
        naming = 1.0
    if event_type == "message.user":
        speaker = 1.0
    elif event_type == "camera.observation":
        speaker = 0.92
    elif event_type == "message.assistant":
        speaker = 0.12
    else:
        speaker = 0.35
    now = datetime.now(UTC)
    days = max(0.0, (now - _as_utc(occurred_at)).total_seconds() / 86400.0)
    recency = math.exp(-days / 90.0)
    phrase = 0.0
    lowered = text.lower()
    for arm in expanded[1:]:
        if len(arm) >= 8 and arm.lower() in lowered:
            phrase = 1.0
            break
    score = (
        0.20 * lexical
        + 0.26 * rare_norm
        + 0.18 * naming
        + 0.16 * proper
        + 0.14 * speaker
        + 0.04 * recency
        + 0.02 * phrase
    )
    parts = {
        "lexical": round(lexical, 4),
        "rare": round(rare_norm, 4),
        "naming": naming,
        "proper": proper,
        "speaker": speaker,
        "recency": round(recency, 4),
        "phrase": phrase,
    }
    reason = "owner_naming" if naming and speaker >= 1.0 else "lexical" if lexical >= 0.04 else "weak"
    return score, parts, reason


_GENERIC = _STOP | {
    "call",
    "called",
    "calling",
    "experiment",
    "feature",
    "give",
    "gave",
    "name",
    "named",
    "originally",
    "project",
    "remember",
    "thing",
}


_WEAK_SPECIFIC = {
    "about",
    "it's",
    "its",
    "names",
    "that's",
    "thats",
    "these",
    "those",
    "what",
    "what's",
    "whats",
    "when",
    "which",
    "who",
    "whose",
}


_STEM_ALIASES = {
    "prefer": {"prefer", "prefers", "preferred", "preference", "preferences"},
    "prefers": {"prefer", "prefers", "preferred", "preference", "preferences"},
    "preferred": {"prefer", "prefers", "preferred", "preference", "preferences"},
    "preference": {"prefer", "prefers", "preferred", "preference", "preferences"},
    "solve": {"solve", "solved", "solving", "solution"},
    "solved": {"solve", "solved", "solving", "solution"},
    "remember": {"remember", "remembered", "remembers", "memorize", "memorise"},
    "remembered": {"remember", "remembered", "remembers", "memorize", "memorise"},
}


def _stems(tokens: set[str]) -> set[str]:
    out = set(tokens)
    for token in tokens:
        if token.endswith("s") and len(token) > 4:
            out.add(token[:-1])
        else:
            out.add(token + "s")
        out |= _STEM_ALIASES.get(token, set())
    return out


def _supported(query: str, parts: dict[str, float], text: str) -> bool:
    specific = simple_tokens(query) - _GENERIC
    distinctive = {
        token
        for token in specific
        if token not in _WEAK_SPECIFIC and len(token) >= 4 and "'" not in token
    }
    text_tokens = simple_tokens(text)
    if distinctive and not (_stems(distinctive) & _stems(text_tokens)):
        return False
    if distinctive and (_stems(distinctive) & _stems(text_tokens)):
        return True
    if parts["lexical"] >= 0.04:
        return True
    if parts["phrase"] >= 1.0:
        return True
    return bool(_named_value_query(query) and parts["naming"] >= 1.0 and parts["speaker"] >= 1.0)


def _when_epoch(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _visual_item_rank(item: dict, *, recency_first: bool = False, topic: str = "") -> tuple:
    """Newest keep/identity first. An older scene must not bury a new memorize."""

    from app.memory.visual import visual_content_tokens, visual_index_tokens, _stems

    blob = " ".join(
        part
        for part in (
            str(item.get("text") or ""),
            str(item.get("recall") or ""),
            str(item.get("object") or ""),
        )
        if part
    ).strip().lower()
    keep = (
        "asked evie to remember" in blob
        or "you asked me to remember" in blob
        or str(item.get("reason") or "") == "visual_keep"
        or str(item.get("memory_type") or "") == "fact"
    )
    recency = -_when_epoch(item.get("when") or item.get("occurred_at"))
    overlap = 0
    wanted = visual_content_tokens(topic) if topic and topic not in {"", "this", "that", "it", "you"} else set()
    if wanted:
        have = visual_index_tokens(blob)
        overlap = -len(_stems(wanted) & _stems(have))
    identity = 0 if (keep or item.get("object") or "it reads" in blob) else 1
    if recency_first:
        return (recency, identity, overlap, -len(blob))
    return (overlap, identity, recency, -len(blob))


def _visual_spoken_rank(text: str) -> tuple:
    return _visual_item_rank({"text": text})


def _is_waffle_evidence_line(text: str, query: str = "") -> bool:
    """True when a packed line is a greeting, echoed question, or story — not a record."""

    blob = " ".join(str(text or "").split()).strip().lower()
    if not blob:
        return True
    if blob.endswith("?") or blob.startswith("tell me "):
        return True
    if blob.startswith(("hey!", "hey ", "hi!", "hi i'm", "hi i am")):
        return True
    if "i'm e v" in blob or "i’m e v" in blob or "im ev" in blob:
        return True
    if "chat buddy" in blob or "think of me as" in blob:
        return True
    if "what's on your mind" in blob or "whats on your mind" in blob:
        return True
    if "visually observe" in blob or "mac camera" in blob:
        return True
    if "help with memory and history questions" in blob:
        return True
    asked = " ".join(str(query or "").split()).strip().lower().rstrip("?.")
    if asked and asked in blob and blob.rstrip("?.") != asked and blob.endswith("?"):
        return True
    return False


def _speak_name_list(lead: str, names: list[str]) -> str:
    shown = [name for name in names if name][:5]
    if not shown:
        return ""
    if len(shown) == 1:
        return f"{lead} {shown[0]}."
    if len(shown) == 2:
        return f"{lead} {shown[0]} and {shown[1]}."
    return f"{lead} {', '.join(shown[:-1])}, and {shown[-1]}."


def _as_when(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _speak_when(value) -> str:
    when = _as_when(value)
    if when is None:
        return ""
    now = datetime.now(UTC)
    if when.year == now.year:
        return f"on {when.strftime('%B')} {when.day}"
    return f"in {when.strftime('%B')} {when.year}"


def _clip_spoken_body(body: str) -> str:
    text = " ".join(str(body or "").split()).strip().strip("\"'")
    if not text or text.lower().startswith("http"):
        return ""
    if len(text) <= 140:
        return text
    clipped = text[:137].rsplit(" ", 1)[0].rstrip(",;:")
    return (clipped or text[:137]).rstrip() + "…"


def _parse_chat_beat(item: dict) -> dict | None:
    text = " ".join(str(item.get("text") or "").split()).strip()
    if not text:
        return None
    title = ""
    who = ""
    body = ""
    owner = False
    match = _CHAT_EXCERPT.match(text)
    if match:
        title = match.group(1).strip()
        who = match.group(2).strip()
        body = match.group(3).strip()
        owner = who.lower() == "owner"
    else:
        live = _LIVE_CHAT_LINE.match(text)
        if not live or text.lower().startswith("whatsapp thread:"):
            return None
        who = live.group(1).strip()
        body = live.group(2).strip()
        owner = who.lower() in {"you", "owner"}
        title = "" if owner else who
    body = _clip_spoken_body(body)
    if not body:
        return None
    speaker = "You" if owner else (who if who.lower() != "owner" else "You")
    return {
        "title": title,
        "who": speaker,
        "body": body,
        "owner": owner,
        "when": item.get("when") or item.get("occurred_at"),
    }


def _speak_chat_overview(cards: list[tuple[str, int]]) -> str:
    ranked = sorted(
        [(name, count) for name, count in cards if name],
        key=lambda item: (-item[1], item[0].lower()),
    )[:5]
    names = [name for name, _count in ranked]
    if not names:
        return ""
    if len(names) == 1:
        return f"You talk on WhatsApp with {names[0]}."
    if len(names) == 2:
        return f"You talk most on WhatsApp with {names[0]}, and also {names[1]}."
    return (
        f"You talk most on WhatsApp with {names[0]}, then {names[1]}, "
        f"and also {_speak_name_list('', names[2:]).lstrip().rstrip('.') }."
    )


def _speak_person_chat(query: str, beats: list[dict], names: list[str]) -> str:
    name = next((item for item in names if item), "")
    if not name:
        for beat in beats:
            title = str(beat.get("title") or "").strip()
            if title:
                name = title
                break
    if not name:
        from app.memory.life_archive.locate import _chat_person_query_token

        token = _chat_person_query_token(query)
        name = token[:1].upper() + token[1:] if token else ""
    recent = sorted(beats, key=lambda item: _as_when(item.get("when")) or datetime.min.replace(tzinfo=UTC))
    recent = recent[-3:]
    if not recent and not name:
        return ""
    when = _speak_when(recent[-1]["when"] if recent else None)
    last_ask = bool(_LAST_CHAT_ASK.search(query or "")) or bool(
        re.search(r"\blast\b", query or "", re.IGNORECASE)
    )
    if last_ask or recent:
        lead = f"Last you talked with {name} on WhatsApp" if name else "Last you were chatting on WhatsApp"
    else:
        lead = f"You and {name} talk on WhatsApp" if name else "You talk on WhatsApp"
    if when:
        lead += f", {when}"
    lead += "."
    parts = [lead]
    seen: set[str] = set()
    for beat in recent:
        key = f"{beat['who']}:{beat['body']}".lower()
        if key in seen:
            continue
        seen.add(key)
        body = beat["body"]
        if not body.endswith((".", "!", "?")):
            body = body.rstrip(".") + "."
        parts.append(f"{beat['who']} said {body}")
    spoken = " ".join(parts)
    return spoken[:400]


def _spoken_from_evidence(evidence: list, query: str = "") -> str:
    """Short live line from packed evidence so pipeline/Grok can speak a hit."""

    from app.memory.life_archive.locate import is_chat_with_other_person, is_owner_history_query
    from app.memory.visual import is_keep_recall_query, is_visual_recall_query, keep_topic

    thread_names: list[str] = []
    thread_cards: list[tuple[str, int]] = []
    person_names: list[str] = []
    excerpts: list[str] = []
    chat_items: list[dict] = []
    preferred: list[str] = []
    loops: list[str] = []
    rest: list[str] = []
    visual_items: list[dict] = []
    person_chat = is_chat_with_other_person(query)
    for item in evidence:
        text = " ".join(str(item.get("text") or "").split()).strip()
        if not text:
            continue
        kind = str(item.get("memory_type") or item.get("kind") or "")
        blob = text.lower()
        if kind == "life.chat.thread":
            meta = _THREAD_META.search(text)
            match = meta or _THREAD_HEAD.search(text)
            name = (match.group(1) if match else "").strip(" .")
            count = int(meta.group(2)) if meta else 0
            if name and name not in thread_names:
                thread_names.append(name[:48])
                thread_cards.append((name[:48], count))
            continue
        if kind == "life.person":
            match = _PERSON_HEAD.search(text)
            name = (match.group(1) if match else "").strip(" .")
            if name and name not in person_names:
                person_names.append(name[:48])
            continue
        if kind == "life.chat.excerpt" or item.get("kind") == "live_life":
            if text not in excerpts:
                excerpts.append(text[:400])
            beat = _parse_chat_beat(item if isinstance(item, dict) else {"text": text})
            if beat:
                chat_items.append(beat)
            continue
        if person_chat:
            continue
        if is_memory_hedge_scene(text) or "(system confirmation" in blob or "(life record" in blob:
            continue
        if blob.startswith("episode:"):
            continue
        if is_camera_prompt_echo(text):
            continue
        if "once upon a time" in blob or "little story for you" in blob:
            continue
        if blob.startswith("observed:"):
            continue
        if _is_waffle_evidence_line(text, query):
            continue
        if blob.rstrip("?.") == (query or "").strip().lower().rstrip("?."):
            continue
        visual_query = is_visual_recall_query(query) or is_keep_recall_query(query)
        if visual_query:
            from app.memory.visual import recall_spoken_from_keep

            keep_item = (
                is_keep_recall_query(query)
                or str(item.get("reason") or "") == "visual_keep"
                or kind == "fact"
                or "asked evie to remember" in blob
                or "you asked me to remember" in blob
            )
            spoken_text = str(item.get("recall") or "").strip()
            if keep_item:
                spoken_text = spoken_text or recall_spoken_from_keep(
                    text, item if isinstance(item, dict) else None
                )
            visual_items.append(
                {
                    "text": (spoken_text or text)[:400],
                    "match_text": text[:400],
                    "recall": spoken_text[:400] if spoken_text else text[:400],
                    "object": item.get("object") or "",
                    "reason": item.get("reason") or "",
                    "memory_type": kind,
                    "when": item.get("when") or item.get("occurred_at"),
                }
            )
        if kind == "open_loop" or blob.startswith("resolved:") or blob.startswith("open:"):
            if text not in loops:
                loops.append(text[:400])
        elif kind in {"fact", "preference", "decision"} or "asked evie to remember" in blob:
            if text not in preferred:
                preferred.append(text[:400])
        elif text not in rest:
            rest.append(text[:400])
        if (
            not visual_query
            and len(preferred) >= 2
            and len(loops) >= 2
        ):
            break
    from app.memory.state import classify_temporal_query

    mode = classify_temporal_query(query).mode
    owner_first = (
        not person_chat
        and (
            is_owner_history_query(query)
            or is_visual_recall_query(query)
            or is_keep_recall_query(query)
        )
    )
    if is_visual_recall_query(query) or is_keep_recall_query(query):
        from app.memory.visual import visual_content_tokens, visual_index_tokens, _stems

        topic = keep_topic(query)
        recency_first = topic in {"", "this", "that", "it", "you"}
        wanted = visual_content_tokens(topic) if not recency_first else set()
        if wanted:
            matched = [
                item
                for item in visual_items
                if _stems(wanted)
                & _stems(
                    visual_index_tokens(
                        " ".join(
                            part
                            for part in (
                                str(item.get("text") or ""),
                                str(item.get("match_text") or ""),
                                str(item.get("object") or ""),
                            )
                            if part
                        )
                    )
                )
            ]
            if matched:
                visual_items = matched
            elif is_keep_recall_query(query):
                return "I cannot find that particular record."
        ranked = sorted(
            visual_items,
            key=lambda item: _visual_item_rank(
                item, recency_first=recency_first, topic=topic
            ),
        )
        if ranked:
            return ranked[0]["text"][:400]
    if mode in {"solved", "leave_off", "still_open"}:
        lines = (loops + preferred)[:3] or rest[:3]
    elif owner_first:
        lines = (preferred + loops + rest)[:3]
    else:
        lines = (preferred + rest)[:3]
    if owner_first and lines:
        return " ".join(
            line if line.endswith((".", "!", "?")) else line.rstrip(".") + "."
            for line in lines
        )[:400]
    if person_chat:
        spoken = _speak_person_chat(query, chat_items, thread_names)
        if spoken:
            return spoken[:400]
    chats = _speak_chat_overview(thread_cards) or _speak_name_list(
        "You talk on WhatsApp with", thread_names
    )
    if chats:
        return chats[:400]
    people = _speak_name_list("People you talk with include", person_names)
    if people:
        return people[:400]
    if lines:
        return " ".join(lines)[:400]
    return "I cannot find that particular record."


def _json_ready(value):
    """Postgres JSON columns refuse datetime. Live book recall died on commit."""

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _finish_explicit_recall(
    *,
    query: str,
    intent: str,
    evidence: list,
    started: float,
    shelf: str | None,
    timeline_rows: list,
    facet_mode: str,
    semantic_ms: int,
    facet: dict | None = None,
) -> dict:
    packed_ms = int((time.perf_counter() - started) * 1000)
    contains = any(
        bool(_PROPER.search(str(item.get("text") or "")))
        or _NAMING_LANG.search(str(item.get("text") or ""))
        for item in evidence
    )
    log_memory(
        "memory.recall_pack",
        extra={
            "evidence_count": len(evidence),
            "intent": intent,
            "elapsed_ms": packed_ms,
            "semantic_ms": semantic_ms,
            "life_shelf": shelf or "none",
            "has_owner_event": any(item.get("source") == "owner" for item in evidence),
        },
    )
    log_memory(
        "memory.tool_output_sent",
        extra={
            "contains_naming_or_proper": contains,
            "count": len(evidence),
        },
    )
    extra = facet or {}
    lines = [
        " ".join(str(item.get("text") or "").split()).strip()[:200]
        for item in evidence[:8]
        if str(item.get("text") or "").strip()
    ]
    return _json_ready({
        "ok": bool(evidence),
        "intent": "explicit_recall" if intent != "fresh" else intent,
        "question": (query or "")[:240],
        "count": len(evidence),
        "lines": lines,
        "evidence": evidence,
        "results": [
            {
                "id": item.get("id"),
                "text": item.get("text"),
                "memory_type": item.get("memory_type") or item.get("kind"),
                "score": item.get("score"),
                "date": item.get("when"),
                "provenance": item.get("provenance") or [],
            }
            for item in evidence
        ],
        "timeline": [
            {
                "id": row.get("id"),
                "occurred_at": row.get("when"),
                "source": row.get("source") or row.get("event_source"),
                "event_type": row.get("memory_type") or row.get("event_type"),
                "text": row.get("text"),
                "score": row.get("score"),
            }
            for row in timeline_rows[:8]
            if row.get("kind") in {"life", "live_life", "event"}
        ],
        "degraded": False,
        "grounding": "evidence" if evidence else "no_reliable_record",
        "spoken": _spoken_from_evidence(evidence, query) if evidence else "I cannot find that particular record.",
        "elapsed_ms": packed_ms,
        "facet": facet_mode,
        "life_shelf": shelf,
        "open_loops": extra.get("open_loops") or [],
        "decisions": extra.get("decisions") or [],
        "changes": extra.get("changes"),
        "project_state": extra.get("project_state"),
    })


async def build_explicit_recall_payload(
    session: AsyncSession,
    query: str,
    *,
    k: int = 10,
    memory_type_hint: str | None = None,
) -> dict:
    started = time.perf_counter()
    intent = classify_memory_intent(query)
    expanded = expand_recall_queries(query)
    log_memory(
        "memory.recall_started",
        extra={
            "intent": intent,
            "query_chars": len(query or ""),
            "query_fp": _query_fp(query),
            "k": k,
        },
    )
    log_memory(
        "memory.query_expanded",
        extra={"arms": len(expanded), "named_value": _named_value_query(query)},
    )
    try:
        from app.memory.state import classify_temporal_query

        temporal = classify_temporal_query(query)
        log_memory(
            "memory.temporal_query",
            extra={"mode": temporal.mode, "query_fp": _query_fp(query)},
        )
        from app.memory.life_archive.locate import is_owner_history_query, life_shelf_for_memory_search

        shelf = life_shelf_for_memory_search(query, await resolve_shelf(session, query))
        if is_visual_recall_query(query) or is_keep_recall_query(query):
            # Camera keeps live in observation/fact rows, not a takeout drawer.
            shelf = None
        archive = await locate_archive(session, query, shelf=shelf, k=min(MAX_ARCHIVE_HITS, k))
        log_memory(
            "memory.life_locate",
            extra={"shelf": shelf or "none", "hits": len(archive)},
        )
        if shelf is not None:
            # One drawer, one small pack. Do not scan chat/memories/neighbors.
            return _finish_explicit_recall(
                query=query,
                intent=intent,
                evidence=archive[: max(1, min(k, MAX_ARCHIVE_HITS))],
                started=started,
                shelf=shelf,
                timeline_rows=archive,
                facet_mode=temporal.mode,
                semantic_ms=0,
            )
        events, _event_meta = await _search_events(
            session, query, expanded, k=max(12, k), until=temporal.until or temporal.as_of
        )
        if is_visual_recall_query(query) or is_keep_recall_query(query):
            visual_hits = await search_visual_observations(
                session, query, k=max(6, k), until=temporal.until or temporal.as_of
            )
            seen_ids = {item.get("id") for item in visual_hits}
            events = visual_hits + [item for item in events if item.get("id") not in seen_ids]
        log_memory("memory.event_search", extra={"candidates": len(events)})
        memories, semantic_ms = await _search_memories(
            session,
            query,
            k=max(12, k),
            memory_type_hint=memory_type_hint,
            as_of=temporal.as_of if temporal.mode in {"as_of", "historical"} else None,
            include_historical=is_owner_history_query(query)
            or temporal.mode in {"historical", "solved", "as_of", "changes"},
        )
        if is_owner_history_query(query):
            owned = await _owner_state_memories(session, query, k=max(12, k))
            seen = {item.get("id") for item in memories}
            memories = owned + [item for item in memories if item.get("id") not in seen]
        log_memory("memory.semantic_search", extra={"candidates": len(memories)})
        episodes = await recent_episodes(session, k=5)
        log_memory("memory.episode_search", extra={"candidates": len(episodes)})
        entities = await _search_entities(session, query, expanded)
        neighbors = await _expand_neighbors(session, events[:4])
        log_memory("memory.neighbor_expand", extra={"windows": len(neighbors)})
        evidence = _pack_evidence(
            query,
            events=events,
            memories=memories,
            episodes=episodes,
            entities=entities,
            neighbors=neighbors,
            k=max(1, min(k, 8)),
        )
        facet = await _facet_pack(session, temporal)
        extras = [
            item
            for item in (facet.get("evidence_extra") or [])
            if str(item.get("text") or "").strip()
        ]
        if temporal.mode in {"solved", "leave_off", "still_open"}:
            if extras:
                # Event search matches the word "solve" in stories and echoed
                # questions. Loops/decisions are the record for these asks.
                evidence = extras[: max(1, min(k, 12))]
        elif extras:
            evidence = extras + evidence
            evidence = evidence[: max(1, min(k, 12))]
        return _finish_explicit_recall(
            query=query,
            intent=intent,
            evidence=evidence,
            started=started,
            shelf=None,
            timeline_rows=events,
            facet_mode=temporal.mode,
            semantic_ms=semantic_ms,
            facet=facet,
        )
    except Exception:  # noqa: BLE001 - tool must not crash the live turn
        logger.exception("explicit_recall_failed")
        log_memory("memory.degraded", extra={"error": "explicit_recall_failed"})
        return {
            "ok": False,
            "intent": intent,
            "question": (query or "")[:240],
            "count": 0,
            "evidence": [],
            "results": [],
            "timeline": [],
            "degraded": True,
            "grounding": "no_reliable_record",
            "spoken": "I cannot find that particular record.",
        }


def _loop_evidence_item(item: dict, *, confidence: str, score: float) -> dict | None:
    title = " ".join(str(item.get("title") or "").split()).strip()
    if not title or len(title) < 4:
        return None
    return {
        "id": item.get("id"),
        "source": "memory",
        "when": item.get("when"),
        "text": title,
        "kind": "open_loop",
        "memory_type": "open_loop",
        "confidence": confidence,
        "score": score,
        "provenance": item.get("resolution_event_ids") or item.get("source_event_ids") or [],
    }


async def _facet_pack(session, temporal) -> dict:
    from app.memory.loops import list_loops, loop_public, rank_open_loops
    from app.memory.state import current_typed, get_changes, get_project_state, leave_off_packet

    extra: list[dict] = []
    pack: dict = {"open_loops": [], "decisions": [], "changes": None, "project_state": None, "evidence_extra": extra}
    seen_titles: set[str] = set()

    def _add_loop(item: dict, *, confidence: str, score: float) -> None:
        key = " ".join(str(item.get("title") or "").split()).strip().lower()
        if not key or key in seen_titles:
            return
        row = _loop_evidence_item(item, confidence=confidence, score=score)
        if row is None:
            return
        seen_titles.add(key)
        extra.append(row)

    if temporal.mode == "leave_off":
        packet = await leave_off_packet(session)
        pack["open_loops"] = packet.get("open_loops") or []
        pack["decisions"] = packet.get("decisions") or []
        pack["project_state"] = packet.get("current_state")
        for item in pack["open_loops"][:6]:
            _add_loop(item, confidence="current_state", score=0.9)
        return pack
    if temporal.mode == "still_open":
        rows = rank_open_loops(await list_loops(session, k=12), k=8)
        pack["open_loops"] = [loop_public(row) for row in rows]
        for item in pack["open_loops"]:
            _add_loop(item, confidence="current_state", score=0.92)
        return pack
    if temporal.mode == "solved":
        rows = await list_loops(session, status="resolved", k=8)
        pack["open_loops"] = [loop_public(row) for row in rows]
        for item in pack["open_loops"]:
            _add_loop(item, confidence="historical", score=0.9)
        decisions = await current_typed(session, "decision", k=8)
        pack["decisions"] = [
            {
                "id": str(row.id),
                "text": row.text,
                "when": row.event_time.isoformat() if row.event_time else None,
            }
            for row in decisions
        ]
        seen_decisions: set[str] = set()
        for row in decisions:
            text = " ".join(str(row.text or "").split()).strip()
            key = text.lower()
            if not text or key in seen_decisions or _is_waffle_evidence_line(text):
                continue
            seen_decisions.add(key)
            extra.append(
                {
                    "id": str(row.id),
                    "source": "memory",
                    "when": row.event_time.isoformat() if row.event_time else None,
                    "text": text[:400],
                    "kind": "memory",
                    "memory_type": "decision",
                    "confidence": "owner_state",
                    "score": 0.88,
                    "provenance": [],
                }
            )
        return pack
    if temporal.mode == "changes":
        pack["changes"] = await get_changes(session, since=temporal.since, until=temporal.until)
        return pack
    if temporal.mode in {"as_of", "historical"}:
        from app.memory.state import memories_as_of

        boundary = temporal.as_of or temporal.until
        if boundary is not None:
            rows = await memories_as_of(session, boundary=boundary, k=16)
            pack["project_state"] = {
                "as_of": boundary.isoformat(),
                "memories": [
                    {
                        "id": str(row.id),
                        "text": row.text,
                        "memory_type": row.memory_type,
                        "is_current": row.is_current,
                    }
                    for row in rows
                ],
            }
        else:
            pack["project_state"] = await get_project_state(session)
        return pack
    return pack


async def _search_events(
    session: AsyncSession,
    query: str,
    expanded: list[str],
    *,
    k: int,
    until=None,
) -> tuple[list[dict], dict]:
    stmt = (
        select(Event)
        .where(
            Event.tombstoned_at.is_(None),
            Event.event_type.in_(
                ("message.user", "message.assistant", "voice.transcript", "camera.observation")
            ),
            Event.privacy_level != "never_send_to_model",
            Event.privacy_level != "sensitive",
        )
        .order_by(Event.occurred_at.desc())
            .limit(800)
    )
    if until is not None:
        stmt = stmt.where(Event.occurred_at <= until)
    rows = list((await session.execute(stmt)).scalars().all())
    try:
        from app.memory.index import search_event_ids

        extra_ids = await search_event_ids(session, query, k=max(24, k))
        have = {row.id for row in rows}
        missing = [event_id for event_id in extra_ids if event_id not in have]
        if missing:
            extra = list(
                (await session.execute(select(Event).where(Event.id.in_(missing)))).scalars().all()
            )
            rows = extra + rows
    except Exception:  # noqa: BLE001 - lexical scan remains authoritative
        pass
    df: dict[str, int] = {}
    texts: list[tuple[Event, str]] = []
    for event in rows:
        text = _event_text(event)
        texts.append((event, text))
        for token in simple_tokens(text):
            df[token] = df.get(token, 0) + 1
    n_docs = max(1, len(texts))
    scored: list[dict] = []
    for event, text in texts:
        if not text:
            continue
        score, parts, reason = _score_event(
            query=query,
            expanded=expanded,
            text=text,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            df=df,
            n_docs=n_docs,
        )
        selected = _supported(query, parts, text)
        visual_hit = event.event_type == VISUAL_EVENT_TYPE and visual_observation_matches(
            query, text
        )
        if visual_hit:
            selected = True
            score = max(score, 0.78)
            reason = "visual_content"
        elif event.event_type == VISUAL_EVENT_TYPE and is_visual_recall_query(query):
            selected = False
        if not selected:
            continue
        camera = event.event_type == VISUAL_EVENT_TYPE
        scored.append(
            {
                "id": str(event.id),
                "when": event.occurred_at.isoformat() if event.occurred_at else None,
                "text": text[:400],
                "score": round(score, 4),
                "kind": "event",
                "memory_type": "observation" if camera else "event",
                "source": "owner" if event.event_type == "message.user" else "evie",
                "confidence": "visual_observation"
                if camera
                else "exact_owner_event"
                if event.event_type == "message.user"
                else "assistant_turn",
                "event_type": event.event_type,
                "event_source": event.source,
                "conversation_id": str(event.conversation_id) if event.conversation_id else None,
                "occurred_at": event.occurred_at,
                "parts": parts,
                "reason": reason,
            }
        )
    scored.sort(key=lambda row: row["score"], reverse=True)
    if _named_value_query(query) and not wants_historical_truth(query):
        scored.sort(
            key=lambda row: (
                0 if row.get("source") == "owner" and row.get("parts", {}).get("naming") else 1,
                -_as_utc(row.get("occurred_at")).timestamp(),
                -float(row.get("score") or 0),
            )
        )
    elif wants_historical_truth(query):
        scored.sort(
            key=lambda row: (
                0 if row.get("source") == "owner" and row.get("parts", {}).get("naming") else 1,
                _as_utc(row.get("occurred_at")),
            )
        )
    for row in scored[:6]:
        parts = row.get("parts") or {}
        log_memory(
            "memory.rerank",
            extra={
                "event_id": row["id"],
                "lexical": parts.get("lexical"),
                "recency": parts.get("recency"),
                "speaker": "user" if row.get("event_type") == "message.user" else "assistant",
                "selected": True,
                "reason": row.get("reason"),
            },
        )
    return scored[:k], {"scanned": len(rows)}


async def _search_memories(
    session: AsyncSession,
    query: str,
    *,
    k: int,
    memory_type_hint: str | None,
    as_of=None,
    include_historical: bool = False,
) -> tuple[list[dict], int]:
    started = time.perf_counter()
    retriever = Retriever(session)
    historical = wants_historical_truth(query) or include_historical
    hits = await retriever.search(
        query,
        k=k,
        access="model",
        min_score=0.0,
        include_historical=historical,
        as_of=as_of,
        memory_types=None,
    )
    rows: list[dict] = []
    hint = (memory_type_hint or "").strip()
    for hit in hits:
        boost = 0.08 if hint and hit.memory_type == hint else 0.0
        if not _memory_supported(query, hit.text):
            continue
        rows.append(
            {
                "id": hit.memory_id,
                "when": hit.event_time.isoformat() if hit.event_time else None,
                "text": (hit.text or "")[:400],
                "score": round(hit.score + boost, 4),
                "kind": "memory",
                "memory_type": hit.memory_type,
                "source": "memory",
                "confidence": "historical_semantic"
                if historical
                else "semantic_memory",
                "provenance": hit.source_event_ids,
            }
        )
    rows.sort(key=lambda row: row["score"], reverse=True)
    return rows[:k], int((time.perf_counter() - started) * 1000)


async def _owner_state_memories(
    session: AsyncSession,
    query: str,
    *,
    k: int,
) -> list[dict]:
    """Preferences/decisions/facts by type, not the importance-capped semantic pool.

    Live "what did I prefer before" failed when the stored preference sat
    outside the top-N retrieval candidates. This reads those rows directly.
    """

    rows = list(
        (
            await session.execute(
                select(Memory)
                .where(
                    Memory.redacted.is_(False),
                    Memory.memory_type.in_(("preference", "decision", "fact")),
                    Memory.privacy_level != "never_send_to_model",
                    Memory.privacy_level != "sensitive",
                )
                .order_by(Memory.event_time.desc())
                .limit(40)
            )
        ).scalars().all()
    )
    hits: list[dict] = []
    for row in rows:
        text = (row.text or "").strip()
        if not text:
            continue
        if not _memory_supported(query, text):
            continue
        hits.append(
            {
                "id": str(row.id),
                "when": row.event_time.isoformat() if row.event_time else None,
                "text": text[:400],
                "score": 0.92 if row.memory_type in {"preference", "decision"} else 0.84,
                "kind": "memory",
                "memory_type": row.memory_type,
                "source": "memory",
                "confidence": "owner_state",
                "provenance": [],
            }
        )
        if len(hits) >= max(1, k):
            break
    return hits


def _memory_supported(query: str, text: str) -> bool:
    if is_visual_recall_query(query) and visual_observation_matches(query, text):
        return True
    distinctive = {
        token
        for token in simple_tokens(query) - _GENERIC
        if token not in _WEAK_SPECIFIC and len(token) >= 4
    }
    text_tokens = simple_tokens(text)
    if distinctive:
        return bool(_stems(distinctive) & _stems(text_tokens))
    left = simple_tokens(query) - _STOP
    if left and text_tokens and (left & text_tokens):
        return True
    return bool(_named_value_query(query) and (_NAMING_LANG.search(text) or _PROPER.search(text)))


async def _search_entities(
    session: AsyncSession, query: str, expanded: list[str]
) -> list[dict]:
    tokens = [token for token in simple_tokens(" ".join(expanded)) if token not in _STOP and len(token) >= 3]
    if not tokens:
        return []
    rows = list((await session.execute(select(Entity).limit(400))).scalars().all())
    hits: list[dict] = []
    for row in rows:
        blob = " ".join([row.name, " ".join(row.aliases or [])]).lower()
        if any(token in blob for token in tokens):
            hits.append(
                {
                    "id": str(row.id),
                    "when": None,
                    "text": f"{row.entity_type}: {row.name}",
                    "score": 0.55,
                    "kind": "entity",
                    "memory_type": "entity",
                    "source": "entity",
                    "confidence": "entity",
                }
            )
    return hits[:6]


async def _expand_neighbors(session: AsyncSession, top_events: list[dict]) -> list[dict]:
    extra: list[dict] = []
    seen: set[str] = {str(row.get("id")) for row in top_events}
    for row in top_events:
        occurred = row.get("occurred_at")
        conversation_id = row.get("conversation_id")
        if occurred is None:
            continue
        moment = _as_utc(occurred)
        stmt = (
            select(Event)
            .where(
                Event.tombstoned_at.is_(None),
                Event.event_type.in_(("message.user", "message.assistant")),
                Event.occurred_at >= moment - timedelta(minutes=20),
                Event.occurred_at <= moment + timedelta(minutes=20),
            )
            .order_by(Event.occurred_at.asc())
            .limit(16)
        )
        if conversation_id:
            from uuid import UUID

            with contextlib.suppress(ValueError):
                stmt = stmt.where(Event.conversation_id == UUID(str(conversation_id)))
        nearby = list((await session.execute(stmt)).scalars().all())
        for event in nearby:
            key = str(event.id)
            if key in seen:
                continue
            text = _event_text(event)
            if not text:
                continue
            seen.add(key)
            extra.append(
                {
                    "id": key,
                    "when": event.occurred_at.isoformat() if event.occurred_at else None,
                    "text": text[:400],
                    "score": 0.42,
                    "kind": "neighbor",
                    "memory_type": "event",
                    "source": "owner" if event.event_type == "message.user" else "evie",
                    "confidence": "neighbor_event",
                    "event_type": event.event_type,
                }
            )
            if len(extra) >= 8:
                return extra
    return extra


def _pack_evidence(
    query: str,
    *,
    events: list[dict],
    memories: list[dict],
    episodes: list[Memory],
    entities: list[dict],
    neighbors: list[dict],
    k: int,
) -> list[dict]:
    episode_rows = [
        {
            "id": str(row.id),
            "when": row.event_time.isoformat() if row.event_time else None,
            "text": (row.text or "")[:240],
            "score": 0.45,
            "kind": "episode",
            "memory_type": "summary",
            "source": "episode",
            "confidence": "episode_summary",
        }
        for row in episodes
        if (row.text or "").strip() and _memory_supported(query, row.text)
    ]
    merged: list[dict] = []
    seen: set[str] = set()
    for group in (events, neighbors, memories, episode_rows, entities):
        for item in group:
            key = f"{item.get('kind')}:{item.get('id')}:{item.get('text')}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    owner_first = [item for item in merged if item.get("source") == "owner"]
    rest = [item for item in merged if item.get("source") != "owner"]
    if is_visual_recall_query(query):
        visual = [
            item
            for item in merged
            if item.get("event_type") == VISUAL_EVENT_TYPE
            or item.get("memory_type") in {"observation", "fact"}
            or item.get("confidence") in {"visual_observation", "visual_keep"}
            or "asked evie to remember" in str(item.get("text") or "").lower()
        ]
        visual = [
            item
            for item in visual
            if not is_memory_hedge_scene(str(item.get("text") or ""))
        ]
        visual.sort(
            key=lambda item: (
                0
                if item.get("memory_type") == "fact"
                or "asked evie to remember" in str(item.get("text") or "").lower()
                else 1
            )
        )
        rest = [item for item in merged if item not in visual]
        owner_first = [item for item in rest if item.get("source") == "owner"]
        rest = [item for item in rest if item.get("source") != "owner"]
        ordered = visual + owner_first + rest
    elif is_owner_history_query(query) or wants_historical_truth(query):
        preferred = [
            item
            for item in merged
            if item.get("memory_type") in {"preference", "decision", "fact"}
            or item.get("confidence") == "owner_state"
            or "asked evie to remember" in str(item.get("text") or "").lower()
        ]
        rest_hist = [item for item in merged if item not in preferred]
        owner_first = [item for item in rest_hist if item.get("source") == "owner"]
        rest_hist = [item for item in rest_hist if item.get("source") != "owner"]
        ordered = preferred + owner_first + rest_hist
    elif _wants_current(query):
        semantic = [item for item in rest if item.get("kind") == "memory"]
        rest = [item for item in rest if item.get("kind") != "memory"]
        ordered = semantic + owner_first + rest
    else:
        ordered = owner_first + rest
    packed = []
    for item in ordered:
        packed.append(
            {
                "id": item.get("id"),
                "source": item.get("source"),
                "when": item.get("when"),
                "text": item.get("text"),
                "kind": item.get("kind"),
                "memory_type": item.get("memory_type"),
                "confidence": item.get("confidence"),
                "score": item.get("score"),
                "provenance": item.get("provenance") or [],
            }
        )
        if len(packed) >= k:
            break
    return packed
