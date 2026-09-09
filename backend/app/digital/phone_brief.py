"""iPhone PWA communications brief — summaries first, actions only when explicit.

Phone turns must not dump WhatsApp, iMessage, or mail threads. Digest or a
short gist is the default. More detail is a follow-up on the focused chat.
Reroute / send / readout happen only on an explicit owner speech-act.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.digital.fabric import OpContext, execute
from app.digital.identity import extract_person_query
from app.digital.taint import taint_external
from app.digital.types import AutonomyLevel, OpStatus
from app.ev.spark_task import TaskDecision, wants_readout
from app.memory.life_archive.locate import life_channel
from app.memory.mail_speak import gist_from_preview, speak_mail
from app.memory.message_speak import speak_messages, speak_person_gist

ImessagePeek = Callable[[str | None, int], list[dict[str, Any]]]
AsyncImessagePeek = Callable[[str | None, int], Awaitable[list[dict[str, Any]]]]


def _mac_hub_on() -> bool:
    """True when this Mac's local copies are the source of truth (no Chrome)."""
    try:
        from app.services.life_stream_daemon import life_stream_should_run

        return bool(life_stream_should_run())
    except Exception:
        return False

_LAST = re.compile(
    r"(?i)\b(last|latest|newest|most recent|what(?:'s| is| are| do i have).{0,24}"
    r"(?:message|messages|chat|mail|email|text|inbox))\b"
)
_DIGEST = re.compile(
    r"(?i)\b(what(?:'s| is| are).{0,20}(?:new|in (?:my )?inbox)|"
    r"any (?:new )?(?:mail|message|text|chat)|catch me up|check (?:my )?(?:mail|inbox|messages))\b"
)
_MORE = re.compile(
    r"(?i)\b("
    r"more details|more about|tell me more|go deeper|a bit more|"
    r"that (?:chat|conversation|thread|mail|email|message)|"
    r"this (?:chat|conversation|thread|mail|email|message)"
    r")\b"
)
_REROUTE = re.compile(
    r"(?i)\b("
    r"re-?route|"
    r"forward (?:this|that|the|those) |"
    r"send (?:this|that|the) (?:chat|thread|conversation|mail|email|message) |"
    r"send the last (?:chat|thread|conversation|mail|email|message) |"
    r"pass (?:this|that) (?:along|on) |"
    r"share (?:this|that) (?:chat|thread|conversation)"
    r")\b"
)
_HOLD = re.compile(r"(?i)\b(do not send|don't send|dont send|cancel send|never mind send)\b")
_SEND = re.compile(r"(?i)\b(send it|approve(?:d)? send|yes send|confirm send)\b")
_WHO_TO = re.compile(
    r"(?i)\b(?:to|onto|over to|with)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"
)
_FILLER = re.compile(
    r"(?i)\b(lorem ipsum|dolor sit amet|consectetur adipiscing|"
    r"unsubscribe|view in browser)\b"
)


def _person_query(text: str) -> str:
    return extract_person_query(text) or ""


def _compact_text(text: str, *, subject: str = "", cap: int = 72) -> str:
    blob = " ".join(str(text or "").split())
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", blob) if len(p.strip()) >= 8]
    keep = [p for p in parts if not _FILLER.search(p)]
    src = " ".join(keep) if keep else subject
    return gist_from_preview(src, subject=subject, cap=cap)


@dataclass
class PhoneFocus:
    device_id: str
    channel: str
    person: str | None
    chat_ref: str | None
    items: list[dict[str, Any]] = field(default_factory=list)
    spoken: str = ""
    at: str = ""


_FOCUS: dict[str, PhoneFocus] = {}


def reset_phone_focus() -> None:
    _FOCUS.clear()


def get_phone_focus(device_id: str) -> PhoneFocus | None:
    return _FOCUS.get(str(device_id or ""))


def is_phone_comm_ask(text: str, *, device_id: str | None = None) -> bool:
    blob = (text or "").strip()
    if not blob:
        return False
    if _REROUTE.search(blob) or _MORE.search(blob) or wants_readout(blob):
        return True
    if _LAST.search(blob) or _DIGEST.search(blob):
        return True
    if device_id and get_phone_focus(device_id) is not None and (_SEND.search(blob) or _HOLD.search(blob)):
        return True
    from app.digital.query import compile_semantic_owner_ask

    ask = compile_semantic_owner_ask(blob)
    if ask in {"send", "reply", "draft_reply", "archive", "download"}:
        return False
    if life_channel(blob) in {"whatsapp", "imessage", "mail"}:
        return True
    return bool(device_id and get_phone_focus(device_id) is not None and _MORE.search(blob))


