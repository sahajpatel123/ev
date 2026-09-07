"""Muse Spark 1.3 picks a Home Station tool for an iPhone request.

Evie on the phone cannot run Clock/Reminders/Mail itself. Spark decides
WHICH Mac/Core tool to run; dispatch executes. Chat returns no tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

logger = logging.getLogger("ev.spark_phone")

_SPARK_BUDGET_S = 6.0

PHONE_MAC_TOOLS = (
    "open_app",
    "close_app",
    "activate_app",
    "list_apps",
    "computer_status",
    "open_url",
    "start_timer",
    "cancel_timer",
    "list_timers",
    "set_reminder",
    "list_reminders",
    "cancel_reminder",
    "get_weather",
    "calendar_read",
    "list_mail",
    "list_messages",
    "list_protocols",
    "brief_me",
    "home_status",
    "home_act",
    "calculate",
    "search_memory",
    "recall",
    "get_person",
    "resolve_contact",
    "heading_out",
    "present",
    "send_message",
    "place_call",
    "computer",
    "code",
    "evie_turn",
)

_CHAT_RE = re.compile(
    r"^(?:hi|hello|hey|yo|yes|yeah|yep|ok|okay|no|nope|thanks|thank you|"
    r"can you hear me|are you (?:there|listening)|evie)\b",
    re.I,
)
_ACTION_ISH_RE = re.compile(
    r"\b("
    r"open|close|start|set|send|text|call|remind|timer|alarm|"
    r"calendar|mail|email|message|inbox|weather|play|pause|"
    r"turn (?:on|off)|lights?|lock|unlock|brief|what's on|"
    r"calculator|safari|spotify|notes|reminders"
    r")\b",
    re.I,
)

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "tool": {
            "type": "string",
            "enum": ["chat", *PHONE_MAC_TOOLS],
        },
        "name": {"type": "string"},
        "minutes": {"type": "number"},
        "text": {"type": "string"},
        "to": {"type": "string"},
        "url": {"type": "string"},
        "query": {"type": "string"},
        "goal": {"type": "string"},
        "expression": {"type": "string"},
    },
    "required": ["tool"],
}

_SPARK_SYSTEM = """You are Evie's action brain (Muse Spark 1.3 Contributor) for a trusted iPhone.
Evie executes on Home Station (the Mac). You only choose WHICH tool, or chat.

tool=chat when they are greeting, confirming hearing, small talk, or a question that is not an action.
Otherwise pick one Home Station tool:
- open_app / close_app / activate_app (name = app)
- start_timer (minutes)
- set_reminder (text)
- get_weather, calendar_read, list_mail, list_messages, brief_me, home_status
- send_message (to, text), place_call (name)
- home_act for lights/locks
- computer for a Mac UI/file job (goal)
- code for a coding job (goal)
- calculate (expression)
- recall / search_memory / get_person / resolve_contact
- present to show the Mac HUD
- evie_turn for projects/goals/commitments (text = their words)

Never invent that the iPhone Clock or Reminders app ran. Home Station runs it.
Return JSON only.
"""


def looks_like_phone_chat(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    if _CHAT_RE.search(raw) and not _ACTION_ISH_RE.search(raw):
        return True
    return False


def should_ask_spark(text: str) -> bool:
    raw = (text or "").strip()
    if looks_like_phone_chat(raw):
        return False
    return bool(_ACTION_ISH_RE.search(raw) or len(raw.split()) >= 4)


async def spark_phone_tool(utterance: str) -> tuple[str, dict[str, Any]] | None:
    from app.gateway.muse import (
        MuseProviderUnavailable,
        muse_intelligence_active,
        muse_spark_key_loaded,
        muse_spark_model,
    )

    if not should_ask_spark(utterance):
        return None
    if not muse_intelligence_active() or not muse_spark_key_loaded():
        return None
    try:
        from app.contracts import ChatMessage
        from app.gateway.muse_spark import muse_spark_provider

        result = await asyncio.wait_for(
            muse_spark_provider().chat_structured(
                [
                    ChatMessage(role="system", content=_SPARK_SYSTEM),
                    ChatMessage(role="user", content=f"Owner said: {(utterance or '')[:1500]}"),
                ],
                schema=_SCHEMA,
                schema_name="phone_mac_tool",
                model=muse_spark_model(),
            ),
            timeout=_SPARK_BUDGET_S,
        )
    except (MuseProviderUnavailable, asyncio.TimeoutError):
        logger.info("spark_phone unavailable")
        return None
    except Exception:
        logger.info("spark_phone failed", exc_info=True)
        return None
    parsed = _parse_tool(getattr(result, "text", None) or "")
    if parsed is None:
        return None
    tool, args = parsed
    if tool == "evie_turn" and not args.get("owner_turn"):
        args["owner_turn"] = (utterance or "").strip()[:2000]
    if tool in {"recall", "search_memory"} and not args.get("query"):
        args["query"] = (utterance or "").strip()[:1000]
    if tool == "computer" and not args.get("goal"):
        args["goal"] = (utterance or "").strip()[:500]
    if tool == "code" and not args.get("goal"):
        args["goal"] = (utterance or "").strip()[:4000]
    logger.warning("spark_phone tool=%s", tool)
    return tool, args


def _parse_tool(raw: str) -> tuple[str, dict[str, Any]] | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    tool = str(data.get("tool") or "chat").strip()
    if tool in {"", "chat"} or tool not in PHONE_MAC_TOOLS:
        return None
    args: dict[str, Any] = {}
    for key in ("name", "minutes", "text", "to", "url", "query", "goal", "expression"):
        value = data.get(key)
        if value is None or value == "":
            continue
        args[key] = value
    return tool, args
