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
    "evie_turn",
)
PHONE_TOOL_ALIASES = {
    "timer": "start_timer",
    "reminder": "set_reminder",
    "weather": "get_weather",
    "calendar": "calendar_read",
    "mail": "list_mail",
    "messages": "list_messages",
    "message": "send_message",
    "call": "place_call",
}

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
_HEALTH_RE = re.compile(
    r"\b(?:healthkit|steps?|heart rate|sleep|calories|blood pressure)\b",
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
        "entity": {"type": "string"},
        "action": {"type": "string"},
        "channel": {"type": "string"},
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
- send_message (to, text, channel), place_call (name)
- send_message channel must match what the owner said: "whatsapp" when they
  say WhatsApp, "mail" when they say email, otherwise omit channel
  (Home Station defaults to Messages). Never invent a channel.
- home_act only for reversible lights on/off actions
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
    return bool(_CHAT_RE.search(raw) and not _ACTION_ISH_RE.search(raw))


def should_ask_spark(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or _HEALTH_RE.search(raw) or looks_like_phone_chat(raw):
        return False
    return bool(_ACTION_ISH_RE.search(raw) or len(raw.split()) >= 4)


def _fill_phone_args(
    tool: str, args: dict[str, Any], utterance: str
) -> tuple[str, dict[str, Any]]:
    """Deterministically fill owner-derived args the decision model must not invent."""

    if tool == "evie_turn" and not args.get("owner_turn"):
        args["owner_turn"] = (utterance or "").strip()[:2000]
    if tool in {"recall", "search_memory"} and not args.get("query"):
        args["query"] = (utterance or "").strip()[:1000]
    if tool == "computer" and not args.get("goal"):
        args["goal"] = (utterance or "").strip()[:500]
    if tool == "code" and not args.get("goal"):
        args["goal"] = (utterance or "").strip()[:4000]
    return tool, args


async def _jev_decide_tool(utterance: str) -> tuple[str, dict[str, Any]] | None:
    """JEV owns phone/Mac tool choice: one finite tool, deterministic args."""

    from app.gateway.openrouter_jev import JevQuestion, OpenRouterJevError
    from app.gateway.roles import answer_choice, decide_via_role

    descriptions = {
        "chat": "Small talk, greeting, confirmation, or a non-action question.",
        "open_app": "Open an app (name = app).",
        "close_app": "Quit an app (name = app).",
        "activate_app": "Bring an app to the front (name = app).",
        "list_apps": "List running or available apps.",
        "computer_status": "Mac status / what is happening on the computer.",
        "start_timer": "Start a timer (minutes).",
        "cancel_timer": "Cancel a timer.",
        "list_timers": "List timers.",
        "set_reminder": "Set a reminder (text).",
        "list_reminders": "List reminders.",
        "cancel_reminder": "Cancel a reminder.",
        "get_weather": "Weather.",
        "calendar_read": "Read the calendar / events.",
        "list_mail": "Read email / inbox.",
        "list_messages": "Read text messages.",
        "list_protocols": "Owner routines / protocols.",
        "brief_me": "Daily or morning briefing.",
        "home_status": "Smart-home status.",
        "home_act": "Smart-home action.",
        "calculate": "Do math.",
        "search_memory": "Search Evie's memory.",
        "recall": "Recall something from memory.",
        "get_person": "Look up a person.",
        "resolve_contact": "Resolve a contact.",
        "heading_out": "Leaving / lock-up routine.",
        "present": "Show or present something.",
        "send_message": "Send a text message.",
        "place_call": "Place a phone call.",
        "evie_turn": "Ask Evie a general turn (owner_turn).",
    }
    criteria = {tool: descriptions.get(tool, tool.replace("_", " ")) for tool in ("chat", *PHONE_MAC_TOOLS)}
    try:
        call = await decide_via_role(
            {
                "transcript": (utterance or "")[:1500],
                "instructions": (
                    "Choose exactly one Home Station tool for this owner turn, or chat "
                    "when it is not an action. Do not invent arguments."
                ),
            },
            {
                "tool": JevQuestion(
                    type="choice",
                    instructions="Which tool should Evie use, or chat?",
                    criteria=criteria,
                )
            },
            actor="spark_phone",
        )
    except OpenRouterJevError:
        logger.info("jev phone-tool decision unavailable")
        return None
    if call.status != "ok":
        logger.info("jev phone-tool decision failed: %s", call.error)
        return None
    tool = answer_choice(call, "tool")
    if tool is None or tool == "chat":
        return None
    return _fill_phone_args(tool, {}, utterance)


async def spark_phone_tool(utterance: str) -> tuple[str, dict[str, Any]] | None:
    from app.gateway.muse import MuseProviderUnavailable, jev_kernel_active
    from app.gateway.openrouter_jev import OpenRouterJevError
    from app.gateway.roles import (
        chat_structured_via_role,
        text_brain_active,
        text_role_available,
    )

    if not should_ask_spark(utterance):
        return None
    if not text_brain_active() or not text_role_available():
        return None
    if jev_kernel_active():
        return await _jev_decide_tool(utterance)
    try:
        from app.contracts import ChatMessage

        result = await asyncio.wait_for(
            chat_structured_via_role(
                [
                    ChatMessage(role="system", content=_SPARK_SYSTEM),
                    ChatMessage(role="user", content=f"Owner said: {(utterance or '')[:1500]}"),
                ],
                schema=_SCHEMA,
                schema_name="phone_mac_tool",
            ),
            timeout=_SPARK_BUDGET_S,
        )
    except (TimeoutError, MuseProviderUnavailable, OpenRouterJevError):
        logger.info("spark_phone unavailable")
        return None
    except Exception:
        logger.info("spark_phone failed", exc_info=True)
        return None
    parsed = _parse_tool(getattr(result, "text", None) or "")
    if parsed is None:
        return None
    tool, args = parsed
    logger.warning("spark_phone tool=%s", tool)
    return _fill_phone_args(tool, args, utterance)


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
    tool = str(data.get("tool") or "chat").strip().lower()
    tool = PHONE_TOOL_ALIASES.get(tool, tool)
    if tool in {"", "chat"} or tool not in PHONE_MAC_TOOLS:
        return None
    args: dict[str, Any] = {}
    for key in (
        "name",
        "minutes",
        "text",
        "to",
        "url",
        "query",
        "goal",
        "expression",
        "entity",
        "action",
        "channel",
    ):
        value = data.get(key)
        if value is None or value == "":
            continue
        args[key] = value
    return tool, args