def classify_phone_manner(text: str, *, has_focus: bool) -> str:
    if has_focus and _HOLD.search(text or ""):
        return "hold"
    if _REROUTE.search(text or "") or (has_focus and _SEND.search(text or "")):
        return "reroute"
    if wants_readout(text or ""):
        return "readout"
    if has_focus and _MORE.search(text or ""):
        return "details"
    if _person_query(text):
        return "particular"
    return "digest"


async def phone_inbox_turn(
    session: AsyncSession,
    text: str,
    *,
    device: Any = None,
    ctx: OpContext | None = None,
    imessage_peek: ImessagePeek | AsyncImessagePeek | None = None,
) -> dict[str, Any]:
    device_id = str(getattr(device, "id", "") or "") or "phone"
    ctx = ctx or OpContext(actor="phone", session=session, autonomy=AutonomyLevel.READ)
    focus = get_phone_focus(device_id)
    manner = classify_phone_manner(text, has_focus=focus is not None)
    who = _person_query(text) or (
        focus.person if focus and manner in {"details", "readout", "reroute"} else None
    )
    channel = life_channel(text)
    if manner in {"details", "readout", "reroute"} and focus and not channel:
        channel = focus.channel if focus.channel != "mixed" else None

    if manner == "reroute":
        return _reroute(text, focus, who)
    if manner == "hold":
        spoken = (
            "Okay. I have not sent anything."
            if focus
            else "Nothing was prepared to send."
        )
        if focus is None:
            return {
                "kind": "phone_brief",
                "status": OpStatus.COMPLETED_VERIFIED.value,
                "manner": "hold",
                "sent": False,
                "unauthorized_sends": 0,
                "spoken": spoken,
            }
        return _brief_payload("hold", spoken, focus)

    if manner == "details" and focus and focus.items:
        mail_items, chat_items = _split_items(focus.items)
        who = who or focus.person
        spoken = _speak_details(text, focus, mail_items, chat_items, who=who, channel=channel)
        if not spoken.strip():
            spoken = "I don't have more on that chat yet. Name the person and I'll summarize that thread."
        new_focus = _remember(
            device_id, channel, who, mail_items, chat_items, spoken, prior=focus, manner=manner
        )
        return _brief_payload(manner, spoken, new_focus)

    mail_items, chat_items = await _gather(
        ctx, text, who=who, channel=channel, imessage_peek=imessage_peek
    )
    if manner == "readout":
        spoken = _speak_readout(text, mail_items, chat_items, who=who, channel=channel)
    elif manner == "particular":
        spoken = _speak_particular(text, mail_items, chat_items, who=who, channel=channel)
    else:
        spoken = _speak_digest(text, mail_items, chat_items, channel=channel)

    if not spoken.strip():
        spoken = "I don't see a recent chat or mail I can summarize yet."

    new_focus = _remember(
        device_id, channel, who, mail_items, chat_items, spoken, prior=focus, manner=manner
    )
    return _brief_payload(manner, spoken, new_focus)


def _brief_payload(manner: str, spoken: str, focus: PhoneFocus) -> dict[str, Any]:
    return {
        "kind": "phone_brief",
        "status": OpStatus.COMPLETED_VERIFIED.value,
        "manner": manner,
        "sent": False,
        "unauthorized_sends": 0,
        "spoken": spoken,
        "phone_may_close": True,
        "second_prompt_required": False,
        "focus": {
            "channel": focus.channel,
            "person": focus.person,
            "chat_ref": focus.chat_ref,
        },
        "origin": "EXTERNAL_CONTENT",
        "authority": "DATA",
    }


def _split_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mail_items = [i for i in items if str(i.get("channel") or "") in {"mail", "gmail"}]
    chat_items = [i for i in items if str(i.get("channel") or "") not in {"mail", "gmail"}]
    if not mail_items and not chat_items:
        chat_items = list(items)
    return mail_items, chat_items


