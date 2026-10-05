"""MiMo-facing Digital Operations tools. No credentials, selectors, or cookies."""

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
            "chats use Apple Mail, iMessage, or the dedicated background WhatsApp connection. "
            "Never open visible windows. Never pass passwords, tokens, "
            "cookies, CSS selectors, or coordinates."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "service": {"type": "string", "enum": ["gmail", "contacts", "calendar", "whatsapp", "browser", "files", "phone"]},
                "operation": {"type": "string"},
                "args": {"type": "object"},
                # Accepted for caller compatibility only. A flag supplied in
                # tool arguments is never human approval and is ignored.
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
_WHATSAPP_WRITE_OPS = frozenset({"send", "reply"})


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
    """MiMo digital.act reads the Mac hub, not Chrome WhatsApp Web / Gmail."""

    service = str(args.get("service") or "").strip().lower()
    operation = str(args.get("operation") or "").strip().lower()
    inner = _inner_args(args)
    if service in {"gmail", "mail"} and operation in _GMAIL_READ_OPS:
        from app.digital.orchestrate import _mac_mail_read

        mac = await _mac_mail_read(_ask_text(inner, default="recent mail"))
        return _hub_result(service="gmail", operation=operation, mac=mac)
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
    session: AsyncSession, args: dict[str, Any], *, actor: str,
    live_session_id: str | None = None, device_id: Any = None,
) -> dict[str, Any]:
    """Use the same bound background approval as life.send; never autosend."""
    from app.ev.tools import _park_whatsapp_send

    inner = _inner_args(args)
    if any(inner.get(key) for key in ("attachment", "attachments", "attachment_ref", "file", "file_path")):
        return {
            "ok": False, "sent": False, "status": "FAILED", "service": "whatsapp",
            "operation": str(args.get("operation") or "send"),
            "error": "whatsapp_attachment_unsupported",
            "spoken": "This background connection cannot send attachments yet. I didn't prepare or send a text-only substitute.",
        }
    payload = await _park_whatsapp_send(
        session,
        {"to": inner.get("chat_ref") or inner.get("to") or inner.get("name"),
         "text": inner.get("text") or inner.get("body")},
        actor=actor, live_session_id=live_session_id, device_id=device_id, channel="digital",
    )
    return {
        **payload, "service": "whatsapp", "operation": str(args.get("operation") or "send"),
        "error": "confirmation_required" if payload.get("pending_approval") else payload.get("error"),
        "status": "WAITING_FOR_APPROVAL" if payload.get("pending_approval") else "FAILED",
        "payload": {"prepared": bool(payload.get("pending_approval")), "sent": False},
    }


async def handle_digital_tool(session: AsyncSession, name: str, args: dict[str, Any], *, actor: str, live_session_id: str | None = None, device_id: Any = None) -> dict[str, Any] | None:
    if name not in {s["name"] for s in DIGITAL_TOOL_SPECS}:
        return None
    if name == "digital_waiting":
        return owner_brief(GLOBAL_WAITING)
    if name == "digital_capabilities":
        return answer_can_you(str(args.get("q") or ""))
    # `confirmed` may arrive in the tool arguments; it is model-supplied, never
    # human approval, so it is never authoritative here. Operations that need
    # the owner's yes park for a spoken confirmation (see
    # _park_digital_whatsapp) and are only executed with a confirmation the
    # owner actually gave.
    ctx = OpContext(actor=actor, session=session, autonomy=AutonomyLevel.SEND_WITH_CONFIRMATION, confirmed=False)
    if name == "digital_act":
        service = str(args.get("service") or "").strip().lower()
        operation = str(args.get("operation") or "").strip().lower()
        if service == "whatsapp" and operation in _WHATSAPP_WRITE_OPS:
            return await _park_digital_whatsapp(
                session, args, actor=actor, live_session_id=live_session_id, device_id=device_id
            )
        hub = await _mac_hub_digital_act(args)
        if hub is not None:
            return hub
        result = await execute(str(args.get("service") or ""), str(args.get("operation") or ""), args.get("args") or {}, ctx=ctx)
        return result.as_model()
    if name == "digital_outcome":
        return await handle_outcome(str(args.get("text") or ""), ctx=ctx)
    return None
