"""Muse Spark 1.3 Contributor decides WHAT kind of owner turn this is.

Mini is the mouth and may still call tools. Spark pokes first on work-shaped
turns so gpt-realtime-2.1-mini does not improvise the job. Evie executes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger("ev.spark_act")

Act = Literal[
    "chat",
    "code",
    "look",
    "recall",
    "search",
    "files",
    "home",
    "computer",
    "life",
    "desk",
]
ACTS: tuple[Act, ...] = (
    "chat",
    "code",
    "look",
    "recall",
    "search",
    "files",
    "home",
    "computer",
    "life",
    "desk",
)
_SPARK_BUDGET_S = 3.5

_CHAT_RE = re.compile(
    r"^(?:(?:hey |hi |hello |evie )*)?(?:"
    r"how are you|what's up|thanks|thank you|good morning|good night|"
    r"ok(?:ay)?|yeah|yep|nope|hm+|mhm+|yes|no"
    r")\s*[.!]?\s*$",
    re.IGNORECASE,
)
_ASK_RE = re.compile(
    r"\b(?:can you|could you|would you|please|"
    r"i want you to|i need you to|help me|go ahead and)\b",
    re.IGNORECASE,
)
_BLOCKED_LIVE_TOOLS = frozenset(
    {"execute_command", "drone", "print_start", "camera_replay", "ticket_buy"}
)

_ACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "act": {
            "type": "string",
            "enum": list(ACTS),
        }
    },
    "required": ["act"],
}

_SPARK_SYSTEM = """You are Evie's turn brain (Muse Spark 1.3 Contributor). Mini will speak. Evie will execute. You only decide WHAT this owner turn is.

act:
- chat: small talk, feelings, opinion, a question you can answer from this conversation. Not a job.
- code: write, edit, run, or build software / a UI / a site / a script.
- look: see what is in view NOW (camera).
- recall: stored life — WhatsApp, who is waiting, colliding plans, where a chat was left, how a thread has been, who starts chats, summaries, last messages, people they talk to, photos/notes already stored.
- search: look up on the web, weather, current facts.
- files: open/move/rename/find a file on the laptop (not write a program).
- home: lights, locks, garage, house devices.
- computer: open/quit an app, click the Mac, drive the desktop.
- life: send a message, place a call, live inbox/mail/calendar right now.
- desk: pin, HUD, notes, a physical thing on the Mac desk. Not a WhatsApp recap.

