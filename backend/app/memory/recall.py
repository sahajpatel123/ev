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
    is_keep_recall_echo,
    is_keep_recall_query,
    is_memory_hedge_scene,
    is_visual_recall_query,
    keep_topic,
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

    from app.memory.visual import (
        _keep_is_thin,
        _remainder_has_identity,
        is_generic_label_scene,
        visual_content_tokens,
        visual_index_tokens,
        _stems,
    )

    blob = " ".join(
        part
        for part in (
            str(item.get("text") or ""),
            str(item.get("recall") or ""),
            str(item.get("object") or ""),
            str(item.get("description") or ""),
        )
        if part
    ).strip().lower()
    keep = (
        "asked evie to remember" in blob
        or "you asked me to remember" in blob
        or str(item.get("reason") or "") == "visual_keep"
        or str(item.get("confidence") or "") == "visual_keep"
        or str(item.get("kind") or "") == "visual_keep"
    )
    stub = (
        is_generic_label_scene(blob)
        or not _remainder_has_identity(blob)
        or _keep_is_thin(
            {
                "description": item.get("description") or item.get("text") or "",
                "recall": item.get("recall") or "",
                "object": item.get("object") or "",
                "usable_scene": True,
            },
            item.get("text"),
        )
    )
    recency = -_when_epoch(item.get("when") or item.get("occurred_at"))
    overlap = 0
    wanted = visual_content_tokens(topic) if topic and topic not in {"", "this", "that", "it", "you"} else set()
    if wanted:
        have = visual_index_tokens(blob)
        overlap = -len(_stems(wanted) & _stems(have))
    identity = 0 if (keep or item.get("object") or "it reads" in blob) else 1
    if recency_first:
        return (0 if keep else 1, recency, 1 if stub else 0, identity, overlap, -len(blob))
    return (1 if stub else 0, overlap, identity, recency, -len(blob))


def _visual_spoken_rank(text: str) -> tuple:
    return _visual_item_rank({"text": text})


def _is_waffle_evidence_line(text: str, query: str = "") -> bool:
    """True when a packed line is a greeting, echoed question, or story — not a record."""

    blob = " ".join(str(text or "").split()).strip().lower()
    if not blob:
        return True
    if blob.endswith("?") or blob.startswith("tell me "):
        return True
    if is_keep_recall_echo(text):
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
    cap = 140
    try:
        from app.ev.spark_task import active_decision

        decision = active_decision()
        if decision is not None and decision.manner == "readout":
            cap = 900
    except Exception:
        cap = 140
    if len(text) <= cap:
        return text
    clipped = text[: cap - 3].rsplit(" ", 1)[0].rstrip(",;:")
    return (clipped or text[: cap - 3]).rstrip() + "…"


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


def _speak_channel(query: str, items: list[dict] | None = None) -> str:
    from app.memory.life_archive.locate import life_channel

    channel = life_channel(query)
    if channel == "whatsapp":
        return "WhatsApp"
    if channel == "imessage":
        return "Messages"
    if channel == "mail":
        return "mail"
    sources = {str(item.get("source") or "") for item in items or []}
    sources.discard("")
    if sources == {"imessage"}:
        return "Messages"
    if sources == {"whatsapp"}:
        return "WhatsApp"
    if "imessage" in sources and "whatsapp" in sources:
        return "Messages and WhatsApp"
    blob = (query or "").lower()
    if re.search(r"\b(imessage|sms|texts?|messages?)\b", blob):
        return "Messages"
    return "WhatsApp"


