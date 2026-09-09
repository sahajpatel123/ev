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
            "Operate the owner's digital services (Gmail, Contacts, Calendar, "
            "WhatsApp Web, browser, files) via semantic operations. Never pass "
            "passwords, tokens, cookies, CSS selectors, or coordinates."
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


async def handle_digital_tool(session: AsyncSession, name: str, args: dict[str, Any], *, actor: str) -> dict[str, Any] | None:
    if name not in {s["name"] for s in DIGITAL_TOOL_SPECS}:
        return None
    if name == "digital_waiting":
        return owner_brief(GLOBAL_WAITING)
    if name == "digital_capabilities":
        return answer_can_you(str(args.get("q") or ""))
    ctx = OpContext(actor=actor, session=session, autonomy=AutonomyLevel.SEND_WITH_CONFIRMATION, confirmed=bool(args.get("confirmed")))
    if name == "digital_act":
        result = await execute(str(args.get("service") or ""), str(args.get("operation") or ""), args.get("args") or {}, ctx=ctx)
        return result.as_model()
    if name == "digital_outcome":
        return await handle_outcome(str(args.get("text") or ""), ctx=ctx)
    return None
