"""Ask-only correspondence desk for WhatsApp people.

Waiting, colliding plans, pickup, climate, and who-starts. Never automatic,
never a transcript, never last-chat recitation or a day-gist recap.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.life_archive.locate import (
    SOURCE,
    _GENERIC_CHAT_NAMES,
    _SELF_ADDRESSEE,
    _STOP,
    _chat_person_query_token,
)
from app.memory.life_archive.parse import read_whatsapp_messages
from app.memory.life_archive.sessions import (
    SESSION_GAP,
    _SUMMARY_CHARS,
    _clock_for,
    _display_name,
    _english_topics,
    _ending_move,
    _item_moment,
    _leftover,
    _live_whatsapp_messages,
    _merge_messages,
    _norm_body,
    _quoted_span,
    _substantial,
    _thread_for_person,
    _topic_phrase,
    _zip_path_for_thread,
    cluster_sessions,
)
from app.models import Event

DeskKind = Literal["waiting", "owed", "board", "stitch", "pickup", "climate", "starts"]

_WAITING = re.compile(
    r"\b("
    r"leav(?:e|ing) hanging|waiting on me|anyone waiting|"
    r"who am i leaving|owe(?:s)? me a reply|"
    r"anyone i haven'?t answered|haven'?t answered|"
    r"unanswered (?:chats?|people|pile)"
    r")\b",
    re.IGNORECASE,
)
_OWED = re.compile(
    r"\b("
    r"hasn'?t gotten back|have(?:n'?t| not) gotten back|"
    r"hasn'?t replied|still owes me|waiting (?:for|on) (?:a |their )?reply|"
    r"who(?:'s| is) ignoring me"
    r")\b",
    re.IGNORECASE,
)
_STITCH = re.compile(
    r"\b("
    r"collid(?:e|ing) plans|same (?:plan|thing) to two|"
    r"double[ -]?book|across (?:my )?chats|"
    r"promise(?:d)? the same|two (?:chats|people).{0,32}(?:same|both)|"
    r"same (?:plan|meetup|call|dinner) .{0,24}(?:two|both|another)"
    r")\b",
    re.IGNORECASE,
)
_PICKUP = re.compile(
    r"\b("
    r"where did (?:we|i) leave (?:it|off|things) with|"
    r"pick(?: up)? where (?:we|i) left(?: off)? with|"
    r"what(?:'s| is) (?:still )?open with|"
    r"unfinished with"
    r")\b",
    re.IGNORECASE,
)
_CLIMATE = re.compile(
    r"\b("
    r"how have things been|how(?:'s| is| has) things been|"
    r"how(?:'s| is) it been|how has it been|"
    r"how(?:'s| is) .{0,32}\b(lately|these days)|"
    r"(?:vibe|rhythm) with"
    r")\b",
    re.IGNORECASE,
)
_STARTS = re.compile(
    r"\b("
    r"who(?:'s| has| is)? (?:been )?(?:usually )?(?:starting|opening|initiating)|"
    r"who usually (?:starts|opens)|"
    r"have i been (?:the one )?(?:starting|opening)|"
    r"who (?:starts|opens) (?:the )?(?:chats?|talks?|conversations?)|"
    r"who(?:'s| is) been starting"
    r")\b",
    re.IGNORECASE,
)
_WITH_PERSON = re.compile(
    r"\b(?:with|for)\s+(?:my\s+)?([A-Za-z][A-Za-z.'-]{1,30})\b",
    re.IGNORECASE,
)
_PLAN_LABEL = {
    "call": "a call",
    "dinner": "dinner",
    "meet": "meeting up",
    "meeting": "a meeting",
    "notes": "notes",
    "office": "the office",
    "project": "the project",
    "send": "sending something",
    "standup": "standup",
    "tomorrow": "tomorrow",
    "tonight": "tonight",
}
_PLAN_WORD = re.compile(
    r"\b(call|dinner|meet|meeting|notes|office|project|send|standup|tomorrow|tonight)\b",
    re.IGNORECASE,
)
_GROUPISH = re.compile(r"[,:]|whatsapp|group|family chat", re.IGNORECASE)

KindWindow = {
    "waiting": 14,
    "owed": 14,
    "board": 14,
    "stitch": 7,
    "pickup": 21,
    "climate": 21,
    "starts": 21,
}


def desk_kind(query: str) -> DeskKind | None:
    blob = query or ""
    if _STITCH.search(blob):
        return "stitch"
    if _PICKUP.search(blob):
        return "pickup"
    if _CLIMATE.search(blob):
        return "climate"
    if _STARTS.search(blob):
        return "starts"
    waiting = bool(_WAITING.search(blob))
    owed = bool(_OWED.search(blob))
    if waiting and owed:
        return "board"
    if waiting:
        return "waiting"
    if owed:
        return "owed"
    return None


def is_chat_desk_query(query: str) -> bool:
    kind = desk_kind(query)
    if kind is None:
        return False
    if kind in {"pickup", "climate"} and not desk_person_token(query):
        return False
    return True


def desk_person_token(query: str) -> str:
    token = _chat_person_query_token(query)
    if token:
        return token
    match = _WITH_PERSON.search(query or "")
    if not match:
        return ""
    raw = match.group(1)
    token = re.sub(r"[^a-z]+", "", raw.lower())
    if len(token) < 2 or token in _SELF_ADDRESSEE or token in _STOP:
        return ""
    if token in _GENERIC_CHAT_NAMES:
        return ""
    if token in {"chats", "chat", "hanging", "plans", "plan", "things", "it"}:
        return ""
    return token


def _pack(spoken: str, *, when: Any = None) -> dict[str, Any]:
    stamp = when
    if hasattr(when, "isoformat"):
        stamp = when.isoformat()
    return {
        "spoken": spoken[:_SUMMARY_CHARS],
        "evidence": [
            {
                "id": "chat-desk",
                "source": SOURCE,
                "when": stamp,
                "text": spoken[:_SUMMARY_CHARS],
                "kind": "life",
                "memory_type": "life.chat.desk",
                "confidence": "correspondence_desk",
                "shelf": "chats",
            }
        ],
    }


def _ago(when: Any, clock) -> str:
    if when is None:
        return ""
    moment = _item_moment({"when": when}, clock)
    days = (clock.date() - moment.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    return "on " + moment.strftime("%B") + f" {moment.day}"


def _compact(name: str) -> str:
    return re.sub(r"[^a-z]+", "", (name or "").lower())


def _skip_partner(name: str) -> bool:
    raw = (name or "").strip()
    if len(raw) < 2:
        return True
    if _GROUPISH.search(raw):
        return True
    token = _compact(raw)
    if len(token) < 2 or token in _SELF_ADDRESSEE or token in _GENERIC_CHAT_NAMES:
        return True
    if token in {"sahaj", "sahajpatel"}:
        return True
    return False


def _display_partner(item: dict[str, Any]) -> str:
    raw = str(item.get("sender") or "").strip()
    if item.get("owner"):
        raw = str(item.get("partner") or raw).strip()
    return _display_name(_compact(raw), raw)


def _in_window(item: dict[str, Any], *, clock, days: int) -> bool:
    when = item.get("when")
    if when is None:
        return False
    moment = _item_moment(item, clock)
    return moment >= clock - timedelta(days=days)


def _plans(text: str) -> set[str]:
    labels: set[str] = set()
    for match in _PLAN_WORD.finditer(text or ""):
        raw = match.group(1).lower()
        label = _PLAN_LABEL.get(raw)
        if label:
            labels.add(label)
    return labels


def _by_person(messages: list[dict[str, Any]], clock) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in messages:
        partner = str(item.get("partner") or item.get("sender") or "").strip()
        if item.get("owner") and item.get("partner"):
            partner = str(item["partner"]).strip()
        elif item.get("owner"):
            continue
        if _skip_partner(partner):
            continue
        key = _compact(partner)
        row = dict(item)
        row["partner"] = partner
        grouped[key].append(row)
    ordered: dict[str, list[dict[str, Any]]] = {}
    for key, rows in grouped.items():
        ordered[key] = sorted(rows, key=lambda row: _item_moment(row, clock))
    return ordered


def _last_substantial(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    substantial = [item for item in rows if _substantial(item)]
    return substantial[-1] if substantial else None


def _hang_side(rows: list[dict[str, Any]]) -> str | None:
    last = _last_substantial(rows)
    if last is None:
        return None
    if not last.get("owner"):
        return "waiting"
    if "?" in _norm_body(last):
        return "owed"
    return None


async def _all_live_messages(session: AsyncSession) -> list[dict[str, Any]]:
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
    for event in rows:
        content = event.content if isinstance(event.content, dict) else {}
        handle = str(content.get("handle") or "").strip()
        body = str(content.get("text") or "").strip()
        if not handle or not body:
            continue
        out.append(
            {
                "sender": handle,
                "partner": handle,
                "body": body,
                "owner": bool(content.get("is_from_me")),
                "when": event.occurred_at,
            }
        )
    return _merge_messages(out, _mac_desk_tails())


def _mac_desk_tails() -> list[dict[str, Any]]:
    try:
        from app.services.life_stream_daemon import (
            get_life_stream_daemon,
            life_stream_should_run,
        )

        if not life_stream_should_run():
            return []
        return get_life_stream_daemon().read_whatsapp_desk_tails()
    except Exception:
        return []


async def _person_messages(
    session: AsyncSession,
    token: str,
    *,
    root: Path | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    thread = await _thread_for_person(session, token)
    title = ""
    if thread is not None:
        content = thread.content if isinstance(thread.content, dict) else {}
        title = str(content.get("title") or content.get("name") or "").strip()
    live = await _live_whatsapp_messages(session, token)
    other = title or token
    for item in live:
        if item.get("owner"):
            item["partner"] = other
        else:
            item["partner"] = str(item.get("sender") or other)
            if not other:
                other = item["partner"]
    messages: list[dict[str, Any]] = []
    if thread is not None:
        path = _zip_path_for_thread(thread, root=root)
        if path is not None:
            messages = read_whatsapp_messages(path)
            for item in messages:
                item["partner"] = other or str(item.get("sender") or token)
    messages = _merge_messages(messages, live)
    if not title:
        for item in messages:
            if not item.get("owner") and str(item.get("sender") or "").strip():
                title = str(item.get("sender") or "").strip()
                break
    return _display_name(token, title), messages


def _speak_waiting(groups: dict[str, list[dict[str, Any]]], *, clock, side: str) -> str:
    hits: list[tuple[str, str, Any]] = []
    for rows in groups.values():
        last = _last_substantial(rows)
        if last is None:
            continue
        if _hang_side(rows) != side:
            continue
        name = _display_partner(last if not last.get("owner") else rows[-1])
        if last.get("owner"):
            name = _display_partner(next((row for row in reversed(rows) if not row.get("owner")), last))
        hits.append((name, _ago(last.get("when"), clock), last.get("when")))
    hits = sorted(hits, key=lambda item: item[2] or clock, reverse=True)[:4]
    if not hits:
        if side == "waiting":
            return "I don't see anyone you're leaving hanging on WhatsApp right now."
        return "I don't see anyone who still owes you a WhatsApp reply right now."
    if side == "waiting":
        if len(hits) == 1:
            name, when, _ = hits[0]
            stamp = f", {when}" if when else ""
            return f"You're leaving {name} hanging{stamp} — they still had the last word."
        bits = [f"{name} ({when})" if when else name for name, when, _ in hits]
        return "You're leaving hanging: " + ", ".join(bits[:-1]) + f", and {bits[-1]}."
    if len(hits) == 1:
        name, when, _ = hits[0]
        stamp = f", {when}" if when else ""
        return f"Still waiting on a reply from {name}{stamp}."
    bits = [f"{name} ({when})" if when else name for name, when, _ in hits]
    return "Still waiting on a reply from " + ", ".join(bits[:-1]) + f", and {bits[-1]}."


def _speak_board(groups: dict[str, list[dict[str, Any]]], *, clock) -> str:
    waiting = _speak_waiting(groups, clock=clock, side="waiting")
    owed = _speak_waiting(groups, clock=clock, side="owed")
    empty_w = "don't see anyone you're leaving hanging" in waiting
    empty_o = "don't see anyone who still owes" in owed
    if empty_w and empty_o:
        return "Nothing's sitting unanswered on WhatsApp right now."
    if empty_w:
        return owed
    if empty_o:
        return waiting
    return f"{waiting} {owed}"


def _speak_stitch(groups: dict[str, list[dict[str, Any]]], *, clock) -> str:
    plans: dict[str, list[str]] = defaultdict(list)
    for rows in groups.values():
        recent = [item for item in rows if _in_window(item, clock=clock, days=7) and _substantial(item)]
        if not recent:
            continue
        name = _display_partner(next((row for row in reversed(rows) if not row.get("owner")), rows[-1]))
        labels: set[str] = set()
        for item in recent:
            labels |= _plans(_norm_body(item))
        for label in labels:
            if name not in plans[label]:
                plans[label].append(name)
    collisions = [(label, names) for label, names in plans.items() if len(names) >= 2]
    if not collisions:
        return "I don't see the same plan sitting in two chats right now."
    weight = {
        "meeting up": 5,
        "a meeting": 5,
        "a call": 4,
        "dinner": 4,
        "tomorrow": 3,
        "tonight": 3,
        "standup": 3,
        "the project": 2,
        "the office": 2,
        "sending something": 1,
        "notes": 1,
    }
    collisions.sort(key=lambda item: -weight.get(item[0], 0))
    collisions = collisions[:2]
    parts: list[str] = []
    for label, names in collisions:
        who = " and ".join(names[:3])
        parts.append(f"{label.capitalize()} is in play with {who}")
    spoken = ". ".join(parts) + "."
    return spoken


def _speak_pickup(name: str, messages: list[dict[str, Any]], *, clock) -> str:
    recent = [item for item in messages if _in_window(item, clock=clock, days=21)]
    pool = recent or messages
    sessions = cluster_sessions(pool, clock=clock)
    if not sessions:
        return ""
    last = sessions[-1]
    topics = _english_topics(last) or _english_topics(pool)
    about = _topic_phrase(topics) if topics else "that thread"
    leftover = _leftover(last, name) or _ending_move(last)
    ago = _ago(last[-1].get("when"), clock)
    lead = f"You and {name} left it on {about}"
    if ago:
        lead += f", {ago}"
    spoken = f"{lead}. {leftover}".strip()
    if _quoted_span(spoken, pool):
        return f"You and {name} still have an open thread, {ago}.".strip()
    return spoken


def _session_stats(sessions: list[list[dict[str, Any]]], *, clock) -> dict[str, Any]:
    owner_starts = sum(1 for block in sessions if block and block[0].get("owner"))
    other_starts = len(sessions) - owner_starts
    last = sessions[-1][-1] if sessions and sessions[-1] else None
    days = { _item_moment(block[0], clock).date() for block in sessions if block }
    return {
        "sessions": len(sessions),
        "days": len(days),
        "owner_starts": owner_starts,
        "other_starts": other_starts,
        "last": last,
        "silence": (clock.date() - _item_moment(last, clock).date()).days if last else 99,
    }


def _speak_climate(name: str, messages: list[dict[str, Any]], *, clock) -> str:
    recent = [item for item in messages if _in_window(item, clock=clock, days=21)]
    if not recent:
        return f"I don't see recent WhatsApp with {name}."
    sessions = cluster_sessions(recent, clock=clock)
    if not sessions:
        return f"I don't see recent WhatsApp with {name}."
    stats = _session_stats(sessions, clock=clock)
    last = sessions[-1]
    leftover = _leftover(last, name)
    parts: list[str] = []
    silence = stats["silence"]
    if silence >= 10 and stats["days"] >= 2:
        parts.append(f"With {name}, it's been quieter than that stretch — last talk was {_ago(stats['last'].get('when'), clock)}.")
    elif silence >= 4:
        parts.append(f"With {name}, last talk was {_ago(stats['last'].get('when'), clock)}.")
    elif stats["sessions"] >= 4 or stats["days"] >= 3:
        parts.append(f"With {name}, you've been in a regular back-and-forth this stretch.")
    else:
        parts.append(f"With {name}, you've talked {stats['sessions']} time{'s' if stats['sessions'] != 1 else ''} lately.")
    if stats["other_starts"] > stats["owner_starts"]:
        parts.append("They've been the one starting.")
    elif stats["owner_starts"] > stats["other_starts"]:
        parts.append("You've been the one opening.")
    else:
        parts.append("You've been taking turns starting.")
    if leftover:
        parts.append(leftover)
    spoken = " ".join(parts)
    if _quoted_span(spoken, recent):
        return f"With {name}, the recent rhythm is still there, and that's where it sits."
    return spoken


def _speak_starts_named(name: str, messages: list[dict[str, Any]], *, clock) -> str:
    recent = [item for item in messages if _in_window(item, clock=clock, days=21)]
    sessions = cluster_sessions(recent, clock=clock) if recent else []
    if len(sessions) < 1:
        return f"I don't see enough recent talks with {name} to say who starts."
    stats = _session_stats(sessions, clock=clock)
    last_owner = bool(sessions[-1][0].get("owner"))
    last_who = "you started the most recent one" if last_owner else "they started the most recent one"
    if stats["owner_starts"] > stats["other_starts"]:
        return (
            f"With {name}, you've opened {stats['owner_starts']} of the last "
            f"{stats['sessions']} talks; {last_who}."
        )
    if stats["other_starts"] > stats["owner_starts"]:
        return (
            f"With {name}, they've opened {stats['other_starts']} of the last "
            f"{stats['sessions']} talks; {last_who}."
        )
    return f"With {name}, you've been splitting who starts; {last_who}."


def _speak_starts_global(groups: dict[str, list[dict[str, Any]]], *, clock) -> str:
    bits: list[str] = []
    ranked: list[tuple[int, str, int, int]] = []
    for rows in groups.values():
        recent = [item for item in rows if _in_window(item, clock=clock, days=21)]
        sessions = cluster_sessions(recent, gap=SESSION_GAP, clock=clock)
        if len(sessions) < 2:
            continue
        name = _display_partner(next((row for row in reversed(rows) if not row.get("owner")), rows[-1]))
        owner_starts = sum(1 for block in sessions if block and block[0].get("owner"))
        ranked.append((len(sessions), name, owner_starts, len(sessions) - owner_starts))
    ranked.sort(reverse=True)
    for _count, name, owner_starts, other_starts in ranked[:3]:
        if owner_starts > other_starts:
            bits.append(f"you've been the one opening with {name}")
        elif other_starts > owner_starts:
            bits.append(f"{name} has been starting with you")
        else:
            bits.append(f"you and {name} take turns opening")
    if not bits:
        return "I don't see a clear starter pattern across recent WhatsApp."
    if len(bits) == 1:
        return bits[0][:1].upper() + bits[0][1:] + "."
    return bits[0][:1].upper() + bits[0][1:] + ", and " + ", and ".join(bits[1:]) + "."


async def answer_desk_query(
    session: AsyncSession,
    query: str,
    *,
    root: Path | None = None,
    now=None,
) -> dict[str, Any] | None:
    """Ask-only correspondence desk. None when the query is not a desk ask."""

    kind = desk_kind(query)
    if kind is None:
        return None
    if kind in {"pickup", "climate"} and not desk_person_token(query):
        return None
    clock = _clock_for(now)
    token = desk_person_token(query)
    if kind in {"pickup", "climate"} or (kind == "starts" and token):
        name, messages = await _person_messages(session, token, root=root)
        if not messages:
            return None
        if kind == "pickup":
            spoken = _speak_pickup(name, messages, clock=clock)
        elif kind == "climate":
            spoken = _speak_climate(name, messages, clock=clock)
        else:
            spoken = _speak_starts_named(name, messages, clock=clock)
        if not spoken:
            return None
        when = max((item.get("when") for item in messages if item.get("when")), default=None)
        return _pack(spoken, when=when)

    live = await _all_live_messages(session)
    groups = _by_person(live, clock)
    window = KindWindow.get(kind, 14)
    trimmed: dict[str, list[dict[str, Any]]] = {}
    for key, rows in groups.items():
        kept = [item for item in rows if _in_window(item, clock=clock, days=window)]
        if kept:
            trimmed[key] = kept
    if kind == "waiting":
        spoken = _speak_waiting(trimmed, clock=clock, side="waiting")
    elif kind == "owed":
        spoken = _speak_waiting(trimmed, clock=clock, side="owed")
    elif kind == "board":
        spoken = _speak_board(trimmed, clock=clock)
    elif kind == "stitch":
        spoken = _speak_stitch(trimmed, clock=clock)
    else:
        spoken = _speak_starts_global(trimmed, clock=clock)
    when = None
    for rows in trimmed.values():
        for item in rows:
            stamp = item.get("when")
            if stamp is not None and (when is None or stamp > when):
                when = stamp
    return _pack(spoken, when=when)
