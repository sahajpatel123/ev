"""Muse-facing Digital Operations tools. No credentials, selectors, or cookies."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.digital.fabric import OpContext, answer_can_you, execute
from app.digital.orchestrate import handle_outcome
from app.digital.types import AutonomyLevel
from app.digital.waiting import GLOBAL_WAITING, owner_brief

DIGITAL_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "digital_act",
        "description": (
            "Operate the owner's digital services (mail, messages, Contacts, "
            "Calendar, browser, files) via semantic operations. Live mail and "
            "chats on this Mac are Apple Mail, WhatsApp Desktop, and iMessage — "
            "reads must not open a browser tab. Never pass passwords, tokens, "
            "cookies, CSS selectors, or coordinates."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "service": {"type": "string", "enum": ["gmail", "contacts", "calendar", "whatsapp", "browser", "files", "phone"]},
                "operation": {"type": "string"},
                "args": {"type": "object"},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["service", "operation"],
        },
        "sensitive": True,
        "read_only": False,
        "risk_class": "R2",
        "permission": "mail:act",
        "undoable": False,
        "target_ownership": "owner",
        "provider": "digital",
        "output": {"type": "object"},
    },
    {
        "name": "digital_outcome",
        "description": "Resolve an owner digital outcome across services without naming the app.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 4000}},
            "required": ["text"],
        },
        "sensitive": False,
        "read_only": True,
        "risk_class": "R1",
        "permission": "mail:read",
        "undoable": False,
        "target_ownership": "owner",
        "provider": "digital",
        "output": {"type": "object"},
    },
    {
        "name": "digital_waiting",
        "description": "Who the owner is waiting on, and who is waiting on the owner.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "sensitive": False,
        "read_only": True,
        "risk_class": "R0",
        "permission": "mail:read",
        "undoable": False,
        "target_ownership": "owner",
        "provider": "digital",
        "output": {"type": "object"},
    },
    {
        "name": "digital_capabilities",
        "description": "Truthful answer to what Evie can currently do with a service.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"q": {"type": "string"}},
        },
        "sensitive": False,
        "read_only": True,
        "risk_class": "R0",
        "permission": "mail:read",
        "undoable": False,
        "target_ownership": "owner",
        "provider": "digital",
        "output": {"type": "object"},
    },
]


_GMAIL_READ_OPS = frozenset({"search", "read", "list", "get", "summarize", "thread"})
_WHATSAPP_WRITE_OPS = frozenset({"send", "compose", "reply", "forward", "delete"})


def _inner_args(args: dict[str, Any]) -> dict[str, Any]:
    inner = args.get("args")
    return dict(inner) if isinstance(inner, dict) else {}


def _ask_text(inner: dict[str, Any], *, default: str) -> str:
    for key in ("text", "q", "query", "prompt", "goal"):
        value = str(inner.get(key) or "").strip()
        if value:
            return value
    return default


async def _mac_hub_digital_act(args: dict[str, Any]) -> dict[str, Any] | None:
    """Spark 1.3 digital.act reads the Mac hub, not Chrome WhatsApp Web / Gmail."""

    service = str(args.get("service") or "").strip().lower()
    operation = str(args.get("operation") or "").strip().lower()
    inner = _inner_args(args)
    if service in {"gmail", "mail"} and operation in _GMAIL_READ_OPS:
        from app.digital.orchestrate import _mac_mail_read

        mac = await _mac_mail_read(_ask_text(inner, default="recent mail"))
        return _hub_result(service="gmail", operation=operation, mac=mac)
    if service == "whatsapp" and operation not in _WHATSAPP_WRITE_OPS:
        from app.digital.orchestrate import _mac_whatsapp_read

        who = str(
            inner.get("query")
            or inner.get("chat_ref")
            or inner.get("to")
            or inner.get("name")
            or ""
        ).strip()
        mac = await _mac_whatsapp_read(_ask_text(inner, default="recent whatsapp"), who)
        return _hub_result(service="whatsapp", operation=operation, mac=mac)
    return None


def _hub_result(*, service: str, operation: str, mac: dict[str, Any] | None) -> dict[str, Any] | None:
    if not mac:
        return None
    spoken = str(mac.get("spoken") or "").strip()
    return {
        "status": mac.get("status"),
        "service": service,
        "operation": operation,
        "availability": "NATIVE",
        "error": None,
        "diagnosis": None,
        "verification": {"source": "live_mac", "verified": True},
        "clarify": None,
        "payload": mac,
        "spoken": spoken,
        "source": "live_mac",
    }


async def _park_digital_whatsapp(
    session: AsyncSession, args: dict[str, Any], *, actor: str
) -> dict[str, Any] | None:
    """Model-invoked WhatsApp writes park for one spoken yes, never autosend."""

    inner = _inner_args(args)
    to = str(inner.get("chat_ref") or inner.get("to") or inner.get("name") or "").strip()
    text = str(inner.get("text") or inner.get("body") or "").strip()
    if not to or not text:
        return None
    from app.ev.messaging.approval import park_send, question_for
    from app.ev.messaging.whatsapp_web import web_available

    if not await web_available():
        return None
    display = to
    try:
        from app.ev.messaging import whatsapp_web

        match = await whatsapp_web.resolve(to)
        if match.get("status") == "unique" and match.get("display"):
            display = str(match["display"])
    except Exception:
        pass
    action = await park_send(
        session, to=to, text=text, display=display, channel="whatsapp", actor=actor
    )
    return {
        "ok": False,
        "status": "WAITING_FOR_APPROVAL",
        "service": "whatsapp",
        "operation": str(args.get("operation") or "send"),
        "availability": "OPERATED",
        "error": "confirmation_required",
        "diagnosis": None,
        "verification": None,
        "clarify": None,
        "payload": {"prepared": True, "sent": False},
        "spoken": question_for(action),
        "action_id": str(action.id),
        "pending_approval": True,
    }


async def handle_digital_tool(session: AsyncSession, name: str, args: dict[str, Any], *, actor: str) -> dict[str, Any] | None:
    if name not in {s["name"] for s in DIGITAL_TOOL_SPECS}:
        return None
    if name == "digital_waiting":
        return owner_brief(GLOBAL_WAITING)
    if name == "digital_capabilities":
        return answer_can_you(str(args.get("q") or ""))
    ctx = OpContext(actor=actor, session=session, autonomy=AutonomyLevel.SEND_WITH_CONFIRMATION, confirmed=bool(args.get("confirmed")))
    if name == "digital_act":
        service = str(args.get("service") or "").strip().lower()
        operation = str(args.get("operation") or "").strip().lower()
        if service == "whatsapp" and operation in _WHATSAPP_WRITE_OPS:
            # A model-supplied confirmed flag is not human approval. Park the
            # prepared send for one spoken yes instead of autosending.
            pending = await _park_digital_whatsapp(session, args, actor=actor)
            if pending is not None:
                return pending
            ctx.confirmed = False
        hub = await _mac_hub_digital_act(args)
        if hub is not None:
            return hub
        result = await execute(str(args.get("service") or ""), str(args.get("operation") or ""), args.get("args") or {}, ctx=ctx)
        return result.as_model()
    if name == "digital_outcome":
        return await handle_outcome(str(args.get("text") or ""), ctx=ctx)
    return None
