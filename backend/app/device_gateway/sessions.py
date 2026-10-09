"""Reviewable conversation sessions on one phone.

The phone used to show its turn receipts as one flat list, which made "what
did we talk about yesterday?" unanswerable: a timer set three days earlier
sat next to this morning's chat with nothing between them. This module groups
a device's conversation into sessions so the surface can show one card per
conversation and let the owner open exactly one of them.

Where the turns live. Two durable paths record a phone conversation, and the
history is only whole when both are read:

* ``PhoneTurnReceipt`` — every voice/live turn, plus typed turns when the
  kernel mouths them. The row carries the transcript and, when the server
  resolved it, Evie's reply in ``evidence.core_reply``.
* ``Event`` (``source="device_text"``, ``message.user`` /
  ``message.assistant``) — every streamed typed turn: the PWA's composer
  runs the chat pipeline, which records the owner's message and Evie's
  answer as thread events rather than a receipt.

Grouping is derived at read time, never stored: both source tables are
immutable, and a scan over them in time order is a pure function of what is
on disk. The scan has a stable-prefix property — a turn appended today can
never change the boundary between two older sessions — so a session the
owner once opened keeps its contents.

Rules, in order:

* A turn carrying a live-voice session id (a receipt's ``session_id``)
  belongs to that live session. Two different live session ids are always
  two sessions; one live session never merges into another.
* Every other turn joins the session before it when the silence since that
  turn is at most ``settings.phone_session_gap_seconds`` — a conversation
  the owner stepped away from and resumed. Once the silence is longer, the
  next turn starts a new session.

Titles are the session's first owner utterance, trimmed. Nothing here
invents a topic: a session means "these turns happened in one sitting", and
the detail view shows the actual exchange, including a turn whose Evie
reply was never stored — said plainly, never simulated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Device, Event, PhoneTurnReceipt
from app.utils.text import utcnow

# Rows scanned to compose the session list, per source. The window only
# bounds work per request; it is far larger than any list the phone renders.
SCAN_WINDOW = 400

# How long after an owner message an assistant event may land and still be
# that turn's answer. Windows longer than this are proactive messages, not
# replies, and must not be glued onto someone else's question.
REPLY_PAIR_WINDOW = timedelta(minutes=30)

# Longest owner line used as a session title, at a word boundary when one
# falls near the cut.
TITLE_MAX = 70

# Longest owner line shown on a session card.
PREVIEW_MAX = 120


def _when(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; the gateway stores UTC."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _trim(text: str, limit: int) -> str:
    """Trim to ``limit`` characters on a word boundary when possible."""

    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:—-") + "…"


@dataclass
class _Row:
    """One exchange in time order, from either durable source."""

    at: datetime
    origin: str  # "owner" | "evie"
    owner_text: str = ""
    reply_text: str | None = None
    reply_recorded: bool = False
    reply_note: str = ""
    chips: list[dict[str, Any]] = field(default_factory=list)
    explicit_key: str | None = None
    kind: str = ""
    # Identity of the durable row this came from. It anchors the session id
    # so a re-scan names the same session the same way, and two sessions
    # that begin in the same second never share a key.
    anchor: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "kind": self.kind,
            "origin": self.origin,
            "owner_text": self.owner_text,
            "reply_text": self.reply_text,
            "reply_recorded": self.reply_recorded,
            "reply_note": self.reply_note,
            "trusted_owner": self.origin == "owner",
            "life_mutation": False,
            "chips": self.chips,
        }


@dataclass
class _Group:
    """One session under construction: its rows and derived summary."""

    key: str
    explicit_key: str | None
    rows: list[_Row] = field(default_factory=list)

    @property
    def first_at(self) -> datetime | None:
        return self.rows[0].at if self.rows else None

    @property
    def last_at(self) -> datetime | None:
        return self.rows[-1].at if self.rows else None

    def title(self) -> str:
        for row in self.rows:
            text = _trim(row.owner_text or row.reply_text or "", TITLE_MAX)
            if text:
                return text
        first = self.first_at
        stamp = first.strftime("%b %-d") if first else "undated"
        return f"Conversation · {stamp}"

    def preview(self) -> str:
        for row in reversed(self.rows):
            text = _trim(row.owner_text or row.reply_text or "", PREVIEW_MAX)
            if text:
                return text
        return ""

    def public(self) -> dict[str, Any]:
        first, last = self.first_at, self.last_at
        kinds = sorted({str(row.kind) for row in self.rows if row.kind})
        return {
            "id": self.key,
            "title": self.title(),
            "preview": self.preview(),
            "turns": len(self.rows),
            "kinds": kinds,
            "live": bool(self.explicit_key),
            "started_at": first.isoformat() if first else None,
            "last_at": last.isoformat() if last else None,
        }


def _gap_seconds() -> int:
    try:
        gap = int(getattr(settings, "phone_session_gap_seconds", 2700) or 0)
    except (TypeError, ValueError):
        gap = 2700
    return max(0, gap)


def _chips_of(row: PhoneTurnReceipt) -> list[dict[str, Any]]:
    """Provenance chips exactly as the flat history endpoint renders them."""

    actions = row.action_calls if isinstance(row.action_calls, list) else []
    return [
        {
            "tool": str(action.get("name") or action.get("tool") or ""),
            "route": str(action.get("route") or action.get("provenance") or ""),
            "executed": bool(action.get("executed", action.get("ok"))),
        }
        for action in actions[:6]
        if isinstance(action, dict)
    ]


def _recorded_reply(row: PhoneTurnReceipt) -> tuple[str | None, str]:
    """Evie's reply as the server recorded it, plus the honest note to show
    when nothing was stored.

    A turn the realtime lane answered lives only in the stream, so its
    evidence names the owner instead of carrying a reply. Saying so is the
    honest answer; showing the owner's own words back as an answer is not.
    """

    evidence = row.evidence if isinstance(row.evidence, dict) else {}
    if evidence.get("response_owner") == "realtime":
        return None, "Answered live — the reply stayed in the stream."
    reply = evidence.get("core_reply")
    text = str(reply or "").strip()
    if not text:
        return None, "No reply was recorded for this turn."
    return text, ""


async def _receipt_rows(
    session: AsyncSession, *, device_id: UUID,
) -> list[_Row]:
    """Receipts as exchange rows. Voice/live turns live here."""

    rows = (
        await session.execute(
            select(PhoneTurnReceipt)
            .where(PhoneTurnReceipt.device_id == device_id)
            .order_by(PhoneTurnReceipt.created_at.desc())
            .limit(SCAN_WINDOW)
        )
    ).scalars().all()
    turns: list[_Row] = []
    for row in rows:
        at = _when(row.created_at)
        if at is None:
            continue
        reply, note = _recorded_reply(row)
        turns.append(
            _Row(
                at=at,
                origin="owner",
                owner_text=str(row.transcript or ""),
                reply_text=reply,
                reply_recorded=reply is not None,
                reply_note=note,
                chips=_chips_of(row),
                explicit_key=(str(row.session_id or "").strip() or None),
                kind=str(row.kind or ""),
                anchor=str(row.id),
            )
        )
    return turns


async def _typed_rows(
    session: AsyncSession, *, device_id: UUID,
) -> list[_Row]:
    """Streamed typed turns as exchange rows, owner messages paired with the
    assistant event that answered them.

    These are the PWA composer's turns: the chat pipeline records them as
    thread events (``source="device_text"``) instead of receipts, so reading
    only receipts would show the phone a history silently missing most of
    what the owner typed. ``never_send_to_model`` events are excluded: the
    privacy boundary keeps them out of every surface, including this one.
    """

    events = (
        await session.execute(
            select(Event)
            .where(
                Event.device_id == str(device_id),
                Event.source == "device_text",
                Event.event_type.in_(["message.user", "message.assistant"]),
                Event.tombstoned_at.is_(None),
                Event.privacy_level != "never_send_to_model",
            )
            .order_by(Event.occurred_at.desc())
            .limit(SCAN_WINDOW)
        )
    ).scalars().all()
    turns: list[_Row] = []
    pending: _Row | None = None
    for event in reversed(events):  # oldest first, so replies follow asks
        at = _when(event.occurred_at) or utcnow()
        text = str(event.content.get("text") or "") if isinstance(event.content, dict) else ""
        if (event.event_type or "") == "message.assistant":
            if (
                pending is not None
                and not pending.reply_recorded
                and at - pending.at <= REPLY_PAIR_WINDOW
            ):
                # This is the answer to the question just asked.
                pending.reply_text = text.strip() or None
                pending.reply_recorded = bool(text.strip())
            else:
                # Evie spoke without a recorded ask (a greeting, a nudge):
                # its own row, labelled as hers, never attached to someone
                # else's question.
                turns.append(
                    _Row(at=at, origin="evie", reply_text=text.strip() or None,
                         reply_recorded=bool(text.strip()), kind="typed",
                         anchor=str(event.id))
                )
            continue
        pending = _Row(
            at=at, origin="owner", owner_text=text, kind="typed",
            anchor=str(event.id),
            reply_note="No reply was recorded for this turn.",
        )
        turns.append(pending)
    return turns


def _group(rows: list[_Row]) -> list[_Group]:
    """Scan oldest to newest into sessions. Stable prefix; see module doc."""

    ordered = sorted(rows, key=lambda row: row.at)
    groups: list[_Group] = []
    for row in ordered:
        explicit = row.explicit_key
        previous = groups[-1].rows[-1] if groups else None
        joins = False
        if previous is not None:
            if explicit is not None:
                # A live session keeps its identity across gaps: its turns
                # are one conversation even when the owner paused between
                # utterances.
                joins = explicit == groups[-1].explicit_key
            else:
                # Typed or Evie's own words: same sitting only while the
                # silence rule still holds.
                joins = (row.at - previous.at).total_seconds() <= _gap_seconds()
        if joins:
            groups[-1].rows.append(row)
        else:
            # Session id is anchored on the first durable row's identity, so
            # a re-scan after today's turns still names yesterday's session
            # the same — and two sessions that begin in the same second stay
            # distinct.
            key = f"sess-{row.at.strftime('%Y%m%d%H%M%S')}-{row.anchor[-8:]}"
            groups.append(_Group(key=key, explicit_key=explicit, rows=[row]))
    return groups


async def _device_rows(session: AsyncSession, *, device: Device) -> list[_Row]:
    receipts = await _receipt_rows(session, device_id=device.id)
    typed = await _typed_rows(session, device_id=device.id)
    return receipts + typed


def _groups_of(rows: list[_Row]) -> list[_Group]:
    return _group(rows)


async def list_sessions(
    session: AsyncSession, *, device: Device, limit: int = 30,
) -> dict[str, Any]:
    """Newest session first; each card carries the summary the list renders."""

    groups = _groups_of(await _device_rows(session, device=device))
    capped = [group for group in reversed(groups) if group.rows][
        : max(1, min(int(limit or 30), 50))
    ]
    return {
        "ok": True,
        "gap_seconds": _gap_seconds(),
        "sessions": [group.public() for group in capped],
    }


async def session_detail(
    session: AsyncSession, *, device: Device, session_id: str,
) -> dict[str, Any] | None:
    """One session as a readable exchange, or None when this phone has none.

    Rows come back oldest first. An owner row carries Evie's reply when the
    server recorded one; ``reply_recorded`` is False exactly when nothing
    was stored, so the surface can say so instead of inventing an answer.
    """

    key = str(session_id or "").strip()
    for group in _groups_of(await _device_rows(session, device=device)):
        if group.key != key:
            continue
        return {
            "ok": True,
            "session": group.public(),
            "turns": [row.public() for row in group.rows],
        }
    return None