def _reroute(text: str, focus: PhoneFocus | None, who: str | None) -> dict[str, Any]:
    dest = None
    m = _WHO_TO.search(text or "")
    if m:
        dest = m.group(1).strip()
    if who and (not dest or dest.lower() == (who or "").lower()) and focus and focus.person:
        # "reroute Rahul's chat" names the source, not the destination.
        dest = None if who.lower() == (focus.person or "").lower() else (dest or who)
    if focus is None:
        spoken = (
            "Nothing to reroute yet. Ask what you last had, then say explicitly "
            "who to reroute that chat to. I have not sent anything."
        )
        return {
            "kind": "phone_brief",
            "status": OpStatus.PREPARED.value,
            "manner": "reroute",
            "sent": False,
            "unauthorized_sends": 0,
            "spoken": spoken,
        }
    if not dest:
        spoken = (
            f"I can reroute the last { _label(focus.channel) } with "
            f"{focus.person or 'that person'}, but I need who to send it to. "
            "I have not sent anything."
        )
        return {
            "kind": "phone_brief",
            "status": OpStatus.CLARIFY.value,
            "manner": "reroute",
            "sent": False,
            "unauthorized_sends": 0,
            "spoken": spoken,
        }
    if not _SEND.search(text or ""):
        spoken = (
            f"Prepared a reroute of the last { _label(focus.channel) } with "
            f"{focus.person or 'that person'} to {dest}. Not sent. "
            "Say 'approve send' if you want it sent."
        )
        return {
            "kind": "phone_brief",
            "status": OpStatus.PREPARED.value,
            "manner": "reroute",
            "sent": False,
            "unauthorized_sends": 0,
            "spoken": spoken,
            "destination": dest,
        }
    # Explicit approve still does not guess the executor here: phone brief
    # prepares; fabric send remains confirmation-gated in orchestrate.
    spoken = (
        f"Ready to send the reroute of {focus.person or 'that chat'} to {dest}. "
        "I still need an approve on the send itself — nothing has gone out."
    )
    return {
        "kind": "phone_brief",
        "status": OpStatus.WAITING_FOR_APPROVAL.value,
        "manner": "reroute",
        "sent": False,
        "unauthorized_sends": 0,
        "spoken": spoken,
        "destination": dest,
    }


async def _gather(
    ctx: OpContext,
    text: str,
    *,
    who: str | None,
    channel: str | None,
    imessage_peek: ImessagePeek | AsyncImessagePeek | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    want_mail = channel in {None, "mail"}
    want_wa = channel in {None, "whatsapp"}
    want_im = channel in {None, "imessage"}
    if channel is None:
        want_mail = want_wa = want_im = True

    async def mail() -> list[dict[str, Any]]:
        if not want_mail:
            return []
        # Mac hub first: Envelope Index needs no OAuth, no tabs. Fabric is fallback.
        mac_items = await _peek_mac_mail(who, text, 5)
        if mac_items:
            return mac_items
        if _mac_hub_on():
            return []
        q = f"from:{who}" if who else "in:inbox newer_than:7d"
        try:
            result = await execute("gmail", "search", {"q": q, "text": text, "limit": 5}, ctx=ctx)
        except Exception:
            return []
        if result.status == OpStatus.SERVICE_AUTH_REQUIRED:
            return []
        previews = result.payload.get("previews") or []
        items = []
        for p in previews[:5]:
            if not isinstance(p, dict):
                continue
            sender = str(p.get("from") or p.get("sender") or "")
            subject = str(p.get("subject") or "")
            gist = _compact_text(str(p.get("snippet") or ""), subject=subject)
            items.append(
                {
                    "memory_type": "mail.envelope.received",
                    "source": "gmail",
                    "channel": "mail",
                    "from": sender,
                    "sender": sender,
                    "subject": subject,
                    "snippet": gist,
                    "preview": gist,
                    "gist": gist,
                    "text": gist or subject,
                    "when": p.get("date") or p.get("internalDate"),
                    "handle": sender,
                }
            )
        return items

    async def whatsapp() -> list[dict[str, Any]]:
        if not want_wa:
            return []
        # Mac hub first: Desktop sqlite needs no Chrome tab. Fabric is fallback.
        mac_items = await _peek_mac_whatsapp(who, 5)
        if mac_items:
            return mac_items
        if _mac_hub_on():
            return []
        try:
            result = await execute(
                "whatsapp",
                "search_chats",
                {"query": who or ""},
                ctx=ctx,
            )
        except Exception:
            return []
        if result.status in {OpStatus.SERVICE_AUTH_REQUIRED, OpStatus.SERVICE_OFFLINE}:
            return []
        chats = result.payload.get("chats") or []
        items = []
        for c in chats[:5]:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or c.get("chat_ref") or "").strip()
            gist = _compact_text(str(c.get("gist") or ""))
            items.append(
                {
                    "memory_type": "message.whatsapp.received",
                    "source": "whatsapp",
                    "channel": "whatsapp",
                    "handle": name,
                    "title": name,
                    "who": name,
                    "preview": gist,
                    "gist": gist,
                    "text": f"{name}: {gist}" if gist else name,
                    "chat_ref": c.get("chat_ref") or name,
                    "when": c.get("timestamp"),
                }
            )
        return items

    async def imessage() -> list[dict[str, Any]]:
        if not want_im:
            return []
        rows = await _peek_imessage(who, 5, injected=imessage_peek)
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            handle = str(item.get("handle") or item.get("title") or item.get("who") or "").strip()
            gist = _compact_text(
                str(item.get("gist") or item.get("preview") or item.get("text") or "")
            )
            item["channel"] = item.get("channel") or "imessage"
            item["source"] = item.get("source") or "imessage"
            item["memory_type"] = item.get("memory_type") or "message.imessage.received"
            item["handle"] = handle
            item["preview"] = gist
            item["gist"] = gist
            item["text"] = f"{handle}: {gist}" if handle and gist else (gist or handle)
            out.append(item)
        return out

    mail_items, wa_items, im_items = await asyncio.gather(mail(), whatsapp(), imessage())
    chat_items = list(wa_items) + list(im_items)
    return mail_items, chat_items