WhatsApp hanging / leave-it / colliding plans / thread climate is recall, not chat, not desk.
If they asked Evie to DO something in the world or in a project, it is not chat.
Return JSON only.
"""

_OBVIOUS_NO_WAIT = frozenset({"code", "search", "home"})
_SPARK_WORK = frozenset({"look", "recall", "life", "desk", "files", "computer"})


@dataclass(frozen=True)
class ActDecision:
    act: Act
    source: str = "fallback"


def maybe_spark_act_utterance(text: str | None) -> bool:
    """True when Spark should poke. Greetings stay Mini."""

    raw = (text or "").strip()
    if not raw or len(raw) < 8:
        return False
    if _CHAT_RE.match(raw):
        return False
    from app.ev.code_studio import looks_like_background_task_ops, looks_like_long_code_goal
    from app.ev.luna_code import looks_like_code_continue, looks_like_code_request
    from app.ev.spark_look import maybe_camera_utterance

    if looks_like_long_code_goal(raw) or looks_like_background_task_ops(raw):
        return False
    if looks_like_code_request(raw) or looks_like_code_continue(raw):
        return True
    if maybe_camera_utterance(raw):
        return True
    if re.search(
        r"\b(?:"
        r"mail|inbox|message|text|call|calendar|contact|"
        r"search|google|weather|look up|"
        r"open|quit|close|click|file|folder|desktop|"
        r"light|lock|garage|camera|remember|what did|"
        r"build|create|make|write|code|website|app ui|"
        r"hanging|gotten back|collid(?:e|ing)|leave it with|"
        r"how have things been|usually starts|summarize|recap|"
        r"catch me up|any word from|whatsapp|conversations with"
        r")\b",
        raw,
        re.IGNORECASE,
    ):
        return True
    # Ask Evie to *do* something, not every eight-word ramble.
    return bool(_ASK_RE.search(raw) and len(raw.split()) >= 5)


def fallback_act(utterance: str) -> ActDecision | None:
    """Cheap map when Spark is dark. None means Mini may talk."""

    raw = (utterance or "").strip()
    if not raw or _CHAT_RE.match(raw):
        return None
    from app.ev.code_studio import looks_like_long_code_goal
    from app.ev.luna_code import looks_like_code_continue, looks_like_code_request
    from app.ev.spark_look import fallback_camera_action

    if looks_like_long_code_goal(raw) or looks_like_code_request(raw) or looks_like_code_continue(raw):
        return ActDecision(act="code", source="fallback")
    camera = fallback_camera_action(raw)
    if camera == "look":
        return ActDecision(act="look", source="fallback")
    if camera == "recall":
        return ActDecision(act="recall", source="fallback")
    from app.search.live import is_weather_query, looks_world_knowledge
    from app.ev.tool_select import SEARCH_WEB_RE

    if is_weather_query(raw) or SEARCH_WEB_RE.search(raw) or looks_world_knowledge(raw):
        return ActDecision(act="search", source="fallback")
    from app.ev.home import parse_home_act

    if parse_home_act(raw):
        return ActDecision(act="home", source="fallback")
    from app.ev.laptop_files import looks_like_file_task

    if looks_like_file_task(raw):
        return ActDecision(act="files", source="fallback")
    from app.ev.computer_strategy import looks_like_computer_task

    if looks_like_computer_task(raw):
        return ActDecision(act="computer", source="fallback")
    from app.ev.tool_select import resolve_live_action

    resolved = resolve_live_action(raw)
    if resolved is None:
        return None
    name = resolved[0]
    if name in {"list_mail", "list_messages", "calendar_read", "place_call", "send_message", "resolve_contact"}:
        return ActDecision(act="life", source="fallback")
    if name in {"search_memory", "recall", "recall_history"}:
        return ActDecision(act="recall", source="fallback")
    if name == "look":
        return ActDecision(act="look", source="fallback")
    if name == "search_web":
        return ActDecision(act="search", source="fallback")
    if name == "code":
        return ActDecision(act="code", source="fallback")
    if name in {"open_app", "close_app", "open_url"}:
        return ActDecision(act="computer", source="fallback")
    if name in {"home_act", "home_status"}:
        return ActDecision(act="home", source="fallback")
    return None


async def decide_owner_act(utterance: str) -> ActDecision | None:
    """Spark pokes work turns. Look/recall go through Spark; obvious code/search/home do not wait."""

    fallback = fallback_act(utterance)
    if fallback is not None and fallback.act in _OBVIOUS_NO_WAIT:
        return fallback
    should_poke = maybe_spark_act_utterance(utterance) or (
        fallback is not None and fallback.act in _SPARK_WORK
    )
    if should_poke:
        sparked = await _spark_decide(utterance)
        if sparked in ACTS and sparked != "chat":
            logger.warning("spark_act act=%s source=spark", sparked)
            return ActDecision(act=sparked, source="spark")
        if sparked == "chat" and fallback is not None and fallback.act != "chat":
            return fallback
    return fallback


def live_tool_for_act(text: str, decision: ActDecision) -> tuple[str, dict[str, Any]] | None:
    """Turn a Spark/fallback act into a live tool call. None = Mini may talk."""

    raw = (text or "").strip()
    act = decision.act
    if act == "chat":
        return None
    if act == "code":
        return _safe_live_tool("code", {"goal": raw[:4000]})
    if act == "look":
        return _safe_live_tool("look", {"prompt": raw[:400], "focus": "auto"})
    if act == "recall":
        from app.ev.tool_select import resolve_live_action

        resolved = resolve_live_action(raw)
        if resolved is not None and resolved[0] in {"recall", "recall_history", "search_memory"}:
            return _safe_live_tool(*resolved)
        return _safe_live_tool("recall", {"query": raw[:1000]})
    if act == "search":
        from app.search.live import is_weather_query

        if is_weather_query(raw):
            return _safe_live_tool("get_weather", {"query": raw[:400]})
        return _safe_live_tool("search_web", {"query": raw[:400]})
    if act == "files":
        return _safe_live_tool("computer", {"goal": raw[:500]})
    if act == "computer":
        from app.ev.laptop_files import looks_like_file_task, parse_file_goal
        from app.ev.desk_meaning import looks_like_desk_job, spark_desk_candidate

        if (
            parse_file_goal(raw) is not None
            or looks_like_file_task(raw)
            or looks_like_desk_job(raw)
            or spark_desk_candidate(raw)
        ):
            return _safe_live_tool("computer", {"goal": raw[:500]})
        from app.ev.tool_select import resolve_live_action

        resolved = resolve_live_action(raw)
        if resolved is not None and resolved[0] in {"open_app", "close_app", "open_url", "computer"}:
            return _safe_live_tool(*resolved)
        return _safe_live_tool("computer", {"goal": raw[:500]})
    if act == "home":
        from app.ev.home import parse_home_act

        parsed = parse_home_act(raw)
        if parsed:
            return _safe_live_tool("home_act", parsed)
        return _safe_live_tool("home_status", {})
    if act == "life":
        from app.ev.tool_select import resolve_live_action

        resolved = resolve_live_action(raw)
        if resolved is not None:
            return _safe_live_tool(*resolved)
        return _safe_live_tool("search_memory", {"query": raw[:400]})
    if act == "desk":
        from app.ev.desk_acts import parse_desk_act

        desk = parse_desk_act(raw)
        if desk is not None and desk.get("channel") == "tool":
            return _safe_live_tool(str(desk["name"]), dict(desk.get("args") or {}))
        from app.ev.laptop_files import looks_like_file_task, parse_file_goal
        from app.ev.desk_meaning import looks_like_desk_job, spark_desk_candidate

        if (
            parse_file_goal(raw) is not None
            or looks_like_file_task(raw)
            or looks_like_desk_job(raw)
            or spark_desk_candidate(raw)
        ):
            return _safe_live_tool("computer", {"goal": raw[:500]})
        return None
    return None


def _safe_live_tool(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    from app.ev.tool_select import LIVE_VOICE_TOOLS

    if name in _BLOCKED_LIVE_TOOLS or name not in LIVE_VOICE_TOOLS:
        return None
    return name, arguments


async def _spark_decide(utterance: str) -> str | None:
    from app.gateway.muse import (
        MuseProviderUnavailable,
        muse_spark_key_loaded,
        muse_spark_model,
    )

    if not (utterance or "").strip():
        return None
    # Mini stays the mouth. Spark pokes work turns whenever OpenCode is
    # provisioned — do not wait for EV_CHAT_PROVIDER to be Muse.
    if not muse_spark_key_loaded():
        return None
    try:
        from app.contracts import ChatMessage
        from app.gateway.muse_spark import muse_spark_provider

        result = await asyncio.wait_for(
            muse_spark_provider().chat_structured(
                [
                    ChatMessage(role="system", content=_SPARK_SYSTEM),
                    ChatMessage(
                        role="user",
                        content=f"Owner said: {(utterance or '')[:1500]}",
                    ),
                ],
                schema=_ACT_SCHEMA,
                schema_name="owner_act",
                model=muse_spark_model(),
                reasoning_effort="low",
            ),
            timeout=_SPARK_BUDGET_S,
        )
    except (MuseProviderUnavailable, asyncio.TimeoutError):
        logger.info("spark_act unavailable")
        return None
    except Exception:  # noqa: BLE001 - Mini must still be able to talk
        logger.info("spark_act failed", exc_info=True)
        return None
    return _parse_act(result.text or "")


def _parse_act(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        lowered = text.lower()
        return next((item for item in ACTS if item in lowered), None)
    if not isinstance(data, dict):
        return None
    act = str(data.get("act") or "").strip().lower()
    return act if act in ACTS else None
