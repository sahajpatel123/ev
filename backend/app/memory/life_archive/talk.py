"""Talk-pattern meaning: who the owner messages, ranked from evidence.

MAC HUB ONLY. WhatsApp Desktop + iMessage on this Mac. Do not edit this
file for iPhone / PWA / device-gateway work.

Everyday 'who do I talk to most' is who they actually message these days
(recent density and last activity), not lifetime archive volume. All-time
volume is only spoken when they asked for history. Names are never invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.life_archive.locate import SOURCE, _chat_person_query_token
from app.memory.life_archive.desk import _ago, _compact, _skip_partner
from app.models import Event
from app.utils.text import utcnow

TalkAxis = Literal["volume", "recency", "roster", "active"]
_SPOKEN_CAP = 520
_SCAN = 80
_STALE = timedelta(days=45)
_NEAR_LEADER = timedelta(days=7)

_THREAD_LINE = re.compile(
    r"WhatsApp thread:\s*(?P<name>[^.]+)\.\s*(?P<n>\d+)\s+messages",
    re.IGNORECASE,
)
_PERSON_LINE = re.compile(
    r"Person:\s*(?P<name>[^(.\n]+).{0,80}?(?P<n>\d+)\s+messages",
    re.IGNORECASE,
)
_COMM = (
    r"(?:talk(?:ed|ing)?|text(?:ed|ing)?|chat(?:ted|ting)?|"
    r"message(?:d|s|ing)?|whatsapp)"
)
_WHO_GRAPH = re.compile(
    rf"\bwho\s+(?:have\s+i|do\s+i|i(?:'ve| have)|am\s+i)\s+"
    rf"(?:been\s+)?{_COMM}",
    re.IGNORECASE,
)
_PEOPLE_GRAPH = re.compile(
    rf"\bpeople\s+i\s+(?:{_COMM}|usually|mostly|often)",
    re.IGNORECASE,
)
_RANK_GRAPH = re.compile(
    rf"\b(?:most|often|usually|frequent(?:ly)?|recent(?:ly)?|latest|lately|"
    rf"the\s+most|least)\b.{{0,48}}\b{_COMM}\b|"
    rf"\b{_COMM}\b.{{0,48}}\b(?:most|often|usually|the\s+most|recently|lately)\b",
    re.IGNORECASE,
)
_VOLUME_CUE = re.compile(
    r"\b(?:most|often|usually|frequent(?:ly)?|a lot|the most|the hardest|"
    r"the longest)\b",
    re.IGNORECASE,
)
_RECENCY_CUE = re.compile(
    r"\b(?:recent(?:ly)?|latest|lately|newest|just now|last(?:ly)?)\b",
    re.IGNORECASE,
)
_ROSTER_CUE = re.compile(
    r"\b(?:who all|which people|people i|different people|everyone i)\b",
    re.IGNORECASE,
)
_MOST_RECENT = re.compile(r"\bmost\s+recent(?:ly)?\b", re.IGNORECASE)
_ALL_TIME = re.compile(
    r"\b(?:ever|all[- ]time|lifetime|histor(?:y|ically)|biggest(?:\s+thread|\s+chat)?|"
    r"the longest)\b",
    re.IGNORECASE,
)
_DIGIT_HANDLE = re.compile(r"^[+\d][\d\s\-()]{6,}$")
_MIN = datetime.min.replace(tzinfo=UTC)
_RANK_WORDS = frozenset(
    {"most", "least", "recently", "lately", "often", "usually", "everyone", "anybody", "today"}
)


@dataclass
class TalkPerson:
    name: str
    volume: int = 0
    recent: int = 0
    last_at: datetime | None = None
    channels: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return _compact(self.name)


@dataclass
class TalkBoard:
    people: list[TalkPerson] = field(default_factory=list)


def is_talk_pattern_query(query: str) -> bool:
    """True when they asked about the talk *graph*, not one named thread."""

    raw = (query or "").strip()
    if not raw:
        return False
    from app.memory.life_archive.locate import (
        _ACT_NOW,
        _CURRENT_TALK,
        _SEND_NOW,
        is_chat_summary_query,
        is_chat_with_other_person,
    )
    from app.memory.life_archive.desk import is_chat_desk_query

    if _SEND_NOW.search(raw) or _ACT_NOW.search(raw):
        return False
    if is_chat_desk_query(raw) or is_chat_summary_query(raw):
        return False
    if _CURRENT_TALK.search(raw):
        return False
    token = _chat_person_query_token(raw)
    if token and token not in _RANK_WORDS:
        return False
    if _WHO_GRAPH.search(raw) or _PEOPLE_GRAPH.search(raw) or _RANK_GRAPH.search(raw):
        return True
    from app.memory.life_archive.locate import _CONVERSATION_AISLE, _WHO_I_TALK

    if _WHO_I_TALK.search(raw):
        return True
    if _CONVERSATION_AISLE.search(raw) and not is_chat_with_other_person(raw):
        return True
    return False


def axes_for(query: str) -> frozenset[TalkAxis]:
    """Which facts the question asked for.

    'Most' in ordinary speech means who they talk with these days. Lifetime
    volume only when they asked for history (ever / all time / biggest).
    """

    raw = query or ""
    if _ALL_TIME.search(raw):
        return frozenset({"volume"})
    if _MOST_RECENT.search(raw):
        return frozenset({"recency"})
    volume = bool(_VOLUME_CUE.search(raw))
    recency = bool(_RECENCY_CUE.search(raw))
    roster = bool(_ROSTER_CUE.search(raw))
    if recency and not volume:
        return frozenset({"recency"})
    if volume:
        return frozenset({"active"})
    if roster:
        return frozenset({"roster"})
    return frozenset({"active"})


async def answer_talk_query(session: AsyncSession, query: str) -> dict[str, Any] | None:
    if not is_talk_pattern_query(query):
        return None
    board = await build_talk_board(session)
    spoken = speak_board(board, axes_for(query))
    if not spoken:
        return None
    clock = utcnow()
    lead = board.people[0] if board.people else None
    stamp = lead.last_at if lead is not None else clock
    if hasattr(stamp, "isoformat"):
        when = stamp.isoformat()
    else:
        when = None
    return {
        "spoken": spoken[:_SPOKEN_CAP],
        "evidence": [
            {
                "id": "chat-talk",
                "source": SOURCE,
                "when": when,
                "text": spoken[:_SPOKEN_CAP],
                "kind": "life",
                "memory_type": "life.chat.talk",
                "confidence": "talk_pattern",
                "score": 1.0,
                "provenance": ["talk_board"],
                "shelf": "chats",
            }
        ],
    }


async def build_talk_board(session: AsyncSession) -> TalkBoard:
    rows = (
        await session.execute(
            select(Event)
            .where(
                Event.source == SOURCE,
                Event.event_type == "life.chat.thread",
                Event.tombstoned_at.is_(None),
            )
            .order_by(Event.occurred_at.desc())
            .limit(_SCAN)
        )
    ).scalars().all()
    people: dict[str, TalkPerson] = {}
    for event in rows:
        parsed = _from_thread_event(event)
        if parsed is None:
            continue
        _merge(people, parsed)
    _merge_live(people)
    now = utcnow()
    ranked = _active_ranked(list(people.values()), now) or sorted(
        people.values(), key=lambda item: (-item.volume, item.name.lower())
    )
    return TalkBoard(people=ranked)


def speak_board(board: TalkBoard, axes: frozenset[str], *, clock: datetime | None = None) -> str:
    people = [item for item in board.people if item.name]
    if not people:
        return "I don't have a reliable record of who you message."
    now = clock or utcnow()
    by_volume = sorted(people, key=lambda item: (-item.volume, item.name.lower()))
    by_recent = sorted(
        people,
        key=lambda item: (item.last_at or _MIN, item.name.lower()),
        reverse=True,
    )
    vol_lead = by_volume[0]
    rec_lead = by_recent[0]
    active = _active_ranked(people, now)
    want_volume = "volume" in axes
    want_recency = "recency" in axes
    want_active = "active" in axes or "roster" in axes

    parts: list[str] = []
    if want_volume and not want_active and not want_recency:
        parts.append(_volume_sentence(by_volume, _channel_label(vol_lead)))
        if vol_lead.key != rec_lead.key:
            parts.append(_recency_aside(rec_lead, now))
    elif want_recency and not want_active and not want_volume:
        parts.append(_recency_sentence(rec_lead, _channel_label(rec_lead), now))
    elif want_active:
        if not active:
            return "I don't see recent chats worth calling 'most' — the heavy threads look old."
        lead = active[0]
        if "roster" in axes and "active" not in axes:
            parts.append(_roster_sentence(active, _channel_label(lead)))
        else:
            parts.append(_active_sentence(active, _channel_label(lead)))
    else:
        lead_list = active or by_recent[:1]
        parts.append(_active_sentence(lead_list, _channel_label(lead_list[0])))
    spoken = " ".join(part for part in parts if part).strip()
    return spoken[:_SPOKEN_CAP]


def _from_thread_event(event: Event) -> TalkPerson | None:
    from app.memory.life_archive.locate import _safe_text

    content = event.content if isinstance(event.content, dict) else {}
    text = _safe_text(event)
    if content.get("group") or re.search(r"\bgroup chat\b", text or "", re.IGNORECASE):
        return None
    name = str(content.get("title") or "").strip()
    volume = int(content.get("messages") or 0)
    if not name:
        match = _THREAD_LINE.search(text or "") or _PERSON_LINE.search(text or "")
        if match:
            name = match.group("name").strip()
            volume = volume or int(match.group("n") or 0)
    if _skip_handle(name):
        return None
    channel = "whatsapp"
    kind = str(content.get("kind") or "").lower()
    if "imessage" in kind or "sms" in kind:
        channel = "imessage"
    return TalkPerson(
        name=name[:48],
        volume=max(volume, 1),
        last_at=event.occurred_at,
        channels={channel},
    )


def _merge_live(people: dict[str, TalkPerson]) -> None:
    from app.services.life_stream_daemon import get_life_stream_daemon, life_stream_should_run

    if not life_stream_should_run():
        return
    try:
        daemon = get_life_stream_daemon()
    except Exception:
        return
    hits: list[dict[str, Any]] = []
    try:
        hits.extend(daemon.peek_whatsapp(limit=48) or [])
        hits.extend(daemon.peek_imessage(limit=48) or [])
    except Exception:
        return
    for item in hits:
        if not isinstance(item, dict):
            continue
        name = _live_partner(item)
        if _skip_handle(name):
            continue
        when = item.get("when")
        moment = None
        if isinstance(when, datetime):
            moment = when
        elif isinstance(when, str) and when:
            try:
                moment = datetime.fromisoformat(when.replace("Z", "+00:00"))
            except ValueError:
                moment = None
        channel = str(item.get("channel") or item.get("source") or "whatsapp").lower()
        if channel not in {"whatsapp", "imessage", "mail"}:
            channel = "whatsapp" if "whatsapp" in channel else "imessage"
        parsed = TalkPerson(name=name[:48], volume=1, recent=1, last_at=moment, channels={channel})
        _merge(people, parsed, live=True)


def _live_partner(item: dict[str, Any]) -> str:
    handle = str(item.get("handle") or item.get("title") or "").strip()
    if handle and handle.lower() not in {"you", "owner", "me", "someone"}:
        return handle
    text = str(item.get("text") or "")
    if ":" in text:
        who = text.split(":", 1)[0].strip()
        if who and who.lower() not in {"you", "owner", "me"}:
            return who
    return handle


def _merge(people: dict[str, TalkPerson], incoming: TalkPerson, *, live: bool = False) -> None:
    key = incoming.key
    if not key:
        return
    existing = people.get(key)
    if existing is None:
        people[key] = incoming
        return
    if live:
        existing.volume += incoming.volume
        existing.recent += incoming.recent or incoming.volume
    else:
        existing.volume = max(existing.volume, incoming.volume)
        existing.recent = max(existing.recent, incoming.recent)
    if incoming.last_at and (
        existing.last_at is None or incoming.last_at > existing.last_at
    ):
        existing.last_at = incoming.last_at
    existing.channels.update(incoming.channels)


def _skip_handle(name: str) -> bool:
    raw = (name or "").strip()
    if _skip_partner(raw):
        return True
    if _DIGIT_HANDLE.match(raw):
        return True
    return False


def _channel_label(person: TalkPerson) -> str:
    if person.channels == {"imessage"}:
        return "Messages"
    if person.channels == {"mail"}:
        return "mail"
    if "whatsapp" in person.channels and "imessage" in person.channels:
        return "WhatsApp and Messages"
    return "WhatsApp"


def _join_names(names: list[str]) -> str:
    clean = [name for name in names if name]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return f"{', '.join(clean[:-1])}, and {clean[-1]}"


def _stamp(value: datetime | None, clock: datetime) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=clock.tzinfo or UTC)
    return value


def _active_ranked(people: list[TalkPerson], clock: datetime) -> list[TalkPerson]:
    """People they actually message now. Stale high-volume threads drop out."""

    live: list[TalkPerson] = []
    for person in people:
        if not person.name:
            continue
        last_at = _stamp(person.last_at, clock)
        age = (clock - last_at).total_seconds() if last_at else 10**12
        if age > _STALE.total_seconds() and person.recent <= 0:
            continue
        live.append(person)
    live.sort(
        key=lambda item: (
            -item.recent,
            -((_stamp(item.last_at, clock) or _MIN).timestamp()),
            item.name.lower(),
        )
    )
    if not live:
        return []
    leader = live[0]
    kept = [leader]
    leader_at = _stamp(leader.last_at, clock)
    for person in live[1:]:
        if leader.recent:
            floor = max(2, int(leader.recent * 0.25))
            if person.recent >= floor:
                kept.append(person)
        else:
            person_at = _stamp(person.last_at, clock)
            if person_at and leader_at and (leader_at - person_at) <= _NEAR_LEADER:
                kept.append(person)
        if len(kept) >= 3:
            break
    return kept


def _active_sentence(ranked: list[TalkPerson], channel: str) -> str:
    names = [item.name for item in ranked[:3] if item.name]
    if not names:
        return ""
    if len(names) == 1:
        return f"These days you talk most with {names[0]} on {channel}."
    return (
        f"These days you talk most with {names[0]} on {channel}, then {_join_names(names[1:])}."
    )


def _roster_sentence(ranked: list[TalkPerson], channel: str) -> str:
    names = [item.name for item in ranked[:5] if item.name]
    if not names:
        return ""
    return f"People you've been messaging on {channel}: {_join_names(names)}."


def _volume_sentence(ranked: list[TalkPerson], channel: str) -> str:
    names = [item.name for item in ranked[:3]]
    if not names:
        return ""
    if len(names) == 1:
        return f"You message {names[0]} the most on {channel}."
    return (
        f"You message {names[0]} the most on {channel}, then {_join_names(names[1:])}."
    )


def _volume_aside(person: TalkPerson, channel: str) -> str:
    return f"By volume that's {person.name} on {channel}."


def _recency_sentence(person: TalkPerson, channel: str, clock: datetime) -> str:
    when = _ago(person.last_at, clock)
    tail = f", {when}" if when else ""
    return f"The latest {channel} thread is {person.name}{tail}."


def _recency_aside(person: TalkPerson, clock: datetime) -> str:
    when = _ago(person.last_at, clock)
    tail = f", {when}" if when else ""
    return f"The latest thread is {person.name}{tail}."