async def _peek_imessage(
    who: str | None,
    limit: int,
    *,
    injected: ImessagePeek | AsyncImessagePeek | None,
) -> list[dict[str, Any]]:
    if injected is not None:
        result = injected(who, limit)
        if asyncio.iscoroutine(result):
            return list(await result)
        return list(result or [])

    def _sync() -> list[dict[str, Any]]:
        try:
            from app.services.life_stream_daemon import get_life_stream_daemon

            daemon = get_life_stream_daemon()
            tokens = [who] if who else None
            return list(daemon.peek_imessage(tokens=tokens, limit=limit) or [])
        except Exception:
            return []

    return await asyncio.to_thread(_sync)


async def _peek_mac_mail(who: str | None, text: str, limit: int) -> list[dict[str, Any]]:
    """Mac hub mail via Envelope Index. Empty when hub is off or unreadable."""

    def _sync() -> list[dict[str, Any]]:
        try:
            from app.services.life_stream_daemon import (
                get_life_stream_daemon,
                life_stream_should_run,
            )

            if not life_stream_should_run():
                return []
            daemon = get_life_stream_daemon()
            tokens = [who] if who else None
            rows = daemon.peek_mail(tokens=tokens, limit=limit, query=text or "")
            out: list[dict[str, Any]] = []
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                sender = str(r.get("sender") or r.get("from") or "")
                subject = str(r.get("subject") or "")
                gist = _compact_text(
                    str(r.get("gist") or r.get("preview") or r.get("snippet") or ""),
                    subject=subject,
                )
                out.append(
                    {
                        "memory_type": "mail.envelope.received",
                        "source": "mail",
                        "channel": "mail",
                        "from": sender,
                        "sender": sender,
                        "subject": subject,
                        "snippet": gist,
                        "preview": gist,
                        "gist": gist,
                        "text": f"{subject} from {sender}".strip() if subject or sender else gist,
                        "when": r.get("when") or r.get("received"),
                        "handle": sender,
                    }
                )
            return out
        except Exception:
            return []

    return await asyncio.to_thread(_sync)


