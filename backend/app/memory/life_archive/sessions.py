"""On-demand chat session summaries. Never automatic, never a full-log dump.

Asked summaries cluster a day's messages into sessions, then speak a compact
paragraph in Evie's own words: topic, what the owner said, what the other
person said, how it landed. Message recitation stays on last-chat.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.life_archive.classify import DEFAULT_ARCHIVE_ROOT
from app.memory.life_archive.locate import (
    SOURCE,
    _chat_person_query_token,
    is_chat_summary_query,
    token_variants,
)
from app.memory.life_archive.parse import read_whatsapp_messages
from app.memory.paths import atomic_write_json, ensure_tree, read_json
from app.models import Event

SESSION_GAP = timedelta(hours=3)
_SUMMARY_CHARS = 520
_REWRITE_SECONDS = 20.0
_STOP = frozenset(
    {
        "about",
        "after",
        "and",
        "are",
        "from",
        "have",
        "just",
        "like",
        "that",
        "the",
        "this",
        "with",
        "you",
        "your",
    }
)
_SIGNAL = re.compile(
    r"\b("
    r"come|call|meet|meeting|tomorrow|tonight|today|send|sent|where|when|"
    r"reach|leave|home|wait|notes|standup|project|office|dinner|plan|"
    r"later|before|after"
    r")\b",
    re.IGNORECASE,
)
_FILLER = frozenset(
    {
        "ok",
        "okay",
        "haan",
        "haa",
        "hmm",
        "hmmm",
        "yes",
        "no",
        "yeah",
        "yep",
        "lol",
        "haha",
        "acha",
        "accha",
        "theek",
        "thik",
        "okayy",
        "okey",
        "done",
        "cool",
        "fine",
    }
)
_COMMIT = re.compile(
    r"\b(i['’]ll|i will|i can|i am going|i'm going|sent|send|coming|on my way|"
    r"reach|leaving|leave|done|confirmed)\b",
    re.IGNORECASE,
)
_TOPIC_LABEL = {
    "call": "a call",
    "dinner": "dinner",
    "home": "getting home",
    "meet": "meeting up",
    "meeting": "a meeting",
    "notes": "notes",
    "office": "the office",
    "plan": "plans",
    "project": "the project",
    "send": "sending something",
    "sent": "something that went out",
    "standup": "standup",
    "tomorrow": "tomorrow",
    "tonight": "tonight",
    "wait": "waiting",
}


def cluster_sessions(
    messages: list[dict[str, Any]],
    *,
    gap: timedelta = SESSION_GAP,
    clock: datetime | None = None,
) -> list[list[dict[str, Any]]]:
    """Split a day's messages into sessions on a long quiet gap."""

    zone_clock = clock or datetime.now(UTC)
    ordered = [
        item
        for item in sorted(
            messages,
            key=lambda row: _item_moment(row, zone_clock),
        )
        if item.get("when") is not None
    ]
    if not ordered:
        return []
    sessions: list[list[dict[str, Any]]] = [[ordered[0]]]
    for item in ordered[1:]:
        prev = _item_moment(sessions[-1][-1], zone_clock)
        current = _item_moment(item, zone_clock)
        if current - prev >= gap:
            sessions.append([item])
        else:
            sessions[-1].append(item)
    return sessions


def day_from_query(query: str, *, now: datetime) -> date | None:
    blob = (query or "").lower()
    today = now.date()
    if "yesterday" in blob or "last night" in blob:
        return today - timedelta(days=1)
    if "today" in blob or "tonight" in blob:
        return today
    return None


def _item_moment(item: dict[str, Any], clock: datetime) -> datetime:
    """Owner-local instant. Takeout `wall` clocks stay as written."""

    when = item.get("when")
    if when is None:
        return clock
    if not isinstance(when, datetime):
        return clock
    zone = clock.tzinfo or UTC
    if bool(item.get("wall")) or when.tzinfo is None:
        naive = when.replace(tzinfo=None)
        return naive.replace(tzinfo=zone)
    return when.astimezone(zone)