def _freshness_diag(kind: str) -> str:
    """Explain empty WhatsApp/mail/Messages with sync state. Empty when truly just empty."""
    try:
        from app.services.life_stream_daemon import (
            ensure_background_sync,
            life_freshness,
        )

        fresh = life_freshness()
        if not fresh.get("ok"):
            return ""
        info = fresh.get(kind) or {}
        if not info.get("readable"):
            if kind == "whatsapp":
                return (
                    "I can't read WhatsApp on this Mac right now — "
                    "WhatsApp Desktop has to be logged in here with Full Disk Access for Evie."
                )
            if kind == "imessage":
                return (
                    "I can't read Messages on this Mac right now — "
                    "grant Evie Full Disk Access so it can see your texts."
                )
            return (
                "I can't read mail on this Mac right now — "
                "grant Evie Full Disk Access so it can see the Mail index."
            )
        if kind == "imessage":
            # chat.db syncs via continuity — no app to nudge. Readable means
            # an empty result is genuinely empty.
            return ""
        age = info.get("mtime_age_s")
        if isinstance(age, (int, float)) and age > 7200:
            try:
                ensure_background_sync()
            except Exception:
                pass
            hours = int(age // 3600)
            if kind == "whatsapp":
                return (
                    f"WhatsApp on this Mac hasn't synced in about {hours} hours. "
                    "I nudged it to sync in the background — ask again in a minute."
                )
            return (
                f"Mail on this Mac hasn't synced in about {hours} hours. "
                "I nudged it to sync in the background — ask again in a minute."
            )
        return ""
    except Exception:
        return ""


def _spoken_empty_connected(query: str) -> str:
    from app.memory.life_archive.locate import (
        CALL_HISTORY_RE,
        _NOTIFICATION_ASK,
        is_live_now_ask,
        life_channel,
    )

    blob = (query or "").lower()
    channel = life_channel(query)
    if channel == "whatsapp" or "whatsapp" in blob:
        from app.memory.life_archive.locate import _CHAT_WITH_OTHER, _QUOTE_FROM_PERSON

        name = ""
        match = _CHAT_WITH_OTHER.search(query or "")
        if match:
            name = next((group for group in match.groups() if group), "")
        if not name:
            quoted = _QUOTE_FROM_PERSON.search(query or "")
            name = quoted.group(1) if quoted else ""
        name = (name or "").strip(" .")
        if name:
            return f"I don't see WhatsApp with {name} on this Mac right now."
        diag = _freshness_diag("whatsapp")
        if diag:
            return diag
        from app.memory.life_archive.locate import chat_search_tokens as _wa_tokens

        _wa_structural = _wa_tokens(query or "")
        if len(_wa_structural) == 1 and re.search(r"\bfrom\b", blob):
            _wa_word = _wa_structural[0]
            _wa_hit = re.search(r"\b" + re.escape(_wa_word) + r"\b", query or "", re.IGNORECASE)
            _wa_display = _wa_hit.group(0) if _wa_hit else _wa_word.title()
            return f"I don't see WhatsApp from {_wa_display} on this Mac right now."
        return (
            "I don't see new WhatsApp on this Mac right now. "
            "WhatsApp Desktop has to be logged in here."
        )
    if CALL_HISTORY_RE.search(blob):
        return "I don't see recent calls on this Mac right now."
    if re.search(r"\bphotos?\b", blob) and is_live_now_ask(query):
        return "I don't see new photos on this Mac right now."
    if channel == "mail" or re.search(r"\b(e-?mails?|gmail|mails?)\b", blob):
        from app.memory.mail_speak import mail_selector

        who = mail_selector(query).who
        if who:
            return f"I don't see mail from {who} on this Mac right now."
        diag = _freshness_diag("mail")
        if diag:
            return diag
        return "I don't see new mail on this Mac right now."
    if channel == "contacts" or re.search(r"\bcontacts?\b", blob):
        return "I don't see that contact on this Mac right now."
    if channel == "imessage":
        diag = _freshness_diag("imessage")
        if diag:
            return diag
        from app.memory.life_archive.locate import (
            _chat_person_query_token as _im_person,
            chat_search_tokens as _im_tokens,
        )

        _im_name = _im_person(query or "")
        if _im_name:
            return f"I don't see messages from {_im_name[:1].upper() + _im_name[1:]} on this Mac right now."
        _im_structural = _im_tokens(query or "")
        if len(_im_structural) == 1 and re.search(r"\bfrom\b", blob):
            _im_word = _im_structural[0]
            _im_hit = re.search(r"\b" + re.escape(_im_word) + r"\b", query or "", re.IGNORECASE)
            _im_display = _im_hit.group(0) if _im_hit else _im_word.title()
            return f"I don't see messages from {_im_display} on this Mac right now."
        return "I don't see new messages on this Mac right now."
    if re.search(r"\b(messages?|texts?|chats?)\b", blob):
        from app.memory.life_archive.locate import (
            _chat_person_query_token as _mix_person,
            chat_search_tokens as _mix_tokens,
        )

        _mix_name = _mix_person(query or "")
        _mix_structural = _mix_tokens(query or "")
        if _mix_name or (len(_mix_structural) == 1 and re.search(r"\bfrom\b", blob)):
            _mix_word = _mix_name or _mix_structural[0]
            _mix_hit = re.search(
                r"\b" + re.escape(_mix_word) + r"\b", query or "", re.IGNORECASE
            )
            _mix_display = _mix_hit.group(0) if _mix_hit else _mix_word.title()
            return f"I don't see messages from {_mix_display} on this Mac right now."
        return "I don't see new messages on this Mac right now."
    if _NOTIFICATION_ASK.search(blob):
        return "I don't see new messages or calls on this Mac right now."
    return "I cannot find that particular record."


def _speak_other_live(query: str, items: list[dict]) -> str:
    from app.memory.life_archive.locate import life_channel
    from app.memory.mail_speak import is_mail_ask, is_mail_hit, speak_mail
    from app.memory.message_speak import is_chat_hit, speak_messages

    channel = life_channel(query)
    mail_items = [item for item in items if is_mail_hit(item)]
    if is_mail_ask(query) or (mail_items and len(mail_items) == len(items)):
        return speak_mail(query, mail_items or items)
    chat_items = [item for item in items if is_chat_hit(item)]
    if chat_items and (channel in {"imessage", "whatsapp"} or len(chat_items) == len(items)):
        spoken = speak_messages(query, chat_items)
        if spoken:
            return spoken
    bits: list[str] = []
    seen: set[str] = set()
    for item in items[:4]:
        if is_mail_hit(item):
            headline = speak_mail(query, [item])
            text = re.sub(r"^Recent mail:\s*", "", headline).strip() if headline else ""
        else:
            text = " ".join(str(item.get("text") or "").split()).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        if not text.endswith((".", "!", "?")):
            text = text.rstrip(".") + "."
        bits.append(text)
    if not bits:
        return ""
    body = " ".join(bits)
    blob = (query or "").lower()
    if re.search(r"\bphotos?\b", blob):
        return f"Latest photos on this Mac: {body}"
    if re.search(r"\b(call|called|calls)\b", blob):
        return f"Recent calls on this Mac: {body}"
    if channel == "contacts" or re.search(r"\bcontacts?\b", blob):
        return f"On this Mac: {body}"
    if channel == "imessage":
        return f"Recent messages on this Mac: {body}"
    if channel == "whatsapp":
        return f"Recent WhatsApp on this Mac: {body}"
    return body


def _speak_person_chat(
    query: str, beats: list[dict], names: list[str], *, channel: str = "WhatsApp"
) -> str:
    from app.ev.spark_task import active_decision, wants_readout
    from app.memory.message_speak import speak_person_gist

    decision = None
    try:
        decision = active_decision()
    except Exception:
        decision = None
    readout = (decision is not None and decision.manner == "readout") or wants_readout(query)
    if not readout:
        return speak_person_gist(query, beats, names, channel=channel or "WhatsApp")
    channel = channel or "WhatsApp"
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
        lead = (
            f"Last you talked with {name} on {channel}"
            if name
            else f"Last you were chatting on {channel}"
        )
    else:
        lead = f"You and {name} talk on {channel}" if name else f"You talk on {channel}"
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


def _keepish_evidence(item: dict) -> bool:
    blob = " ".join(
        part
        for part in (
            str(item.get("text") or ""),
            str(item.get("recall") or ""),
            str(item.get("description") or ""),
            str(item.get("keep_request") or ""),
        )
        if part
    ).lower()
    return (
        str(item.get("reason") or "") == "visual_keep"
        or str(item.get("kind") or "") == "visual_keep"
        or str(item.get("confidence") or "") == "visual_keep"
        or "asked evie to remember" in blob
        or "you asked me to remember" in blob
    )


def _spoken_from_newest_keep(evidence: list, query: str) -> str | None:
    """Generic keep-recall speaks this look, not last week's thicker keep."""

    from app.memory.visual import (
        is_keep_identity_speech,
        is_keep_recall_echo,
        is_keep_recall_query,
        keep_topic,
        recall_spoken_from_keep,
    )

    if not is_keep_recall_query(query):
        return None
    if keep_topic(query) not in {"", "this", "that", "it", "you"}:
        return None
    rows = [item for item in evidence if isinstance(item, dict) and _keepish_evidence(item)]
    if not rows:
        return None
    newest = max(
        enumerate(rows),
        key=lambda pair: (
            _when_epoch(pair[1].get("when") or pair[1].get("occurred_at")),
            1 if str(pair[1].get("attachment_id") or "").strip() else 0,
            1 if str(pair[1].get("reason") or "") == "visual_keep" else 0,
            pair[0],
        ),
    )[1]
    aid = str(newest.get("attachment_id") or "").strip()
    pool: list[dict] = [newest]
    newest_line = str(newest.get("text") or newest.get("description") or "").strip()
    newest_named = bool(
        recall_spoken_from_keep(newest_line, newest).strip()
        or is_keep_identity_speech(newest_line)
    )
    newest_echo = (not newest_named) and is_keep_recall_echo(newest_line)
    for item in evidence:
        if not isinstance(item, dict) or item is newest:
            continue
        other_aid = str(item.get("attachment_id") or "").strip()
        line = str(item.get("text") or item.get("description") or "").strip()
        if aid:
            if other_aid == aid:
                pool.append(item)
            elif newest_echo and is_keep_identity_speech(line):
                pool.append(item)
            continue
        if newest_named:
            continue
        if newest_echo:
            if is_keep_identity_speech(line):
                pool.append(item)
            continue
        if _keepish_evidence(item):
            continue
        if other_aid:
            continue
        lowered = line.lower()
        if lowered.startswith(("i looked", "i recorded", "i took a photo", "i watched")):
            continue
        if is_keep_identity_speech(line):
            pool.append(item)
    ranked = sorted(pool, key=lambda item: _visual_item_rank(item))
    for item in ranked:
        spoken = recall_spoken_from_keep(str(item.get("text") or ""), item)
        line = spoken.strip() or str(item.get("text") or item.get("description") or "").strip()
        if not line:
            continue
        if is_keep_recall_echo(line):
            continue
        asked = " ".join(str(query or "").split()).strip().lower().rstrip("?.")
        if asked and line.lower().rstrip("?.") == asked:
            continue
        if spoken.strip() and not is_keep_recall_echo(spoken):
            return spoken.strip()[:800]
        if line and is_keep_identity_speech(line):
            return line[:800]
    return "I cannot find that particular record."


def _spoken_from_evidence(evidence: list, query: str = "") -> str:
    """Short live line from packed evidence so pipeline/Grok can speak a hit."""

    from app.memory.life_archive.locate import (
        is_chat_summary_query,
        is_chat_with_other_person,
        is_live_now_ask,
        is_owner_history_query,
    )
    from app.memory.life_archive.desk import is_chat_desk_query
    from app.memory.visual import is_keep_recall_query, is_visual_recall_query, keep_topic

    newest_keep = _spoken_from_newest_keep(evidence, query)
    if newest_keep is not None:
        return newest_keep

    thread_names: list[str] = []
    thread_cards: list[tuple[str, int]] = []
    person_names: list[str] = []
    excerpts: list[str] = []
    chat_items: list[dict] = []
    other_live: list[dict] = []
    preferred: list[str] = []
    loops: list[str] = []
    rest: list[str] = []
    visual_items: list[dict] = []
    person_chat = is_chat_with_other_person(query)
    summary_ask = is_chat_summary_query(query)
    desk_ask = is_chat_desk_query(query)
    live_now = is_live_now_ask(query)
    if summary_ask:
        for item in evidence:
            kind = str(item.get("memory_type") or item.get("kind") or "")
            text = " ".join(str(item.get("text") or "").split()).strip()
            if kind == "life.chat.session" and text:
                return text[:520]
        # Fall through and gist beats. Recitation is readout-only.
    if desk_ask:
        for item in evidence:
            kind = str(item.get("memory_type") or item.get("kind") or "")
            text = " ".join(str(item.get("text") or "").split()).strip()
            if kind == "life.chat.desk" and text:
                return text[:520]
        return "I cannot find that particular record."
    if any(str(item.get("memory_type") or "") == "life.chat.talk" for item in evidence):
        for item in evidence:
            kind = str(item.get("memory_type") or item.get("kind") or "")
            text = " ".join(str(item.get("text") or "").split()).strip()
            if kind == "life.chat.talk" and text:
                return text[:520]

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
        live_row = item.get("kind") in {"live_life", "live_mac"}
        now_row = str(kind) in {
            "call.history.recorded",
            "photo.library.indexed",
            "mail.envelope.received",
            "contact.discovered",
            "contact.updated",
        }
        chat_row = (
            kind == "life.chat.excerpt"
            or str(kind).startswith("message.")
            or (
                live_row
                and kind not in {
                    "mail.envelope.received",
                    "call.history.recorded",
                    "photo.library.indexed",
                    "contact.discovered",
                    "contact.updated",
                }
                and bool(_LIVE_CHAT_LINE.match(text))
            )
        )
        if chat_row or now_row:
            if text not in excerpts:
                excerpts.append(text[:400])
            # Mail/call/photo/contact envelopes are never chat beats, even when
            # the subject contains a colon ("Run failed: ...").
            if isinstance(item, dict) and now_row:
                other_live.append(item)
                continue
            beat = _parse_chat_beat(item if isinstance(item, dict) else {"text": text})
            if beat:
                chat_items.append(beat)
            elif isinstance(item, dict) and (now_row or live_row):
                other_live.append(item)
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
                str(item.get("reason") or "") == "visual_keep"
                or str(item.get("kind") or "") == "visual_keep"
                or str(item.get("confidence") or "") == "visual_keep"
                or "asked evie to remember" in blob
                or "you asked me to remember" in blob
            )
            spoken_text = str(item.get("recall") or "").strip()
            if keep_item:
                spoken_text = recall_spoken_from_keep(
                    text, item if isinstance(item, dict) else None
                )
            identity_only = (
                str(item.get("reason") or "") == "visual_keep"
                or str(item.get("kind") or "") == "visual_keep"
                or "asked evie to remember" in blob
                or "you asked me to remember" in blob
            )
            if identity_only and not spoken_text:
                pass
            else:
                visual_items.append(
                    {
                        "text": (spoken_text or ("" if keep_item else text))[:800],
                        "match_text": text[:800],
                        "recall": spoken_text[:800] if spoken_text else (
                            "" if keep_item else text[:800]
                        ),
                        "description": str(item.get("description") or "").strip(),
                        "object": item.get("object") or "",
                        "surface": item.get("surface") or "",
                        "placement": item.get("placement") or "",
                        "reason": item.get("reason") or "",
                        "kind": item.get("kind") or kind,
                        "confidence": item.get("confidence") or "",
                        "keep_request": item.get("keep_request") or "",
                        "attachment_id": item.get("attachment_id") or "",
                        "memory_type": kind,
                        "when": item.get("when") or item.get("occurred_at"),
                        "occurred_at": item.get("occurred_at"),
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
        if recency_first and is_keep_recall_query(query):
            visual_items = [
                item for item in visual_items if _keepish_evidence(item)
            ]
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
            from app.memory.room import looks_like_object_locate, spoken_object_locate

            if looks_like_object_locate(query):
                return spoken_object_locate(query, ranked[0])[:400]
            line = str(ranked[0].get("text") or "").strip()
            if line:
                return line[:800]
        if is_keep_recall_query(query):
            return "I cannot find that particular record."
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
    has_connected = any(
        str(item.get("kind") or "") == "live_mac"
        or str(item.get("memory_type") or "").startswith("message.")
        or str(item.get("memory_type") or "")
        in {
            "call.history.recorded",
            "photo.library.indexed",
            "mail.envelope.received",
            "contact.discovered",
            "contact.updated",
        }
        for item in evidence
    )
    if person_chat or has_connected or other_live:
        from app.memory.mail_speak import SPOKEN_MAIL_CAP, SPOKEN_READOUT_CAP, is_mail_ask, is_mail_hit

        extra = _speak_other_live(query, other_live)
        if extra and (
            is_mail_ask(query)
            or (other_live and all(is_mail_hit(item) for item in other_live))
        ):
            cap = (
                SPOKEN_READOUT_CAP
                if extra.lower().startswith("reading it out")
                else SPOKEN_MAIL_CAP
            )
            return extra[:cap]
        if person_chat:
            spoken = _speak_person_chat(
                query, chat_items, thread_names, channel=_speak_channel(query, chat_items)
            )
            combined = " ".join(part for part in (spoken, extra) if part).strip()
            if combined:
                return combined[:520]
        else:
            from app.memory.message_speak import is_chat_hit, speak_messages

            chat_rows = [
                item
                for item in evidence
                if isinstance(item, dict) and is_chat_hit(item)
            ]
            spoken = speak_messages(query, chat_rows)
            if spoken and extra:
                from app.memory.life_archive.locate import life_channel

                if life_channel(query) is None:
                    # Mixed inbox ("any new notifications", "what did I miss"):
                    # chats alone drop the mail/calls half of the digest.
                    return f"{spoken} {extra}".strip()[:520]
            if spoken:
                return spoken[:520]
            if extra:
                return extra[:520]
        if live_now:
            return _spoken_empty_connected(query)
    if live_now:
        return _spoken_empty_connected(query)
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
    return _spoken_empty_connected(query)


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
    line_cap = 800 if is_visual_recall_query(query) or is_keep_recall_query(query) else 200
    from app.memory.visual import owner_memory_hit_text

    lines = [
        owner_memory_hit_text(item.get("text"), item if isinstance(item, dict) else None)[:line_cap]
        for item in evidence[:8]
        if str(item.get("text") or item.get("description") or "").strip()
    ]
    return _json_ready({
        "ok": bool(evidence),
        "intent": "explicit_recall" if intent != "fresh" else intent,
        "question": (query or "")[:240],
        "count": len(evidence),
        "lines": [line for line in lines if line],
        "evidence": evidence,
        "results": [
            {
                "id": item.get("id"),
                "text": owner_memory_hit_text(
                    item.get("text"), item if isinstance(item, dict) else None
                ),
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
            if row.get("kind") in {"life", "live_life", "live_mac", "event"}
        ],
        "degraded": False,
        "grounding": "evidence" if evidence else "no_reliable_record",
        "spoken": (
            _spoken_from_evidence(evidence, query)
            if evidence
            else _spoken_empty_connected(query)
        ),
        "elapsed_ms": packed_ms,
        "facet": facet_mode,
        "life_shelf": shelf,
        "open_loops": extra.get("open_loops") or [],
        "decisions": extra.get("decisions") or [],
        "changes": extra.get("changes"),
        "project_state": extra.get("project_state"),
    })


async def _apply_life_spoken(session: AsyncSession, query: str, pack: dict) -> dict:
    """Twin rewind and leave-X misses get a human sentence, not a hedge."""

    from app.ev.edith import looks_like_twin_query, spoken_twin
    from app.memory.life_archive.locate import is_chat_summary_query
    from app.memory.life_archive.sessions import summarize_chat_for_query
    from app.memory.room import looks_like_object_locate, spoken_object_locate

    if looks_like_twin_query(query):
        line = await spoken_twin(session, query)
        if line:
            pack["spoken"] = line
            pack["twin"] = True
        return pack
    from app.memory.life_archive.desk import answer_desk_query, is_chat_desk_query

    if is_chat_desk_query(query):
        result = await answer_desk_query(session, query)
        if result and result.get("spoken"):
            evidence = result["evidence"]
            pack["spoken"] = result["spoken"]
            pack["evidence"] = evidence
            pack["results"] = [
                {
                    "id": item.get("id"),
                    "text": item.get("text"),
                    "memory_type": item.get("memory_type") or item.get("kind"),
                    "score": item.get("score"),
                    "date": item.get("when"),
                    "provenance": item.get("provenance") or [],
                }
                for item in evidence
            ]
            pack["lines"] = [str(result["spoken"])[:200]]
            pack["count"] = len(evidence)
            pack["ok"] = True
            pack["grounding"] = "evidence"
            pack["life_shelf"] = "chats"
        return pack
    from app.memory.life_archive.talk import answer_talk_query, is_talk_pattern_query

    if is_talk_pattern_query(query):
        result = await answer_talk_query(session, query)
        if result and result.get("spoken"):
            evidence = result["evidence"]
            pack["spoken"] = result["spoken"]
            pack["evidence"] = evidence
            pack["results"] = [
                {
                    "id": item.get("id"),
                    "text": item.get("text"),
                    "memory_type": item.get("memory_type") or item.get("kind"),
                    "score": item.get("score"),
                    "date": item.get("when"),
                    "provenance": item.get("provenance") or [],
                }
                for item in evidence
            ]
            pack["lines"] = [str(result["spoken"])[:200]]
            pack["count"] = len(evidence)
            pack["ok"] = True
            pack["grounding"] = "evidence"
            pack["life_shelf"] = "chats"
        return pack
    if is_chat_summary_query(query):
        result = await summarize_chat_for_query(session, query)
        if result and result.get("spoken"):
            evidence = result["evidence"]
            pack["spoken"] = result["spoken"]
            pack["evidence"] = evidence
            pack["results"] = [
                {
                    "id": item.get("id"),
                    "text": item.get("text"),
                    "memory_type": item.get("memory_type") or item.get("kind"),
                    "score": item.get("score"),
                    "date": item.get("when"),
                    "provenance": item.get("provenance") or [],
                }
                for item in evidence
            ]
            pack["lines"] = [str(result["spoken"])[:200]]
            pack["count"] = len(evidence)
            pack["ok"] = True
            pack["grounding"] = "evidence"
            pack["life_shelf"] = "chats"
            return pack
        # No session gist yet — fall through and speak a header+about line.
    if looks_like_object_locate(query):
        spoken = str(pack.get("spoken") or "")
        lowered = spoken.lower()
        if not spoken or "cannot find that particular record" in lowered:
            pack["spoken"] = spoken_object_locate(query, None)
            pack["ok"] = False
            return pack

    from app.ev.spark_task import bind_decision, decide_task, reset_decision
    from app.memory.life_archive.locate import life_channel

    shelf = str(pack.get("life_shelf") or "")
    channel = life_channel(query)
    if shelf in {"mail", "chats", "calls", "contacts", "calendar", "inbox"} or channel:
        decision = await decide_task(query, family_hint=shelf or (channel or ""))
        token = bind_decision(decision)
        try:
            pack["task_decision"] = decision.as_dict()
            evidence = [item for item in (pack.get("evidence") or []) if isinstance(item, dict)]
            if shelf == "inbox":
                # Mixed inbox ("any new notifications", "what did I miss")
                # spans chats + mail + calls. Never let the mail family bias
                # drop the chat/call half of the digest.
                spoken = _spoken_from_evidence(evidence, query)
                if spoken:
                    pack["spoken"] = spoken
            elif decision.family == "mail" or shelf == "mail" or channel == "mail":
                from app.memory.mail_speak import fill_readout, is_mail_hit, speak_mail

                rows = [item for item in evidence if is_mail_hit(item)] or evidence
                if decision.manner == "readout" and rows:
                    fill_readout(rows[0])
                spoken = speak_mail(query, rows, decision=decision)
                if spoken:
                    pack["spoken"] = spoken
            elif decision.family == "messages" or shelf == "chats" or channel in {
                "imessage",
                "whatsapp",
            }:
                from app.memory.life_archive.locate import is_chat_with_other_person
                from app.memory.message_speak import speak_messages

                if is_chat_with_other_person(query) and decision.manner != "readout":
                    spoken = _spoken_from_evidence(evidence, query)
                else:
                    spoken = speak_messages(query, evidence, decision=decision)
                    if not spoken:
                        spoken = _spoken_from_evidence(evidence, query)
                if spoken:
                    pack["spoken"] = spoken
            else:
                spoken = _spoken_from_evidence(evidence, query)
                if spoken:
                    pack["spoken"] = spoken
        finally:
            reset_decision(token)
    return pack


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
        from app.ev.edith import looks_like_twin_query

        if looks_like_twin_query(query):
            shelf = None
        archive = await locate_archive(session, query, shelf=shelf, k=min(MAX_ARCHIVE_HITS, k))
        log_memory(
            "memory.life_locate",
            extra={"shelf": shelf or "none", "hits": len(archive)},
        )
        if shelf is not None:
            # One drawer, one small pack. Do not scan chat/memories/neighbors.
            return await _apply_life_spoken(
                session,
                query,
                _finish_explicit_recall(
                    query=query,
                    intent=intent,
                    evidence=archive[: max(1, min(k, MAX_ARCHIVE_HITS))],
                    started=started,
                    shelf=shelf,
                    timeline_rows=archive,
                    facet_mode=temporal.mode,
                    semantic_ms=0,
                ),
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
        return await _apply_life_spoken(
            session,
            query,
            _finish_explicit_recall(
                query=query,
                intent=intent,
                evidence=evidence,
                started=started,
                shelf=None,
                timeline_rows=events,
                facet_mode=temporal.mode,
                semantic_ms=semantic_ms,
                facet=facet,
            ),
        )
    except Exception:  # noqa: BLE001 - tool must not crash the live turn
        logger.exception("explicit_recall_failed")
        log_memory("memory.degraded", extra={"error": "explicit_recall_failed"})
        from app.memory.room import looks_like_object_locate, spoken_object_locate

        spoken = "I cannot find that particular record."
        if looks_like_object_locate(query):
            spoken = spoken_object_locate(query, None)
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
            "spoken": spoken,
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
    recency_keep = is_keep_recall_query(query) and keep_topic(query) in {
        "",
        "this",
        "that",
        "it",
        "you",
    }
    if recency_keep:
        keep_rows = [item for item in merged if _keepish_evidence(item)]
        if keep_rows:
            keep_rows.sort(
                key=lambda item: -_when_epoch(item.get("when") or item.get("occurred_at"))
            )
            ordered = keep_rows[:1]
        else:
            ordered = owner_first + rest
    elif is_visual_recall_query(query):
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
            key=lambda item: _visual_item_rank(
                item,
                recency_first=True,
                topic=keep_topic(query),
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
                "reason": item.get("reason"),
                "recall": item.get("recall"),
                "description": item.get("description"),
                "object": item.get("object"),
                "keep_request": item.get("keep_request"),
                "attachment_id": item.get("attachment_id"),
                "labels": item.get("labels"),
                "colors": item.get("colors"),
                "ocr_text": item.get("ocr_text"),
                "printed": item.get("printed"),
                "surface": item.get("surface"),
                "placement": item.get("placement"),
                "occurred_at": item.get("occurred_at"),
            }
        )
        if len(packed) >= k:
            break
    return packed