async def _peek_mac_whatsapp(who: str | None, limit: int) -> list[dict[str, Any]]:
    """Mac hub WhatsApp via Desktop sqlite. Empty when hub is off or unreadable."""

    def _sync() -> list[dict[str, Any]]:
        try:
            from app.services.life_stream_daemon import (
                get_life_stream_daemon,
                life_stream_should_run,
            )

            if not life_stream_should_run():
                return []
            daemon = get_life_stream_daemon()
            tokens = [who.lower()] if who else None
            rows = daemon.peek_whatsapp(tokens=tokens, limit=limit)
            out: list[dict[str, Any]] = []
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                name = str(r.get("handle") or "").strip()
                gist = _compact_text(str(r.get("preview") or r.get("gist") or ""))
                out.append(
                    {
                        "memory_type": r.get("memory_type") or "message.whatsapp.received",
                        "source": "whatsapp",
                        "channel": "whatsapp",
                        "handle": name,
                        "title": name,
                        "who": name,
                        "preview": gist,
                        "gist": gist,
                        "text": f"{name}: {gist}" if gist else name,
                        "chat_ref": name,
                        "when": r.get("when"),
                    }
                )
            return out
        except Exception:
            return []

    return await asyncio.to_thread(_sync)


def _speak_digest(
    text: str,
    mail_items: list[dict[str, Any]],
    chat_items: list[dict[str, Any]],
    *,
    channel: str | None,
) -> str:
    bits: list[str] = []
    wa = [i for i in chat_items if str(i.get("channel")) == "whatsapp"]
    im = [i for i in chat_items if str(i.get("channel")) == "imessage"]
    if (channel in {None, "imessage"} or not channel) and im:
        line = speak_messages(
            text,
            im,
            decision=TaskDecision(family="messages", manner="digest", latest=True, source="fallback"),
        )
        if line:
            bits.append(line)
    if (channel in {None, "whatsapp"} or not channel) and wa:
        line = speak_messages(
            text,
            wa,
            decision=TaskDecision(family="messages", manner="digest", latest=True, source="fallback"),
        )
        if line:
            bits.append(line)
    if (channel in {None, "mail"} or not channel) and mail_items:
        line = speak_mail(
            text,
            mail_items,
            decision=TaskDecision(family="mail", manner="digest", latest=True, source="fallback"),
        )
        if line:
            bits.append(line)
    spoken = " ".join(bits).strip()
    if spoken and "more about" not in spoken.lower():
        spoken = spoken.rstrip(".") + ". Ask for more about one of them if you want that chat."
    return spoken


def _speak_particular(
    text: str,
    mail_items: list[dict[str, Any]],
    chat_items: list[dict[str, Any]],
    *,
    who: str | None,
    channel: str | None,
) -> str:
    wa = [i for i in chat_items if str(i.get("channel")) == "whatsapp"]
    im = [i for i in chat_items if str(i.get("channel")) == "imessage"]
    if channel == "mail" or (mail_items and not chat_items):
        return speak_mail(
            text,
            mail_items,
            decision=TaskDecision(family="mail", manner="particular", who=who or "", latest=True, source="fallback"),
        )
    picked_chats = wa if channel == "whatsapp" else im if channel == "imessage" else (im or wa)
    if who and picked_chats:
        beats = [
            {
                "title": i.get("handle") or i.get("title"),
                "who": i.get("handle") or i.get("who"),
                "body": i.get("preview") or i.get("gist") or "",
                "when": i.get("when"),
            }
            for i in picked_chats
        ]
        label = "WhatsApp" if (channel == "whatsapp" or (not channel and wa and not im)) else "Messages"
        return speak_person_gist(text, beats, [who], channel=label)
    if picked_chats:
        return speak_messages(
            text,
            picked_chats,
            decision=TaskDecision(family="messages", manner="particular", who=who or "", latest=True, source="fallback"),
        )
    if mail_items:
        return speak_mail(
            text,
            mail_items,
            decision=TaskDecision(family="mail", manner="particular", who=who or "", latest=True, source="fallback"),
        )
    return ""