def _part_of_day(value: datetime) -> str:
    hour = value.hour
    if 5 <= hour < 12:
        return "in the morning"
    if 12 <= hour < 17:
        return "in the afternoon"
    if 17 <= hour < 20:
        return "in the evening"
    return "at night"


def _speak_day(day: date, *, now: datetime) -> str:
    today = now.date()
    if day == today:
        return "today"
    if day == today - timedelta(days=1):
        return "yesterday"
    return "on " + day.strftime("%B") + f" {day.day}"


def _clip(body: str, limit: int = 72) -> str:
    text = " ".join(str(body or "").split()).strip().strip("\"'")
    if len(text) <= limit:
        return text
    clipped = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:")
    return (clipped or text[: limit - 1]).rstrip() + "…"


def _norm_body(item: dict[str, Any]) -> str:
    return " ".join(str(item.get("body") or "").split()).strip()


def _script_hint(text: str) -> str:
    if re.search(r"[\u0A80-\u0AFF]", text):
        return "Gujarati"
    if re.search(r"[\u0900-\u097F]", text):
        return "Hindi"
    return ""


def _flatten(sessions: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [item for block in sessions for item in block]


def _quoted_span(spoken: str, messages: list[dict[str, Any]]) -> str | None:
    """Return a copied message span if the spoken line is still reciting."""

    blob = (spoken or "").lower()
    if not blob:
        return None
    for item in messages:
        body = _norm_body(item)
        if len(body) < 18:
            continue
        lowered = body.lower()
        if lowered in blob:
            return body
        words = body.split()
        if len(words) < 6:
            continue
        for index in range(len(words) - 5):
            span = " ".join(words[index : index + 6]).lower()
            if span in blob:
                return span
    return None


def _session_evidence(
    session: list[dict[str, Any]],
    *,
    limit: int = 8,
    clock: datetime | None = None,
) -> list[dict[str, Any]]:
    """Spread + high-signal lines, always keeping the last ask and last owner line."""

    from app.memory.life_archive.parse import _spread_pick

    zone = clock or datetime.now(UTC)
    pool = [item for item in session if _substantial(item)] or list(session)
    if len(pool) <= limit:
        return sorted(pool, key=lambda row: _item_moment(row, zone))
    pins: list[dict[str, Any]] = []
    for item in reversed(session):
        if not item.get("owner") and "?" in _norm_body(item) and _substantial(item):
            pins.append(item)
            break
    for item in reversed(session):
        if item.get("owner") and _substantial(item):
            pins.append(item)
            break
    for item in session:
        if _substantial(item):
            pins.append(item)
            break

    ranked = sorted(pool, key=_score_message, reverse=True)[: max(3, limit // 2)]
    spread = _spread_pick(pool, limit)
    seen: set[str] = set()
    picked: list[dict[str, Any]] = []
    for item in pins + ranked + spread:
        key = _norm_body(item).lower()[:80]
        if not key or key in seen:
            continue
        seen.add(key)
        picked.append(item)
        if len(picked) >= limit:
            break
    return sorted(picked, key=lambda row: _item_moment(row, zone))


def _english_topics(messages: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    blob = " ".join(_norm_body(item) for item in messages)
    for match in _SIGNAL.finditer(blob):
        raw = match.group(1).lower()
        if raw not in _TOPIC_LABEL and raw in {
            "after",
            "before",
            "later",
            "when",
            "where",
            "today",
            "just",
            "leave",
            "reach",
            "wait",
        }:
            continue
        label = _TOPIC_LABEL.get(raw, raw)
        if label in seen:
            continue
        seen.add(label)
        labels.append(label)
        if len(labels) >= 4:
            return labels
    for token in _session_tokens(messages):
        if token in _TOPIC_LABEL or token in seen or len(token) < 5:
            continue
        if not re.fullmatch(r"[a-z]+", token):
            continue
        seen.add(token)
        labels.append(token)
        if len(labels) >= 4:
            break
    return labels


def _topic_phrase(topics: list[str]) -> str:
    if not topics:
        return "the day's chat"
    if len(topics) == 1:
        return topics[0]
    if len(topics) == 2:
        return f"{topics[0]} and {topics[1]}"
    return ", ".join(topics[:-1]) + f", and {topics[-1]}"


def _other_move(messages: list[dict[str, Any]], name: str, topics: list[str]) -> str:
    others = [item for item in messages if not item.get("owner") and _substantial(item)]
    topic = topics[0] if topics else "it"
    if any("?" in _norm_body(item) for item in others):
        return f"{name} asked about {topic}."
    if others:
        return f"{name} brought up {topic}."
    return ""


def _owner_move(messages: list[dict[str, Any]]) -> str:
    owners = [item for item in messages if item.get("owner") and _substantial(item)]
    blob = " ".join(_norm_body(item) for item in owners)
    if _COMMIT.search(blob):
        return "You said you'd take care of your side."
    if owners:
        return "You answered and kept it moving."
    return ""


def _ending_move(session: list[dict[str, Any]]) -> str:
    substantial = [item for item in session if _substantial(item)] or list(session)
    if not substantial:
        return "That's where you left it."
    last = substantial[-1]
    body = _norm_body(last).lower()
    if last.get("owner") and _COMMIT.search(body):
        return "You closed it by following through."
    if not last.get("owner") and "?" in body:
        return "It was still open at the end."
    if body in _FILLER or ((not last.get("owner")) and len(body) < 12):
        return "You both lined up on it."
    return "That's where you left it."


def _leftover(session: list[dict[str, Any]], name: str) -> str:
    """What was still hanging — never the wording of the last line."""

    substantial = [item for item in session if _substantial(item)]
    if not substantial:
        return ""
    last = substantial[-1]
    if last.get("owner"):
        return ""
    body = _norm_body(last)
    if "?" in body:
        return f"{name} still had a question hanging."
    if body.lower() in _FILLER or len(body) < 12:
        return ""
    return f"It was still open on {name}'s side."


def _day_leftover(sessions: list[list[dict[str, Any]]], name: str) -> str:
    bits = [_leftover(block, name) for block in sessions]
    return " ".join(bit for bit in bits if bit)


def _paraphrase_session(session: list[dict[str, Any]], name: str) -> str:
    evidence = _session_evidence(session)
    blob = " ".join(_norm_body(item) for item in evidence)
    hint = _script_hint(blob)
    topics = _english_topics(evidence)
    if not topics and hint:
        topics = [f"a {hint} conversation"]
    about = f"It was about {_topic_phrase(topics)}."
    leftover = _leftover(session, name)
    landing = leftover or _ending_move(session)
    parts = [about, _other_move(evidence, name, topics), _owner_move(evidence), landing]
    spoken = " ".join(part for part in parts if part)
    if _quoted_span(spoken, session):
        return f"It was about {_topic_phrase(topics)}. You and {name} each had a say, and that's where it landed."
    return spoken


def _session_lead(*, name: str, sessions: list[list[dict[str, Any]]], day: date, now: datetime) -> str:
    day_label = _speak_day(day, now=now)
    first_when = _item_moment(sessions[0][0], now)
    tod = _part_of_day(first_when)
    if len(sessions) == 1:
        return f"{day_label.capitalize()} you and {name} talked once, {tod}."
    times = [_part_of_day(_item_moment(block[0], now)) for block in sessions]
    return (
        f"{day_label.capitalize()} you and {name} talked {len(sessions)} times — "
        + ", then ".join(times)
        + "."
    )


def speak_session_summary(
    *,
    name: str,
    sessions: list[list[dict[str, Any]]],
    day: date,
    now: datetime,
) -> str:
    """Short spoken gist of one day's sessions. Not a transcript."""

    if not sessions or not name:
        return ""
    lead = _session_lead(name=name, sessions=sessions, day=day, now=now)
    parts = [lead]
    if len(sessions) == 1:
        parts.append(_paraphrase_session(sessions[0], name))
        spoken = " ".join(part for part in parts if part)
        return spoken[:_SUMMARY_CHARS]
    for index, block in enumerate(sessions):
        bit = _paraphrase_session(block, name)
        if not bit:
            continue
        if index == 0:
            parts.append("Earlier: " + bit)
            continue
        prefix = "Later: "
        if _sessions_linked(sessions[index - 1], block):
            prefix = "Later, continuing that: "
        parts.append(prefix + bit)
    return " ".join(part for part in parts if part)[:_SUMMARY_CHARS]


def _substantial(item: dict[str, Any]) -> bool:
    body = str(item.get("body") or "").strip()
    if len(body) < 12:
        return False
    if body.lower() in _FILLER:
        return False
    if body.lower().startswith("http"):
        return False
    return True


def _score_message(item: dict[str, Any]) -> float:
    body = str(item.get("body") or "")
    score = min(len(body), 160) / 40.0
    if "?" in body:
        score += 1.5
    if _SIGNAL.search(body):
        score += 2.0
    if item.get("owner"):
        score += 0.2
    return score


def _session_tokens(session: list[dict[str, Any]]) -> set[str]:
    tokens: set[str] = set()
    for item in session:
        body = str(item.get("body") or "")
        for raw in re.findall(
            r"[a-zA-Z][a-zA-Z']{3,}|[\u0900-\u097F]{3,}|[\u0A80-\u0AFF]{3,}",
            body.lower(),
        ):
            token = raw.replace("'", "")
            if token in _STOP or token in _FILLER:
                continue
            tokens.add(token)
    return tokens


def _sessions_linked(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> bool:
    overlap = _session_tokens(left) & _session_tokens(right)
    return len(overlap) >= 2


def messages_on_day(
    messages: list[dict[str, Any]],
    day: date,
    *,
    clock: datetime,
) -> list[dict[str, Any]]:
    return [
        item
        for item in messages
        if item.get("when") and _item_moment(item, clock).date() == day
    ]


def last_active_day(messages: list[dict[str, Any]], *, clock: datetime) -> date | None:
    dates = [_item_moment(item, clock).date() for item in messages if item.get("when")]
    return max(dates) if dates else None


def _clock_for(now: datetime | None) -> datetime:
    if now is None:
        try:
            from app.ev.resolve import owner_now

            clock = owner_now()
        except Exception:
            clock = datetime.now(UTC)
    else:
        clock = now
    if clock.tzinfo is None:
        return clock.replace(tzinfo=UTC)
    return clock


def plan_day_sessions(
    messages: list[dict[str, Any]],
    query: str,
    *,
    now: datetime | None = None,
) -> tuple[datetime, date | None, list[list[dict[str, Any]]]]:
    clock = _clock_for(now)
    wanted = day_from_query(query, now=clock)
    last = last_active_day(messages, clock=clock)
    if wanted is None:
        wanted = last
    if wanted is None:
        return clock, None, []
    day_messages = messages_on_day(messages, wanted, clock=clock)
    if not day_messages and last is not None:
        wanted = last
        day_messages = messages_on_day(messages, last, clock=clock)
    return clock, wanted, cluster_sessions(day_messages, clock=clock)


def build_session_summary(
    *,
    name: str,
    messages: list[dict[str, Any]],
    query: str,
    now: datetime | None = None,
) -> str:
    clock, wanted, sessions = plan_day_sessions(messages, query, now=now)
    if wanted is None or not sessions:
        return ""
    return speak_session_summary(name=name, sessions=sessions, day=wanted, now=clock)


def _evidence_prompt(
    name: str,
    sessions: list[list[dict[str, Any]]],
    *,
    clock: datetime,
) -> str:
    lines: list[str] = []
    for index, block in enumerate(sessions[:4], start=1):
        stamp = _part_of_day(_item_moment(block[0], clock))
        lines.append(f"Session {index} ({stamp}):")
        for item in _session_evidence(block, limit=8, clock=clock):
            who = "Sahaj" if item.get("owner") else name
            lines.append(f"{who}: {_clip(_norm_body(item), 100)}")
    leftover = _day_leftover(sessions, name)
    if leftover:
        lines.append(f"Leftover: {leftover}")
    return "\n".join(lines)


def _clean_draft(text: str) -> str:
    cleaned = " ".join(str(text or "").split()).strip().strip("\"'")
    cleaned = re.sub(r"^(summary|recap)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned


_REWRITE_SYSTEM = (
    "You are Evie, speaking out loud to Sahaj about one WhatsApp thread. "
    "Write one compact spoken paragraph in Evie's own words. "
    "Do not quote, read, or list the messages. No transcript. No 'they said:' recitation. Paraphrase. "
    "English for speech even if the chat is Gujarati, Hindi, Hinglish, or mixed. "
    "Cover: what it was about, what Sahaj said, what the other person said, how it landed "
    "(agreed, still open, a plan, leftover). "
    "If a question or promised send was left hanging, say that last — never the wording. "
    "If there are multiple sessions, weave them and say when a later one continued the earlier. "
    "One or two short sentences. No camera, no extra people, no invented facts."
)


async def _rewrite_with_model(
    *,
    name: str,
    sessions: list[list[dict[str, Any]]],
    lead: str,
    clock: datetime,
) -> str | None:
    """Ask Spark (or the live chat model) for an abstractive paragraph."""

    from app.config import settings
    from app.contracts import ChatMessage
    from app.gateway.muse import (
        MuseProviderUnavailable,
        muse_spark_key_loaded,
        muse_spark_model,
    )

    blob = " ".join(_norm_body(item) for item in _flatten(sessions))
    hint = _script_hint(blob)
    lang = f" The chat mixes {hint}." if hint else ""
    leftover = _day_leftover(sessions, name)
    hang = f" Leftover to mention: {leftover}." if leftover else ""
    prompt = (
        f"{lead}{lang}{hang}\nThe other person is {name}. Messages (Sahaj is the owner):\n"
        f"{_evidence_prompt(name, sessions, clock=clock)}"
    )
    messages = [
        ChatMessage(role="system", content=_REWRITE_SYSTEM),
        ChatMessage(role="user", content=prompt),
    ]

    async def _chat(
        provider: Any,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> str | None:
        kwargs: dict[str, Any] = {}
        if model:
            kwargs["model"] = model
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        result = await asyncio.wait_for(provider.chat(messages, **kwargs), timeout=_REWRITE_SECONDS)
        drafted = _clean_draft(getattr(result, "text", None) or "")
        if len(drafted) < 24:
            return None
        return drafted[:_SUMMARY_CHARS]

    if muse_spark_key_loaded():
        try:
            from app.gateway.muse_spark import muse_spark_provider

            drafted = await _chat(
                muse_spark_provider(),
                model=muse_spark_model(),
                reasoning_effort="low",
            )
            if drafted:
                return drafted
        except (MuseProviderUnavailable, TimeoutError, asyncio.TimeoutError, Exception):
            pass
    provider_name = (getattr(settings, "chat_provider", None) or "").strip().lower()
    if provider_name in {"", "echo", "mock"}:
        return None
    try:
        from app.gateway.providers import get_chat_provider

        return await _chat(get_chat_provider())
    except (MuseProviderUnavailable, TimeoutError, asyncio.TimeoutError, Exception):
        return None


def _cache_path(name: str, day: date) -> Path:
    safe = re.sub(r"[^a-z0-9]+", "", (name or "").lower())[:32] or "chat"
    return ensure_tree() / "cache" / "chat-session" / f"{safe}-{day.isoformat()}.json"


def _session_fingerprint(name: str, day: date, sessions: list[list[dict[str, Any]]]) -> str:
    hasher = hashlib.sha256()
    hasher.update((name or "").lower().encode("utf-8"))
    hasher.update(day.isoformat().encode("utf-8"))
    for item in _flatten(sessions):
        hasher.update(b"1" if item.get("owner") else b"0")
        hasher.update(b"w" if item.get("wall") else b"u")
        when = item.get("when")
        stamp = when.isoformat() if hasattr(when, "isoformat") else str(when or "")
        hasher.update(stamp.encode("utf-8"))
        hasher.update(_norm_body(item).encode("utf-8", errors="ignore"))
        hasher.update(b"\n")
    return hasher.hexdigest()[:32]


def _cached_spoken(name: str, day: date, fingerprint: str) -> str | None:
    row = read_json(_cache_path(name, day)) or {}
    if str(row.get("fp") or "") != fingerprint:
        return None
    spoken = str(row.get("spoken") or "").strip()
    return spoken or None


def _store_spoken(name: str, day: date, fingerprint: str, spoken: str) -> None:
    atomic_write_json(
        _cache_path(name, day),
        {"fp": fingerprint, "spoken": spoken, "day": day.isoformat()},
    )


async def compose_session_summary(
    *,
    name: str,
    messages: list[dict[str, Any]],
    query: str,
    now: datetime | None = None,
) -> str:
    """Paraphrased spoken summary. Tries Spark, never recites the log."""

    clock, wanted, sessions = plan_day_sessions(messages, query, now=now)
    if wanted is None or not sessions:
        return ""
    flat = _flatten(sessions)
    local = speak_session_summary(name=name, sessions=sessions, day=wanted, now=clock)
    fingerprint = _session_fingerprint(name, wanted, sessions)
    cached = _cached_spoken(name, wanted, fingerprint)
    if cached and not _quoted_span(cached, flat):
        return cached[:_SUMMARY_CHARS]
    lead = _session_lead(name=name, sessions=sessions, day=wanted, now=clock)
    drafted = await _rewrite_with_model(
        name=name, sessions=sessions, lead=lead, clock=clock
    )
    spoken = local
    if drafted and not _quoted_span(drafted, flat):
        if lead.lower()[:18] not in drafted.lower():
            drafted = f"{lead} {drafted}"
        spoken = drafted[:_SUMMARY_CHARS]
    if spoken and not _quoted_span(spoken, flat):
        _store_spoken(name, wanted, fingerprint, spoken)
    return spoken


def _archive_root() -> Path:
    catalog = read_json(ensure_tree() / "catalog" / "last-dry-run.summary.json") or {}
    raw = str(catalog.get("archive_root") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if path.is_dir():
            return path
    default = DEFAULT_ARCHIVE_ROOT.expanduser()
    return default


def _display_name(token: str, title: str) -> str:
    raw = (title or token or "").strip()
    if raw:
        return raw[:1].upper() + raw[1:] if raw[:1].islower() else raw
    return token[:1].upper() + token[1:] if token else ""


async def _thread_for_person(session: AsyncSession, token: str) -> Event | None:
    variants = {item for item in (token_variants(token) | {token}) if len(item) >= 2}
    rows = list(
        (
            await session.execute(
                select(Event)
                .where(
                    Event.source == SOURCE,
                    Event.event_type == "life.chat.thread",
                    Event.tombstoned_at.is_(None),
                    Event.privacy_level != "never_send_to_model",
                )
                .order_by(Event.occurred_at.desc())
                .limit(200)
            )
        ).scalars().all()
    )
    for event in rows:
        content = event.content if isinstance(event.content, dict) else {}
        title = str(content.get("title") or content.get("name") or "").strip().lower()
        blob = " ".join(
            str(part or "")
            for part in (
                content.get("text"),
                content.get("title"),
                content.get("name"),
                content.get("ref"),
            )
        ).lower()
        words = set(re.findall(r"[a-z]+", blob))
        if any(variant == title or variant in words for variant in variants):
            return event
    return None


def _zip_path_for_thread(event: Event, *, root: Path | None = None) -> Path | None:
    content = event.content if isinstance(event.content, dict) else {}
    meta = event.metadata_ if isinstance(event.metadata_, dict) else {}
    rel = str(content.get("ref") or meta.get("ref") or "").strip()
    if not rel:
        return None
    bases = [root] if root is not None else [_archive_root(), DEFAULT_ARCHIVE_ROOT.expanduser()]
    for base in bases:
        if base is None:
            continue
        path = (Path(base) / rel).expanduser()
        if path.is_file():
            return path
    return None


def _merge_messages(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, bool]] = set()
    for group in groups:
        for item in group:
            when = item.get("when")
            stamp = when.isoformat() if hasattr(when, "isoformat") else str(when or "")
            body = str(item.get("body") or "").strip()[:80].lower()
            key = (stamp, body, bool(item.get("owner")))
            if not body or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


async def _live_whatsapp_messages(session: AsyncSession, token: str) -> list[dict[str, Any]]:
    """Recent live WhatsApp envelopes whose handle is that person. Ask-only."""

    if len(token) < 2:
        return []
    rows = list(
        (
            await session.execute(
                select(Event)
                .where(
                    Event.source == "whatsapp",
                    Event.event_type.in_(
                        ("message.whatsapp.received", "message.whatsapp.sent")
                    ),
                    Event.tombstoned_at.is_(None),
                    Event.privacy_level != "never_send_to_model",
                )
                .order_by(Event.occurred_at.desc())
                .limit(2000)
            )
        ).scalars().all()
    )
    out: list[dict[str, Any]] = []
    compact_token = re.sub(r"[^a-z]+", "", token)
    for event in rows:
        content = event.content if isinstance(event.content, dict) else {}
        handle = str(content.get("handle") or "").strip()
        compact_handle = re.sub(r"[^a-z]+", "", handle.lower())
        if compact_token not in compact_handle:
            continue
        body = str(content.get("text") or "").strip()
        if not body:
            continue
        out.append(
            {
                "sender": handle or token,
                "body": body,
                "owner": bool(content.get("is_from_me")),
                "when": event.occurred_at,
            }
        )
    return _merge_messages(out, _mac_whatsapp_messages(token))


def _mac_whatsapp_messages(token: str) -> list[dict[str, Any]]:
    """Desktop WhatsApp copy for that person. Empty in CI."""

    try:
        from app.services.life_stream_daemon import (
            get_life_stream_daemon,
            life_stream_should_run,
        )

        if not life_stream_should_run():
            return []
        return get_life_stream_daemon().read_whatsapp_person(token)
    except Exception:
        return []


async def summarize_chat_for_query(
    session: AsyncSession,
    query: str,
    *,
    root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Load that person's takeout + live chat and summarize the asked day. Ask-only."""

    if not is_chat_summary_query(query):
        return None
    token = _chat_person_query_token(query)
    if not token:
        return None
    thread = await _thread_for_person(session, token)
    title = ""
    if thread is not None:
        content = thread.content if isinstance(thread.content, dict) else {}
        title = str(content.get("title") or content.get("name") or "").strip()
    live = await _live_whatsapp_messages(session, token)
    if not title:
        for item in live:
            if not item.get("owner") and str(item.get("sender") or "").strip():
                title = str(item.get("sender") or "").strip()
                break
    name = _display_name(token, title)
    messages: list[dict[str, Any]] = []
    if thread is not None:
        path = _zip_path_for_thread(thread, root=root)
        if path is not None:
            messages = read_whatsapp_messages(path)
    messages = _merge_messages(messages, live)
    if not messages:
        return None
    spoken = await compose_session_summary(
        name=name, messages=messages, query=query, now=now
    )
    if not spoken:
        return None
    when = None
    dated = [item.get("when") for item in messages if item.get("when")]
    if dated:
        when = max(dated)
        if hasattr(when, "isoformat"):
            when = when.isoformat()
    return {
        "spoken": spoken,
        "evidence": [
            {
                "id": str(thread.id) if thread is not None else "live-whatsapp",
                "source": SOURCE,
                "when": when,
                "text": spoken,
                "kind": "life",
                "memory_type": "life.chat.session",
                "confidence": "session_summary",
                "shelf": "chats",
            }
        ],
    }
