"""MiMo picks a Home Station tool for an iPhone request.

Evie on the phone cannot run Clock/Reminders/Mail itself. MiMo decides
WHICH Mac/Core tool to run via one finite choice; dispatch executes.
Chat returns no tool.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("ev.spark_phone")

PHONE_MAC_TOOLS = (
    "open_app",
    "close_app",
    "activate_app",
    "list_apps",
    "computer_status",
    "screen_look",
    "inspect_ui",
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


async def _mimo_decide_tool(utterance: str) -> tuple[str, dict[str, Any]] | None:
    """MiMo owns phone/Mac tool choice: one finite tool, deterministic args."""

    from app.gateway.openrouter_mimo import MimoEgressDenied, MimoUnavailable
    from app.gateway.roles import DecisionQuestion, answer_choice, decide_via_role

    descriptions = {
        "chat": "Small talk, greeting, confirmation, or a non-action question.",
        "open_app": "Open an app (name = app).",
        "close_app": "Quit an app (name = app).",
        "activate_app": "Bring an app to the front (name = app).",
        "list_apps": "List running or available apps.",
        "computer_status": "Mac status / what is happening on the computer.",
        "screen_look": "Look at the Mac screen (read-only observe, never control).",
        "inspect_ui": "Read the Mac screen's UI elements (read-only observe, never control).",
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
                "tool": DecisionQuestion(
                    type="choice",
                    instructions="Which tool should Evie use, or chat?",
                    criteria=criteria,
                )
            },
            actor="spark_phone",
        )
    except (MimoUnavailable, MimoEgressDenied):
        logger.info("mimo phone-tool decision unavailable")
        return None
    if call.status != "ok":
        logger.info("mimo phone-tool decision failed: %s", call.error)
        return None
    tool = answer_choice(call, "tool")
    if tool is None or tool == "chat":
        return None
    return _fill_phone_args(tool, {}, utterance)


async def spark_phone_tool(utterance: str) -> tuple[str, dict[str, Any]] | None:
    from app.gateway.roles import text_role_available

    if not should_ask_spark(utterance):
        return None
    if not text_role_available():
        return None
    decided = await _mimo_decide_tool(utterance)
    if decided is not None:
        logger.warning("spark_phone tool=%s", decided[0])
    return decided
