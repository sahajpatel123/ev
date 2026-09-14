"""WhatsApp personal account — OPERATED via authorized WhatsApp Web on Home Station.

No unofficial inbox API. No reverse-engineered protocol. No password prompts.
Muse sees semantic operations only — never coordinates, CSS, or cookies.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.digital.descriptor import ServiceCapabilityDescriptor, cap
from app.digital.fabric import OpContext, OpResult
from app.digital.taint import taint_external
from app.digital.types import WHATSAPP_READ_BOUND, Availability, OpStatus, Verb
from app.ev.messaging.recipients import UNIQUE_GAP, UNIQUE_SCORE, score_person_name

# One draft per chat that Evie typed and never saw land. The page reports only
# the *length* of a draft it refuses to replace, never its text, so our own
# leftover is recognized by that length against this record: a draft of any
# other length stays foreign and is never cleared.
_last_failed_compose: dict[str, str] = {}


def _own_stale_draft(chat_ref: str, result: dict[str, Any]) -> str:
    """The text of Evie's own refused draft, or "" when the box is foreign.

    The page never hands back the draft's text, only its length, so ownership
    is decided against the last text Evie typed into that chat: only a draft
    of exactly that length is ours to clear.
    """

    mine = _last_failed_compose.get(chat_ref)
    prior_len = int(result.get("prior_len") or 0)
    return mine if mine and prior_len == len(mine) else ""


def _sidebar_gist(text: str, cap: int = 80) -> str:
    """One sidebar line — first sentence, never a pasted thread."""
    blob = " ".join(str(text or "").split())
    if not blob:
        return ""
    for sep in (". ", "? ", "! "):
        at = blob.find(sep)
        if 8 <= at + 1 <= cap:
            return blob[: at + 1].strip()
    return blob[:cap].rstrip()


def _thread_body(row: dict[str, Any]) -> str:
    """Best-effort message body without the thread chrome.

    Thread rows wrap the text with a timestamp line; comparing raw row text
    lets a short new message hide inside a longer old one (or miss an exact
    resend). Prefer the page-extracted ``body`` when present.
    """

    raw = str(row.get("body") or row.get("text") or "")
    return re.sub(r"\n\d{1,2}:\d{2}(\s?[AP]M)?\s*$", "", raw).strip()


class WhatsAppBacking(Protocol):
    async def status(self) -> dict[str, Any]:
        ...

    async def search_chats(self, query: str) -> list[dict[str, Any]]:
        ...

    async def open_chat(self, chat_ref: str) -> dict[str, Any]:
        ...

    async def read_recent(self, chat_ref: str, *, limit: int) -> list[dict[str, Any]]:
        ...

    async def search_messages(self, chat_ref: str, query: str) -> list[dict[str, Any]]:
        ...

    async def compose(self, chat_ref: str, text: str) -> dict[str, Any]:
        ...

    async def send(self, chat_ref: str, text: str, *, attachment: str | None = None) -> dict[str, Any]:
        ...

    async def download_attachment(self, chat_ref: str, attachment_ref: str) -> dict[str, Any]:
        ...


@dataclass
class FakeWhatsAppBacking:
    """Hermetic in-memory WhatsApp Web. Never counts as live REAL PASS."""

    authenticated: bool = True
    chats: dict[str, dict[str, Any]] = field(default_factory=dict)
    sent: list[dict[str, Any]] = field(default_factory=list)
    focus_events: int = 0
    last_open: str | None = None

    async def status(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated,
            "backing": "fake",
            "focus_theft": self.focus_events,
            "profile": "dedicated-fake",
        }

    async def search_chats(self, query: str) -> list[dict[str, Any]]:
        q = query.lower().strip()
        hits = []
        for cid, chat in self.chats.items():
            name = str(chat.get("name") or "")
            if not q or q in name.lower() or q in cid.lower():
                last = (chat.get("messages") or [])[-1] if chat.get("messages") else {}
                gist = _sidebar_gist(str(last.get("text") or chat.get("gist") or ""))
                hits.append(
                    {
                        "chat_ref": cid,
                        "name": name,
                        "unread": int(chat.get("unread") or 0),
                        "gist": gist,
                    }
                )
        return hits

    async def open_chat(self, chat_ref: str) -> dict[str, Any]:
        chat = self.chats.get(chat_ref)
        if chat is None:
            # resolve by name
            for cid, row in self.chats.items():
                if str(row.get("name") or "").lower() == chat_ref.lower() or cid == chat_ref:
                    chat_ref = cid
                    chat = row
                    break
        if chat is None:
            raise KeyError("chat_not_found")
        self.last_open = chat_ref
        return {"chat_ref": chat_ref, "name": chat.get("name")}

    async def read_recent(self, chat_ref: str, *, limit: int) -> list[dict[str, Any]]:
        opened = await self.open_chat(chat_ref)
        msgs = list(self.chats[opened["chat_ref"]].get("messages") or [])
        return msgs[-max(1, min(limit, WHATSAPP_READ_BOUND)) :]

    async def search_messages(self, chat_ref: str, query: str) -> list[dict[str, Any]]:
        msgs = await self.read_recent(chat_ref, limit=WHATSAPP_READ_BOUND)
        q = query.lower()
        return [m for m in msgs if q in str(m.get("text") or "").lower()]

    async def compose(self, chat_ref: str, text: str) -> dict[str, Any]:
        opened = await self.open_chat(chat_ref)
        return {"chat_ref": opened["chat_ref"], "composed": text, "sent": False}

    async def send(self, chat_ref: str, text: str, *, attachment: str | None = None) -> dict[str, Any]:
        opened = await self.open_chat(chat_ref)
        cid = opened["chat_ref"]
        # Observe before retry: if identical last outbound exists, do not duplicate.
        msgs = list(self.chats[cid].get("messages") or [])
        last = msgs[-1] if msgs else None
        if last and last.get("from_me") and last.get("text") == text and last.get("attachment") == attachment:
            return {
                "chat_ref": cid,
                "sent": True,
                "duplicate_prevented": True,
                "message": last,
            }
        msg = {
            "id": f"wa-{len(msgs)+1}",
            "from_me": True,
            "text": text,
            "attachment": attachment,
            "timestamp": "now",
            "state": "sent",
        }
        msgs.append(msg)
        self.chats[cid]["messages"] = msgs
        self.sent.append(msg)
        return {"chat_ref": cid, "sent": True, "message": msg, "verified_in_thread": True}

    async def download_attachment(self, chat_ref: str, attachment_ref: str) -> dict[str, Any]:
        return {
            "chat_ref": chat_ref,
            "attachment_ref": attachment_ref,
            "origin": "EXTERNAL_CONTENT",
            "authority": "DATA",
            "bytes_present": False,
            "note": "fake backing does not store blobs",
        }


class ComputerWhatsAppBacking:
    """OPERATED path: computer executor / WhatsApp Web. No selectors leak out."""

    def __init__(self) -> None:
        self.focus_events = 0

    async def status(self) -> dict[str, Any]:
        observed = await self._observe()
        focus = int(observed.get("focus_theft") or self.focus_events or 0)
        if observed.get("ok") is False:
            return {
                "authenticated": False,
                "backing": "computer",
                "diagnosis": observed.get("diagnosis") or observed.get("error") or "browser_offline",
                "focus_theft": focus,
            }
        if observed.get("qr") or not bool(observed.get("authenticated")):
            return {
                "authenticated": False,
                "backing": "computer",
                "diagnosis": observed.get("diagnosis") or "session_logged_out",
                "focus_theft": focus,
            }
        return {
            "authenticated": True,
            "backing": "computer",
            "focus_theft": focus,
            "foreground_required": bool(observed.get("activated")),
            "diagnosis": observed.get("diagnosis") or "ok",
        }

    async def search_chats(self, query: str) -> list[dict[str, Any]]:
        import asyncio

        q = str(query or "")
        if q:
            typed = await self._act("whatsapp.type_search", {"query": q})
            if typed.get("ok"):
                await asyncio.sleep(1.2)
        try:
            result = await self._act("whatsapp.search_chats", {"query": q})
            chats = result.get("chats") if isinstance(result.get("chats"), list) else []
            if not chats and self._looks_self(q):
                await self._act("whatsapp.open_new_chat", {})
                await asyncio.sleep(0.8)
                result = await self._act("whatsapp.search_chats", {"query": q})
                chats = result.get("chats") if isinstance(result.get("chats"), list) else []
        finally:
            # Never leave the owner's sidebar filtered.
            with contextlib.suppress(Exception):
                await self._act("whatsapp.type_search", {"query": ""})
        return [
            {
                "chat_ref": c.get("chat_ref") or c.get("name"),
                "name": c.get("name"),
                "gist": str(c.get("gist") or "")[:80],
            }
            for c in chats
            if isinstance(c, dict)
        ]

    async def open_chat(self, chat_ref: str) -> dict[str, Any]:
        import asyncio

        last_error: str | None = None

        async def _opened() -> dict[str, Any] | None:
            nonlocal last_error
            result = await self._act("whatsapp.open_chat", {"chat_ref": chat_ref})
            if result.get("ok") and (result.get("already") or result.get("opened")):
                return {
                    "chat_ref": result.get("chat_ref") or chat_ref,
                    "name": result.get("name"),
                }
            if result.get("error"):
                last_error = str(result["error"])
            return None

        opened = await _opened()
        if opened is None and self._looks_self(chat_ref):
            await self._act("whatsapp.open_new_chat", {})
            await asyncio.sleep(0.8)
            opened = await _opened()
        if opened is None:
            # Search ranks variants first ("Mansi Makani" over "Mansi"). Clear
            # and type the name, then open the exact row with a real mouse
            # gesture; a bare .click() does not navigate and Enter takes the
            # top hit.
            await self._act("whatsapp.type_search", {"query": ""})
            await asyncio.sleep(0.3)
            await self._act("whatsapp.type_search", {"query": chat_ref})
            for _ in range(5):
                await asyncio.sleep(0.7)
                clicked = await self._act("whatsapp.click_exact_chat", {"chat_ref": chat_ref})
                if clicked.get("ok") and clicked.get("clicked"):
                    await asyncio.sleep(1.0)
                    opened = await _opened()
                    if opened is not None:
                        break
        if opened is None:
            # Last resort: search+Enter, then one repaint beat to verify.
            opened = await _opened()
        if opened is None:
            await asyncio.sleep(1.5)
            opened = await _opened()
        if opened is None:
            await self._act("whatsapp.type_search", {"query": ""})
            raise KeyError(last_error or "chat_not_found")
        return opened

    async def read_recent(self, chat_ref: str, *, limit: int) -> list[dict[str, Any]]:
        await self.open_chat(chat_ref)
        result = await self._act("whatsapp.read_recent", {"chat_ref": chat_ref, "limit": limit})
        msgs = result.get("messages") if isinstance(result.get("messages"), list) else []
        return [m for m in msgs if isinstance(m, dict)][:limit]

    async def search_messages(self, chat_ref: str, query: str) -> list[dict[str, Any]]:
        await self.open_chat(chat_ref)
        result = await self._act("whatsapp.search_messages", {"chat_ref": chat_ref, "query": query})
        msgs = result.get("messages") if isinstance(result.get("messages"), list) else []
        return [m for m in msgs if isinstance(m, dict)]

    async def compose(self, chat_ref: str, text: str) -> dict[str, Any]:
        import asyncio

        await self.open_chat(chat_ref)
        # The editor needs a beat after the thread renders; a stale or
        # missing selection silently appends instead of replacing.
        await asyncio.sleep(0.8)
        result: dict[str, Any] = {"ok": False}
        for _ in range(3):
            result = await self._act("whatsapp.compose", {"chat_ref": chat_ref, "text": text})
            if result.get("matched"):
                # The box now holds our text: remember it as ours in case the
                # send that follows never lands.
                _last_failed_compose[chat_ref] = text
                break
            if result.get("foreign_draft"):
                # The page refused before touching the box: a foreign draft
                # is present. Never click send on top of it.
                result = {**result, "foreign_draft": True}
                break
            if result.get("present_len"):
                # The page typed for us and the box did not read back as the
                # exact text (WhatsApp re-renders links, emoji, spacing). It
                # is still our draft: never click send on unverified text,
                # but let the sender clear and retype it.
                _last_failed_compose[chat_ref] = text
                result = {**result, "matched": False, "own_draft": True}
                break
            await asyncio.sleep(0.8)
        await asyncio.sleep(0.35)
        return {"chat_ref": chat_ref, "composed": text, "sent": False, **result}

    async def send(self, chat_ref: str, text: str, *, attachment: str | None = None) -> dict[str, Any]:
        import asyncio

        if attachment:
            blocked = await self._act("whatsapp.attach", {"chat_ref": chat_ref, "attachment": attachment})
            return {
                "chat_ref": chat_ref,
                "sent": False,
                "verified_in_thread": False,
                "error": blocked.get("error") or "attachment_file_picker_blocked",
                "diagnosis": blocked.get("diagnosis") or "cannot_set_file_input_from_js",
                "focus_theft": 0,
            }
        # Observe first — never send twice into an already-matching last message.
        recent = await self.read_recent(chat_ref, limit=3)
        last = recent[-1] if recent else None
        # Exact body equality only: a new message that merely appears inside
        # a longer previous outbound must still be sent (and reported
        # honestly), never swallowed as a "duplicate".
        if last and last.get("from_me") and text and _thread_body(last) == text:
            return {
                "chat_ref": chat_ref,
                "sent": True,
                "duplicate_prevented": True,
                "message": last,
                "verified_in_thread": True,
            }
        composed = await self.compose(chat_ref, text)
        stale = _own_stale_draft(chat_ref, composed) if composed.get("foreign_draft") else ""
        if composed.get("foreign_draft") and not stale:
            raise RuntimeError("compose_box_has_other_text")

        async def _appeared() -> tuple[bool, list[dict[str, Any]]]:
            await asyncio.sleep(0.8)
            rows = await self.read_recent(chat_ref, limit=5)
            # Only our own rows count: an incoming message with the same words
            # must never verify a send.
            return any(
                m.get("from_me") and text in str(m.get("text") or "") for m in rows
            ), rows

        reclaimed = bool(composed.get("own_draft") or stale)
        if reclaimed:
            # Clear the box and type our text again: the guarded compose never
            # replaces content it did not write, and this content is ours — a
            # draft of an earlier attempt that never landed, not someone
            # else's.
            _last_failed_compose.pop(chat_ref, None)
            clicked = await self._act("whatsapp.send", {"chat_ref": chat_ref, "text": text})
        elif not composed.get("matched"):
            raise RuntimeError(composed.get("error") or "compose_failed")
        else:
            clicked = await self._act("whatsapp.click_send", {"chat_ref": chat_ref})
        appeared, verify = await _appeared()
        if not appeared and reclaimed:
            # That op types and clicks in one tick and React may not have
            # rendered the send control yet. A separate click sees it; once
            # the text has gone out the box is empty and there is nothing to
            # click.
            clicked = await self._act("whatsapp.click_send", {"chat_ref": chat_ref})
            appeared, verify = await _appeared()
        if appeared:
            _last_failed_compose.pop(chat_ref, None)
        else:
            # Our text may still sit in the box: remember it so the next
            # attempt to this chat can clear and retype it.
            _last_failed_compose[chat_ref] = text
        if not clicked.get("ok") and not clicked.get("sent") and not appeared:
            raise RuntimeError(clicked.get("error") or "send_failed")
        return {
            "chat_ref": chat_ref,
            "sent": bool(appeared),
            "verified_in_thread": appeared,
            "message": verify[-1] if verify else None,
            "prepared": bool(clicked.get("prepared")),
        }

    @staticmethod
    def _looks_self(ref: str) -> bool:
        t = str(ref or "").strip().lower()
        return t in {"__self__", "you", "me"} or "message yourself" in t or "yourself" in t

    async def download_attachment(self, chat_ref: str, attachment_ref: str) -> dict[str, Any]:
        result = await self._act(
            "whatsapp.download_attachment",
            {"chat_ref": chat_ref, "attachment_ref": attachment_ref},
        )
        result.setdefault("origin", "EXTERNAL_CONTENT")
        result.setdefault("authority", "DATA")
        return result

    async def _observe(self) -> dict[str, Any]:
        from app.digital.chrome_session import eval_in_tab, wrap_js
        from app.digital.whatsapp_js import status_js

        observed = await eval_in_tab(url_contains="web.whatsapp.com", javascript=wrap_js(status_js()))
        observed.setdefault("activated", False)
        observed.setdefault("focus_theft", 0)
        return observed

    async def _act(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        from app.digital import whatsapp_js as wjs
        from app.digital.chrome_session import eval_in_tab, wrap_js

        js = ""
        if op == "whatsapp.type_search":
            js = wrap_js(wjs.type_search_js(str(args.get("query") or "")))
        elif op == "whatsapp.search_chats":
            js = wrap_js(wjs.search_chats_js(str(args.get("query") or "")))
        elif op == "whatsapp.open_new_chat":
            js = wrap_js(wjs.open_new_chat_js())
        elif op == "whatsapp.open_chat":
            js = wrap_js(wjs.open_chat_js(str(args.get("chat_ref") or "")))
        elif op == "whatsapp.click_exact_chat":
            js = wrap_js(wjs.click_exact_chat_js(str(args.get("chat_ref") or "")))
        elif op == "whatsapp.read_recent":
            js = wrap_js(wjs.read_recent_js(int(args.get("limit") or 30)))
        elif op == "whatsapp.search_messages":
            js = wrap_js(wjs.search_messages_js(str(args.get("query") or "")))
        elif op == "whatsapp.compose":
            js = wrap_js(wjs.compose_js(str(args.get("text") or "")))
        elif op == "whatsapp.click_send":
            js = wrap_js(wjs.click_send_js())
        elif op == "whatsapp.send":
            js = wrap_js(wjs.send_js(str(args.get("text") or "")))
        elif op == "whatsapp.attach":
            js = wrap_js(wjs.attach_js())
        else:
            return {"ok": False, "error": "unknown_semantic_op", "diagnosis": "ui_changed"}
        result = await eval_in_tab(url_contains="web.whatsapp.com", javascript=js)
        result.setdefault("activated", False)
        result.setdefault("focus_theft", 0)
        return result


class WhatsAppWebAdapter:
    slug = "whatsapp"
    display_name = "WhatsApp Web"

    def __init__(self, backing: WhatsAppBacking | None = None) -> None:
        self.backing = backing or ComputerWhatsAppBacking()

    def descriptors(self) -> list[ServiceCapabilityDescriptor]:
        common = dict(
            credential_type="whatsapp_web_session",
            data_classification="external_chat",
            backing="WhatsApp Web / Home Station computer executor",
            supports_background=True,
            requires_foreground=False,
        )
        av = Availability.OPERATED
        return [
            cap("whatsapp", "search_chats", Verb.SEARCH, av, read_write="read", risk="R0",
                verification_method="chat_ref", **common),
            cap("whatsapp", "resolve_chat", Verb.RESOLVE, av, read_write="read", risk="R0",
                verification_method="unique_or_clarify", **common),
            cap("whatsapp", "read_thread", Verb.READ, av, read_write="read", risk="R0",
                verification_method="bounded_messages", **common),
            cap("whatsapp", "read_recent", Verb.READ, av, read_write="read", risk="R0",
                verification_method="bounded_messages", **common),
            cap("whatsapp", "search_messages", Verb.SEARCH, av, read_write="read", risk="R0",
                verification_method="message_hits", **common),
            cap("whatsapp", "thread_summary", Verb.READ, av, read_write="read", risk="R0",
                verification_method="bounded_summary", **common),
            cap("whatsapp", "compose", Verb.PREPARE, av, read_write="write", risk="R1",
                verification_method="composed_not_sent", **common),
            cap("whatsapp", "send", Verb.SEND, av, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="appears_in_thread", **common),
            cap("whatsapp", "reply", Verb.REPLY, av, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="appears_in_thread", **common),
            cap("whatsapp", "attach", Verb.ATTACH, av, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="appears_in_thread", **common),
            cap("whatsapp", "download_attachment", Verb.DOWNLOAD, av, read_write="read", risk="R1",
                verification_method="sha256", **common),
            cap("whatsapp", "watch", Verb.WATCH, av, read_write="read", risk="R0",
                verification_method="bounded_poll", **common,
                notes="watched chat/thread only; no full scrape"),
        ]

    async def execute(self, operation: str, args: dict[str, Any], *, ctx: OpContext) -> OpResult:
        backing = getattr(ctx, "whatsapp_backing", None) or self.backing
        try:
            st = await backing.status()
        except Exception as exc:
            return OpResult(status=OpStatus.SERVICE_OFFLINE, service="whatsapp", operation=operation,
                            availability=Availability.OPERATED, error="backing_offline",
                            diagnosis=type(exc).__name__)
        if not st.get("authenticated"):
            return OpResult(
                status=OpStatus.SERVICE_AUTH_REQUIRED,
                service="whatsapp",
                operation=operation,
                availability=Availability.CONNECTION_REQUIRED,
                error="whatsapp_web_not_linked",
                diagnosis=st.get("diagnosis") or "session_logged_out",
                payload={"focus_theft": st.get("focus_theft", 0)},
            )
        try:
            return await self._run(backing, operation, args, st)
        except KeyError:
            return OpResult(status=OpStatus.FAILED, service="whatsapp", operation=operation,
                            availability=Availability.OPERATED, error="chat_not_found",
                            diagnosis="chat_not_found")
        except Exception as exc:
            return OpResult(status=OpStatus.FAILED, service="whatsapp", operation=operation,
                            availability=Availability.OPERATED, error="whatsapp_ui_error",
                            diagnosis=str(exc)[:120] or "ui_changed")

    async def _run(self, backing: WhatsAppBacking, operation: str, args: dict[str, Any], st: dict[str, Any]) -> OpResult:
        if operation == "search_chats":
            chats = await backing.search_chats(str(args.get("query") or args.get("name") or ""))
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="whatsapp", operation=operation,
                            availability=Availability.OPERATED, payload={"chats": chats})
        if operation == "resolve_chat":
            query = str(args.get("query") or args.get("name") or args.get("chat_ref") or "")
            chats = await backing.search_chats(query)
            # The row set is whatever the page's substring filter returned:
            # one row is not an identity. Score whole name tokens and accept
            # only a strong, untied winner; otherwise ask, offering the rows.
            scored = sorted(
                ((score_person_name(query, str(row.get("name") or "")), row) for row in chats),
                key=lambda pair: pair[0],
                reverse=True,
            )
            strong = [(score, row) for score, row in scored if score >= UNIQUE_SCORE]
            tied = [row for score, row in strong if strong and strong[0][0] - score < UNIQUE_GAP]
            if len(tied) > 1:
                return OpResult(status=OpStatus.CLARIFY, service="whatsapp", operation=operation,
                                availability=Availability.OPERATED, error="ambiguous",
                                clarify=tied, payload={"sent": False})
            if not tied:
                return OpResult(status=OpStatus.CLARIFY, service="whatsapp", operation=operation,
                                availability=Availability.OPERATED,
                                error="ambiguous" if len(chats) > 1 else "not_found",
                                clarify=chats, payload={"sent": False})
            opened = await backing.open_chat(tied[0]["chat_ref"])
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="whatsapp", operation=operation,
                            availability=Availability.OPERATED, payload={"chat": opened},
                            verification={"chat_ref": opened.get("chat_ref")})
        if operation in {"read_thread", "read_recent"}:
            msgs = await backing.read_recent(str(args.get("chat_ref") or args.get("name")), limit=int(args.get("limit") or 30))
            wrapped = taint_external({"messages": msgs, "bounded": True, "complete_history": False}, source="whatsapp")
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="whatsapp", operation=operation,
                            availability=Availability.OPERATED, payload=wrapped, taint=wrapped,
                            verification={"count": len(msgs), "complete_history": False})
        if operation == "search_messages":
            msgs = await backing.search_messages(str(args.get("chat_ref")), str(args.get("query") or ""))
            wrapped = taint_external({"messages": msgs}, source="whatsapp")
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="whatsapp", operation=operation,
                            availability=Availability.OPERATED, payload=wrapped, taint=wrapped)
        if operation == "thread_summary":
            msgs = await backing.read_recent(str(args.get("chat_ref")), limit=int(args.get("limit") or 40))
            summary = summarize_thread(msgs)
            wrapped = taint_external({"summary": summary, "based_on": len(msgs)}, source="whatsapp")
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="whatsapp", operation=operation,
                            availability=Availability.OPERATED, payload=wrapped, taint=wrapped)
        if operation == "compose":
            composed = await backing.compose(str(args.get("chat_ref")), str(args.get("text") or args.get("body") or ""))
            return OpResult(status=OpStatus.PREPARED, service="whatsapp", operation=operation,
                            availability=Availability.OPERATED, payload={**composed, "sent": False})
        if operation == "attach":
            sent = await backing.send(
                str(args.get("chat_ref")),
                str(args.get("text") or args.get("body") or ""),
                attachment=args.get("attachment") or "safe-test",
            )
            if sent.get("error") == "attachment_file_picker_blocked" or sent.get("diagnosis") == "cannot_set_file_input_from_js":
                return OpResult(
                    status=OpStatus.BLOCKED,
                    service="whatsapp",
                    operation=operation,
                    availability=Availability.OPERATED,
                    error="attachment_file_picker_blocked",
                    diagnosis="cannot_set_file_input_from_js",
                    payload={**sent, "sent": False, "focus_theft": 0},
                )
            ok = bool(sent.get("verified_in_thread") or sent.get("sent"))
            return OpResult(
                status=OpStatus.COMPLETED_VERIFIED if ok else OpStatus.UNKNOWN,
                service="whatsapp",
                operation=operation,
                availability=Availability.OPERATED,
                payload=sent,
                verification={
                    "chat_ref": sent.get("chat_ref"),
                    "verified_in_thread": sent.get("verified_in_thread"),
                },
            )
        if operation in {"send", "reply"}:
            sent = await backing.send(
                str(args.get("chat_ref")),
                str(args.get("text") or args.get("body") or ""),
                attachment=args.get("attachment"),
            )
            ok = bool(sent.get("verified_in_thread") or sent.get("sent"))
            return OpResult(
                status=OpStatus.COMPLETED_VERIFIED if ok else OpStatus.UNKNOWN,
                service="whatsapp",
                operation=operation,
                availability=Availability.OPERATED,
                payload=sent,
                verification={
                    "chat_ref": sent.get("chat_ref"),
                    "text_fragment": str(args.get("text") or "")[:80],
                    "verified_in_thread": sent.get("verified_in_thread"),
                    "duplicate_prevented": sent.get("duplicate_prevented"),
                },
            )
        if operation == "download_attachment":
            blob = await backing.download_attachment(str(args.get("chat_ref")), str(args.get("attachment_ref")))
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="whatsapp", operation=operation,
                            availability=Availability.OPERATED, payload=blob)
        if operation == "watch":
            msgs = await backing.read_recent(str(args.get("chat_ref")), limit=10)
            return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="whatsapp", operation=operation,
                            availability=Availability.OPERATED,
                            payload={"messages": msgs[-3:], "poll": "bounded"})
        return OpResult(status=OpStatus.FAILED, service="whatsapp", operation=operation,
                        availability=Availability.OPERATED, error="unknown_operation")


def summarize_thread(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Latest-state summary, not a chronological dump."""
    if not messages:
        return {"latest_state": "empty", "requests": [], "commitments": [], "next_action": None}
    last = messages[-1]
    requests = [m for m in messages if "?" in str(m.get("text") or "") and not m.get("from_me")]
    return {
        "latest_state": str(last.get("text") or "")[:400],
        "last_sender": "owner" if last.get("from_me") else last.get("sender") or "them",
        "requests": [str(m.get("text") or "")[:200] for m in requests[-3:]],
        "unresolved": bool(requests) and not last.get("from_me"),
        "next_action": "reply" if (requests and not last.get("from_me")) else None,
        "based_on_count": len(messages),
        "complete_history": False,
    }
