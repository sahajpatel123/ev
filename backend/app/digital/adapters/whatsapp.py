"""WhatsApp personal account — OPERATED via authorized WhatsApp Web on Home Station.

No unofficial inbox API. No reverse-engineered protocol. No password prompts.
Muse sees semantic operations only — never coordinates, CSS, or cookies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.digital.descriptor import ServiceCapabilityDescriptor, cap
from app.digital.fabric import OpContext, OpResult
from app.digital.taint import taint_external
from app.digital.types import WHATSAPP_READ_BOUND, Availability, OpStatus, Verb


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
        q = query.lower()
        hits = []
        for cid, chat in self.chats.items():
            name = str(chat.get("name") or "")
            if q in name.lower() or q in cid.lower():
                hits.append({"chat_ref": cid, "name": name, "unread": int(chat.get("unread") or 0)})
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
        text = str(observed.get("spoken") or observed.get("text") or "").lower()
        if "qr" in text or "log in" in text or "keep me signed in" in text:
            return {
                "authenticated": False,
                "backing": "computer",
                "diagnosis": "session_logged_out",
                "focus_theft": self.focus_events,
            }
        if observed.get("ok") is False:
            return {
                "authenticated": False,
                "backing": "computer",
                "diagnosis": observed.get("error") or "browser_offline",
                "focus_theft": self.focus_events,
            }
        return {
            "authenticated": True,
            "backing": "computer",
            "focus_theft": self.focus_events,
            "foreground_required": bool(observed.get("activated")),
        }

    async def search_chats(self, query: str) -> list[dict[str, Any]]:
        result = await self._act("whatsapp.search_chats", {"query": query})
        chats = result.get("chats") if isinstance(result.get("chats"), list) else []
        return [
            {"chat_ref": c.get("chat_ref") or c.get("name"), "name": c.get("name")}
            for c in chats
            if isinstance(c, dict)
        ]

    async def open_chat(self, chat_ref: str) -> dict[str, Any]:
        result = await self._act("whatsapp.open_chat", {"chat_ref": chat_ref})
        if not result.get("ok"):
            raise KeyError(result.get("error") or "chat_not_found")
        return {"chat_ref": result.get("chat_ref") or chat_ref, "name": result.get("name")}

    async def read_recent(self, chat_ref: str, *, limit: int) -> list[dict[str, Any]]:
        result = await self._act("whatsapp.read_recent", {"chat_ref": chat_ref, "limit": limit})
        msgs = result.get("messages") if isinstance(result.get("messages"), list) else []
        return [m for m in msgs if isinstance(m, dict)][:limit]

    async def search_messages(self, chat_ref: str, query: str) -> list[dict[str, Any]]:
        result = await self._act("whatsapp.search_messages", {"chat_ref": chat_ref, "query": query})
        msgs = result.get("messages") if isinstance(result.get("messages"), list) else []
        return [m for m in msgs if isinstance(m, dict)]

    async def compose(self, chat_ref: str, text: str) -> dict[str, Any]:
        result = await self._act("whatsapp.compose", {"chat_ref": chat_ref, "text": text})
        return {"chat_ref": chat_ref, "composed": text, "sent": False, **result}

    async def send(self, chat_ref: str, text: str, *, attachment: str | None = None) -> dict[str, Any]:
        # Observe first — never send twice into an already-matching last message.
        recent = await self.read_recent(chat_ref, limit=3)
        last = recent[-1] if recent else None
        if last and last.get("from_me") and str(last.get("text") or "") == text:
            return {
                "chat_ref": chat_ref,
                "sent": True,
                "duplicate_prevented": True,
                "message": last,
                "verified_in_thread": True,
            }
        result = await self._act(
            "whatsapp.send",
            {"chat_ref": chat_ref, "text": text, "attachment": attachment},
        )
        if not result.get("ok") and not result.get("sent"):
            raise RuntimeError(result.get("error") or "send_failed")
        verify = await self.read_recent(chat_ref, limit=3)
        appeared = any(str(m.get("text") or "") == text for m in verify)
        return {
            "chat_ref": chat_ref,
            "sent": bool(appeared),
            "verified_in_thread": appeared,
            "message": verify[-1] if verify else None,
        }

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
        if op == "whatsapp.search_chats":
            js = wrap_js(wjs.search_chats_js(str(args.get("query") or "")))
        elif op == "whatsapp.open_chat":
            js = wrap_js(wjs.open_chat_js(str(args.get("chat_ref") or "")))
        elif op == "whatsapp.read_recent":
            js = wrap_js(wjs.read_recent_js(int(args.get("limit") or 30)))
        elif op == "whatsapp.search_messages":
            js = wrap_js(wjs.search_messages_js(str(args.get("query") or "")))
        elif op == "whatsapp.compose":
            js = wrap_js(wjs.compose_js(str(args.get("text") or "")))
        elif op == "whatsapp.send":
            js = wrap_js(wjs.send_js(str(args.get("text") or "")))
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
            chats = await backing.search_chats(str(args.get("query") or args.get("name") or args.get("chat_ref") or ""))
            if len(chats) > 1:
                return OpResult(status=OpStatus.CLARIFY, service="whatsapp", operation=operation,
                                availability=Availability.OPERATED, error="ambiguous",
                                clarify=chats, payload={"sent": False})
            if not chats:
                return OpResult(status=OpStatus.CLARIFY, service="whatsapp", operation=operation,
                                availability=Availability.OPERATED, error="not_found", payload={"sent": False})
            opened = await backing.open_chat(chats[0]["chat_ref"])
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
        if operation in {"send", "reply", "attach"}:
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
