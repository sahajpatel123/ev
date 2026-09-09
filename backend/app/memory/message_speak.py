"""Spoken iMessage/WhatsApp: headers and a short gist, never the full thread.

MAC HUB ONLY. chat.db / WhatsApp Desktop on this Mac. Do not edit this
file for iPhone / PWA / device-gateway work.

Mail already had this split. Chats now follow the same job: digest is who/what
showed up, particular is what the last talk was about, readout is only when
they asked to hear the words themselves.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.memory.life_archive.desk import _ago
from app.memory.mail_speak import gist_from_preview

SPOKEN_MSG_CAP = 520
SPOKEN_READOUT_CAP = 1600
DIGEST_HEADLINES = 3
GIST_CAP = 140

_LIVE_LINE = re.compile(r"^(You|.+?):\s+(.+)$", re.DOTALL)
_EXCERPT = re.compile(
    r"^(?:WhatsApp|Messages|iMessage) with\s+(.+?)\s+—\s+(Owner|.+?):\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_CHAT_TYPES = frozenset(
    {
        "life.chat.excerpt",
        "message.imessage.received",
        "message.imessage.sent",
        "message.whatsapp.received",
        "message.whatsapp.sent",
    }
)


def is_chat_hit(item: dict[str, Any]) -> bool:
    kind = str(item.get("memory_type") or item.get("kind") or "")
    if kind in {"life.chat.thread", "life.person", "life.chat.talk", "life.chat.session", "life.chat.desk"}:
        return False
    if kind in {
        "mail.envelope.received",
        "call.history.recorded",
        "photo.library.indexed",
        "contact.discovered",
        "contact.updated",
    }:
        # Mail/call/photo/contact envelopes are never chat, even when a mail
        # subject contains a colon ("Run failed: ...").
        return False
    channel = str(item.get("channel") or item.get("source") or "").lower()
    text = str(item.get("text") or "")
    if text.lower().startswith("whatsapp thread:") or text.lower().startswith("person:"):
        return False
    if kind in _CHAT_TYPES or kind.startswith("message."):
        return True
    if channel in {"imessage", "whatsapp", "messages"}:
        return True
    if channel in {"mail", "calls", "photos", "contacts"}:
        return False
    return bool(_LIVE_LINE.match(text) or _EXCERPT.match(text))


def message_fields(item: dict[str, Any]) -> dict[str, str]:
    text = " ".join(str(item.get("text") or "").split()).strip()
    handle = str(item.get("handle") or item.get("title") or item.get("sender") or "").strip()
    preview = str(item.get("preview") or item.get("body") or item.get("snippet") or "").strip()
    speaker = str(item.get("who") or "").strip()
    owner = bool(item.get("owner") or item.get("is_from_me"))
    match = _EXCERPT.match(text)
    if match:
        handle = handle or match.group(1).strip()
        speaker = speaker or match.group(2).strip()
        preview = preview or match.group(3).strip()
        owner = owner or speaker.lower() in {"owner", "you"}
    else:
        live = _LIVE_LINE.match(text)
        if live:
            speaker = speaker or live.group(1).strip()
            preview = preview or live.group(2).strip()
            owner = owner or speaker.lower() in {"you", "owner", "me"}
            if not owner and not handle:
                handle = speaker
    if owner:
        speaker = "You"
    elif speaker.lower() in {"owner", "me"}:
        speaker = "You"
        owner = True
    gist = str(item.get("gist") or "").strip() or gist_from_preview(preview, cap=GIST_CAP)
    if handle.lower() in {"you", "owner", "me", "someone"}:
        handle = ""
    return {
        "handle": handle[:48],
        "speaker": speaker or ("You" if owner else handle or "someone"),
        "preview": preview,
        "gist": gist,
        "channel": _channel_label(item),
    }


def speak_messages(query: str, items: list[dict[str, Any]], decision: Any | None = None) -> str:
    """Spark (or fallback) chooses readout vs gist vs digest. Never dumps by default."""

    rows = [item for item in items if isinstance(item, dict) and is_chat_hit(item)]
    if not rows:
        rows = [item for item in items if isinstance(item, dict)]
    if not rows:
        return ""
    if decision is None:
        try:
            from app.ev.spark_task import active_decision

            decision = active_decision()
        except Exception:
            decision = None
    manner = str(getattr(decision, "manner", "") or "")
    who = str(getattr(decision, "who", "") or "").strip()
    latest = bool(getattr(decision, "latest", False))
    focus = str(getattr(decision, "focus", "") or "")
    if manner == "readout":
        picked = _pick(rows, who=who, latest=True)
        return _speak_readout(picked) if picked else ""
    # Manner wins. `latest` on a digest means "the recent set", not "one
    # particular last thread". Hundreds of recents wordings share digest.
    if manner == "digest":
        return _speak_digest(rows)
    if manner == "particular" or who or focus in {"when", "who", "subject"}:
        picked = _pick(rows, who=who, latest=True)
        return _speak_particular(query, picked, decision) if picked else ""
    if latest:
        picked = _pick(rows, who=who, latest=True)
        return _speak_particular(query, picked, decision) if picked else ""
    return _speak_digest(rows)


def speak_person_gist(
    query: str,
    beats: list[dict[str, Any]],
    names: list[str],
    *,
    channel: str = "WhatsApp",
) -> str:
    """What the last talk was about — never 'X said <line>'."""

    name = next((item for item in names if item), "")
    if not name:
        for beat in beats:
            title = str(beat.get("title") or beat.get("handle") or "").strip()
            if title and title.lower() not in {"you", "owner"}:
                name = title
                break
    timed = [beat for beat in beats if isinstance(beat, dict)]
    timed.sort(key=lambda item: str(item.get("when") or ""))
    timed = timed[-6:]
    bodies = [str(beat.get("body") or beat.get("preview") or "").strip() for beat in timed]
    bodies = [item for item in bodies if item]
    when = ""
    if beats:
        stamp = timed[-1].get("when") if timed else None
        when = _ago(stamp, datetime.now(UTC)) if stamp else ""
    lead = (
        f"Last you talked with {name} on {channel}"
        if name
        else f"Last you were chatting on {channel}"
    )
    if when:
        lead += f", {when}"
    lead += "."
    about = _about_from_bodies(bodies)
    spoken = f"{lead} {about}".strip()
    return spoken[:SPOKEN_MSG_CAP]


def shape_message_payload(payload: dict[str, Any], query: str) -> dict[str, Any]:
    """Keep tool JSON to handle+gist so the model cannot recite a thread."""

    items = payload.get("messages") or payload.get("items") or []
    hits = [message_fields(item) if isinstance(item, dict) else message_fields({"text": str(item)}) for item in items]
    spoken = str(payload.get("spoken") or "").strip() or speak_messages(query, [item for item in items if isinstance(item, dict)])
    shaped = dict(payload)
    readout = spoken.lower().startswith("reading it out")
    shaped["messages"] = [
        {
            "text": (
                f"{hit['speaker']}: {hit['preview']}"[:400]
                if readout
                else f"{hit['handle'] or hit['speaker']} — {hit['gist']}"[:160]
            ),
            "when": item.get("when") if isinstance(item, dict) else None,
            "handle": hit["handle"],
            "gist": hit["gist"],
            "channel": hit["channel"],
        }
        for item, hit in zip(items, hits)
        if isinstance(item, dict) or hit["gist"]
    ]
    cap = SPOKEN_READOUT_CAP if readout else SPOKEN_MSG_CAP
    shaped["spoken"] = spoken[:cap]
    shaped.pop("items", None)
    return shaped


def _channel_label(item: dict[str, Any]) -> str:
    channel = str(item.get("channel") or item.get("source") or "").lower()
    kind = str(item.get("memory_type") or "")
    if "imessage" in channel or "imessage" in kind or channel == "messages":
        return "Messages"
    if "whatsapp" in channel or "whatsapp" in kind:
        return "WhatsApp"
    text = str(item.get("text") or "").lower()
    if text.startswith("whatsapp"):
        return "WhatsApp"
    return "Messages"


def _pick(rows: list[dict[str, Any]], *, who: str, latest: bool) -> dict[str, Any] | None:
    token = re.sub(r"[^a-z]+", "", (who or "").lower())
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(rows):
        fields = message_fields(item)
        blob = f"{fields['handle']} {fields['speaker']} {fields['preview']}".lower()
        match = 1 if token and token in re.sub(r"[^a-z]+", "", blob) else 0
        recency = len(rows) - index
        scored.append((match, recency, item))
    if token:
        matched = [item for hit, _recency, item in scored if hit]
        if matched:
            return matched[0]
    if latest or rows:
        return rows[0]
    return None


def _speak_digest(rows: list[dict[str, Any]]) -> str:
    bits: list[str] = []
    seen: set[str] = set()
    for item in rows:
        fields = message_fields(item)
        who = fields["handle"] or ("" if fields["speaker"] == "You" else fields["speaker"])
        gist = _cap_clause(fields["gist"] or fields["preview"], 72)
        if not who and not gist:
            continue
        headline = f"{who} — {gist}" if who and gist else (who or gist)
        key = (who or "").lower()
        if key in seen:
            continue
        seen.add(key)
        bits.append(headline.rstrip(".") + ".")
        if len(bits) >= DIGEST_HEADLINES:
            break
    if not bits:
        return ""
    channels = {_channel_label(item) for item in rows if isinstance(item, dict)}
    if len(channels) > 1:
        # Mixed inbox (WhatsApp + Messages + mail): no single channel owns it.
        return ("Latest across your inbox: " + " ".join(bits))[:SPOKEN_MSG_CAP]
    channel = _channel_label(rows[0])
    return (f"Latest {channel}: " + " ".join(bits))[:SPOKEN_MSG_CAP]


def _speak_particular(query: str, item: dict[str, Any], decision: Any = None) -> str:
    del query
    fields = message_fields(item)
    who = fields["handle"] or fields["speaker"]
    gist = fields["gist"] or _about_from_bodies([fields["preview"]])
    channel = fields["channel"]
    from app.memory.mail_speak import speak_received

    when_bit = speak_received(item)
    focus = str(getattr(decision, "focus", "") or "gist")
    if focus == "when":
        if when_bit:
            return f"Last with {who} on {channel} arrived {when_bit}."[:SPOKEN_MSG_CAP]
        return f"Last with {who} on {channel}."[:SPOKEN_MSG_CAP]
    if focus == "who":
        return f"That last {channel} was with {who}."[:SPOKEN_MSG_CAP]
    if not gist:
        if when_bit:
            return f"Last on {channel} with {who}, {when_bit}."[:SPOKEN_MSG_CAP]
        return f"Last on {channel} with {who}."[:SPOKEN_MSG_CAP]
    line = f"Last with {who} on {channel}"
    if when_bit:
        line += f", {when_bit}"
    line += f". {gist}"
    if not line.endswith((".", "!", "?")):
        line += "."
    return line[:SPOKEN_MSG_CAP]


def _speak_readout(item: dict[str, Any]) -> str:
    fields = message_fields(item)
    body = " ".join(str(fields["preview"] or fields["gist"]).split()).strip()
    if not body:
        return ""
    who = fields["handle"] or fields["speaker"]
    spoken = f"Reading it out from {who}: {body}"
    if len(spoken) > SPOKEN_READOUT_CAP - 36:
        spoken = spoken[: SPOKEN_READOUT_CAP - 36].rsplit(" ", 1)[0] + ". That's as far as I'll read."
    return spoken[:SPOKEN_READOUT_CAP]


_STOP = frozenset(
    {
        "about",
        "after",
        "because",
        "before",
        "could",
        "going",
        "have",
        "just",
        "lets",
        "let's",
        "there",
        "their",
        "that's",
        "that's",
        "this",
        "that",
        "they",
        "them",
        "want",
        "when",
        "where",
        "which",
        "would",
        "your",
        "youre",
    }
)


def _about_from_bodies(bodies: list[str]) -> str:
    blob = " ".join(bodies)
    if not blob.strip():
        return "You were catching up."
    gist = gist_from_preview(blob, cap=GIST_CAP)
    lowered = (gist or "").lower()
    for body in bodies:
        compact = " ".join(body.split())
        if len(compact) >= 18 and compact.lower() in lowered:
            gist = ""
            break
    if gist:
        if gist.lower().startswith("it was"):
            return gist if gist.endswith((".", "!", "?")) else gist + "."
        return gist if gist.endswith((".", "!", "?")) else gist + "."
    words: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-zA-Z]{5,}", blob.lower()):
        if token in _STOP or token in seen:
            continue
        seen.add(token)
        words.append(token)
        if len(words) >= 2:
            break
    if not words:
        return "You were catching up."
    if len(words) == 1:
        return f"It was about {words[0]}."
    return f"It was about {words[0]} and {words[1]}."


def _cap_clause(text: str, cap: int) -> str:
    compact = " ".join(str(text or "").split()).strip().rstrip(".")
    if len(compact) <= cap:
        return compact
    clipped = compact[: cap - 1].rsplit(" ", 1)[0].rstrip(",;:")
    return (clipped or compact[: cap - 1]).rstrip() + "…"