def _speak_details(
    text: str,
    focus: PhoneFocus | None,
    mail_items: list[dict[str, Any]],
    chat_items: list[dict[str, Any]],
    *,
    who: str | None,
    channel: str | None,
) -> str:
    person = who or (focus.person if focus else None)
    src_chat = list(chat_items)
    src_mail = list(mail_items)
    if focus and focus.items:
        if not src_chat:
            src_chat = [i for i in focus.items if str(i.get("channel") or "") not in {"mail", "gmail"}]
        if not src_mail:
            src_mail = [i for i in focus.items if str(i.get("channel") or "") in {"mail", "gmail"}]
    if person:
        named = [
            i
            for i in src_chat
            if person.lower() in str(i.get("handle") or i.get("title") or i.get("who") or "").lower()
        ]
        if named:
            src_chat = named
        named_mail = [
            i
            for i in src_mail
            if person.lower() in str(i.get("from") or i.get("sender") or i.get("handle") or "").lower()
        ]
        if named_mail:
            src_mail = named_mail
    label = _label(channel or (focus.channel if focus else "") or "whatsapp")
    if src_chat:
        beats = [
            {
                "title": i.get("handle") or i.get("title"),
                "who": i.get("handle") or i.get("who"),
                "body": i.get("gist") or i.get("preview") or "",
                "when": i.get("when"),
            }
            for i in src_chat[:6]
        ]
        names = [person] if person else [
            str(src_chat[0].get("handle") or src_chat[0].get("title") or "")
        ]
        return speak_person_gist(text, beats, names, channel=label)
    if src_mail:
        return speak_mail(
            text,
            src_mail,
            decision=TaskDecision(
                family="mail", manner="particular", who=person or "", latest=True, source="fallback"
            ),
        )
    return "I don't have more on that chat yet. Name the person and I'll summarize that thread."


def _speak_readout(
    text: str,
    mail_items: list[dict[str, Any]],
    chat_items: list[dict[str, Any]],
    *,
    who: str | None,
    channel: str | None,
) -> str:
    if channel == "mail" or (mail_items and not chat_items):
        return speak_mail(
            text,
            mail_items,
            decision=TaskDecision(family="mail", manner="readout", who=who or "", latest=True, source="fallback"),
        )
    picked = chat_items
    if channel == "whatsapp":
        picked = [i for i in chat_items if str(i.get("channel")) == "whatsapp"] or chat_items
    elif channel == "imessage":
        picked = [i for i in chat_items if str(i.get("channel")) == "imessage"] or chat_items
    return speak_messages(
        text,
        picked,
        decision=TaskDecision(family="messages", manner="readout", who=who or "", latest=True, source="fallback"),
    )


def _remember(
    device_id: str,
    channel: str | None,
    who: str | None,
    mail_items: list[dict[str, Any]],
    chat_items: list[dict[str, Any]],
    spoken: str,
    *,
    prior: PhoneFocus | None = None,
    manner: str = "",
) -> PhoneFocus:
    # Keep BOTH halves of a mixed digest. `chat_items or mail_items` dropped
    # the mail half, so "more about that mail" after a mixed brief failed.
    items = [*chat_items, *mail_items]
    if manner == "details" and prior and not items:
        prior.spoken = spoken
        _FOCUS[device_id] = prior
        return prior
    if manner == "details" and prior:
        who = who or prior.person
        items = items or list(prior.items)
        channel = channel or prior.channel
    ch = channel or (
        "mixed"
        if mail_items and chat_items
        else "gmail"
        if mail_items
        else str((chat_items[0].get("channel") if chat_items else "mixed") or "mixed")
    )
    person = who
    if not person and items:
        person = str(items[0].get("handle") or items[0].get("from") or items[0].get("title") or "") or None
    chat_ref = None
    if items:
        chat_ref = items[0].get("chat_ref")
    elif prior:
        chat_ref = prior.chat_ref
    focus = PhoneFocus(
        device_id=device_id,
        channel=ch,
        person=person,
        chat_ref=str(chat_ref) if chat_ref else None,
        items=items[:8],
        spoken=spoken,
        at=datetime.now(UTC).isoformat(),
    )
    _FOCUS[device_id] = focus
    return focus


def _label(channel: str) -> str:
    if channel in {"imessage", "messages"}:
        return "Messages"
    if channel == "whatsapp":
        return "WhatsApp"
    if channel in {"mail", "gmail"}:
        return "mail"
    return "chat"


def public_brief(result: dict[str, Any]) -> dict[str, Any]:
    """Muse-safe: no raw threads, cookies, or tokens."""
    wrapped = taint_external(
        {
            "kind": result.get("kind"),
            "manner": result.get("manner"),
            "sent": False,
            "focus": result.get("focus"),
        },
        source="phone_brief",
    )
    return {
        "kind": result.get("kind"),
        "status": result.get("status"),
        "manner": result.get("manner"),
        "sent": False,
        "spoken": result.get("spoken"),
        "focus": result.get("focus"),
        "destination": result.get("destination"),
        "origin": wrapped.get("origin"),
        "authority": wrapped.get("authority"),
    }
