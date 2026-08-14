"""Pre-LLM intelligence briefing: run existing layers before the model speaks.

Opencode does not honor native function calling reliably. Voice used to dump
every tool into the prompt and hope for JSON. This module selects 1–3 read
tools, dispatches them, and injects cited results so DeepSeek composes wording
instead of inventing facts.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.contracts import ToolCall
from app.ev import health_radar, navigation, tools
from app.ev.actions import LIFE_ACTION_NAMES
from app.ev.interaction import detect_life_action
from app.ev.tool_select import CAPABILITIES_RE, NAV_RE, SHOW_PHRASE_RE, TIME_RE, select_tool
from app.search.live import extract_place, is_weather_query, looks_world_knowledge

BRIEFING_TIMEOUT_SECONDS = 8.0
VOICE_BRIEFING_TIMEOUT_SECONDS = 3.5
MAX_PREFETCH = 3
RESULT_CHARS = 900

WRITE_TOOLS = frozenset(
    {"send_message", "place_call", "open_url", "set_reminder", "present"}
)
CORE_TURN_TOOLS = (
    "search_memory",
    "search_web",
    "get_weather",
    "calculate",
    "present",
    "get_upcoming_alerts",
)
LIFE_TURN_TOOLS = (
    "resolve_contact",
    "send_message",
    "list_messages",
    "place_call",
    "list_mail",
    "set_reminder",
)
CAPABILITIES_TEXT = (
    "Named companion EVIE. Memory, decisions, timeline. Live weather. Web lookup. "
    "Clock, calendar, leave-by. Health trends when asked. Gear/battery. People in "
    "memory. Research and maker projects. Safe math. HUD/lookout via present. "
    "Messages, calls, mail, reminders through granted Mac/iPhone bridges. "
    "Not city surveillance, not weapons, not a host-model brand."
)

_PERCENT_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*%\s+of\s+(\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)
_EXPR_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?(?:\s*[\+\-\*/%^]\s*\d[\d,]*(?:\.\d+)?)+)"
)
_PERSON_RE = re.compile(
    r"\b(?:where(?:'s| is)|who(?:'s| is)|how's|how is|text|message|call|ring|"
    r"facetime)\s+(?:my\s+\w+\s+)?([A-Z][a-z]+)\b"
)
_RELATION_RE = re.compile(
    r"\b(Mom|Dad|Mother|Father|Mama|Papa|Mum)\b"
)
_RELATION_PHRASE_RE = re.compile(
    r"\b(?:my|our)\s+(?:friend|colleague|boss|manager|mom|dad|mother|father|"
    r"brother|sister|wife|husband|partner|girlfriend|boyfriend|roommate|"
    r"neighbor)\s+([A-Z][a-z]+)",
    re.IGNORECASE,
)
_PROJECT_RE = re.compile(
    r"(?:the\s+)?(.+?)\s+project\b",
    re.IGNORECASE,
)
_HEALTH_METRICS = (
    ("sleep", "sleep_hours"),
    ("hrv", "hrv_ms"),
    ("heart", "resting_hr"),
    ("steps", "steps"),
    ("mood", "mood"),
    ("readiness", "sleep_hours"),
)


def extract_expression(message: str) -> str | None:
    percent = _PERCENT_RE.search(message or "")
    if percent:
        left = percent.group(1).replace(",", "")
        right = percent.group(2).replace(",", "")
        return f"({left}/100)*{right}"
    normalized = (message or "").replace("×", "*").replace("÷", "/").replace(" x ", " * ")
    match = _EXPR_RE.search(normalized)
    if not match:
        return None
    return match.group(1).replace(",", "")


def extract_person_name(message: str) -> str | None:
    relation = _RELATION_RE.search(message or "")
    if relation:
        return relation.group(1)
    phrase = _RELATION_PHRASE_RE.search(message or "")
    if phrase:
        return phrase.group(1)
    named = _PERSON_RE.search(message or "")
    if named:
        return named.group(1)
    return None


def infer_health_metric(message: str) -> str:
    lowered = (message or "").lower()
    for token, metric in _HEALTH_METRICS:
        if token in lowered:
            return metric
    return "sleep_hours"


_SEND_TO_RE = re.compile(
    r"\b(?:text|message|sms|imessage|whatsapp)\s+"
    r"(?:my\s+)?(?P<to>Mom|Dad|Mother|Father|Mama|Papa|Mum|[A-Z][\w.'-]+)\s+"
    r"(?:that\s+|saying\s+|[:\-]\s*)?(?P<text>.+)$",
    re.IGNORECASE,
)
_SEND_TO_ALT_RE = re.compile(
    r"\bsend\s+(?:a\s+|an\s+)?(?:text|message|sms|note)\s+to\s+"
    r"(?:my\s+)?(?P<to>Mom|Dad|Mother|Father|Mama|Papa|Mum|[A-Z][\w.'-]+)\s+"
    r"(?:that\s+|saying\s+|[:\-]\s*)?(?P<text>.+)$",
    re.IGNORECASE,
)
_CALL_TO_RE = re.compile(
    r"\b(?:call|ring|phone|facetime)\s+"
    r"(?:my\s+)?(?P<to>Mom|Dad|Mother|Father|Mama|Papa|Mum|[A-Z][\w.'-]+)\b",
    re.IGNORECASE,
)
_REMIND_RE = re.compile(
    r"\b(?:remind\s+me(?:\s+to)?|set\s+(?:a\s+|an\s+)?reminder(?:\s+to)?)\s+"
    r"(?P<text>.+)$",
    re.IGNORECASE,
)
_OPEN_URL_RE = re.compile(r"(https?://\S+)", re.IGNORECASE)


def infer_send_message_args(message: str) -> dict[str, Any] | None:
    """Extract ``to`` + ``text`` for an explicit send/text command."""

    text = (message or "").strip()
    if not text:
        return None
    for pattern in (_SEND_TO_RE, _SEND_TO_ALT_RE):
        match = pattern.search(text)
        if match:
            to = match.group("to").strip()
            body = (match.group("text") or "").strip()
            if to and body:
                return {"to": to, "text": body}
    person = extract_person_name(text)
    if not person:
        return None
    remainder = re.sub(
        rf"^\s*(?:please\s+)?(?:text|message|sms|imessage|whatsapp|send(?:\s+a)?(?:\s+text|\s+message)?)\s+"
        rf"(?:to\s+)?(?:my\s+)?{re.escape(person)}\s*(?:that\s+|saying\s+|[:\-]\s*)?",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    if remainder and remainder.lower() != text.lower():
        return {"to": person, "text": remainder}
    return None


def infer_write_args(name: str, message: str) -> dict[str, Any] | None:
    """Arguments for a write/life tool the owner just asked to run."""

    if name == "send_message":
        return infer_send_message_args(message)
    if name == "place_call":
        match = _CALL_TO_RE.search(message or "")
        person = match.group("to").strip() if match else extract_person_name(message)
        return {"to": person} if person else None
    if name == "set_reminder":
        match = _REMIND_RE.search(message or "")
        body = (match.group("text") if match else None) or (message or "").strip()
        body = body.strip()
        return {"text": body} if body else None
    if name == "open_url":
        match = _OPEN_URL_RE.search(message or "")
        return {"url": match.group(1)} if match else None
    if name == "present":
        body = (message or "").strip()
        if not body:
            return None
        return {"title": "EVIE", "body": body[:4000]}
    return None


def plan_life_tool_calls(message: str, offered: set[str]) -> list[ToolCall]:
    """Deterministic write-tool plan when the model only described the action.

    Prefetch still refuses write tools via ``infer_args``; this planner is the
    tool-loop fallback so OpenCode-shaped replies still hit ``dispatch``.
    """

    selection = select_tool(message)
    life = detect_life_action(message)
    want: str | None = None
    if life == "send_message" or selection.selected == "send_message":
        want = "send_message"
    elif life == "phone_call" or selection.selected == "place_call":
        want = "place_call"
    elif life == "reminder" or selection.selected == "set_reminder":
        want = "set_reminder"
    elif selection.selected == "open_url":
        want = "open_url"
    elif selection.selected == "present":
        want = "present"
    if want is None or want not in offered:
        return []
    calls: list[ToolCall] = []
    if want in {"send_message", "place_call"} and "resolve_contact" in offered:
        person = extract_person_name(message)
        if person:
            calls.append(
                ToolCall(
                    id="plan-resolve",
                    name="resolve_contact",
                    arguments={"name": person, "limit": 5},
                )
            )
    args = infer_write_args(want, message)
    if not args:
        return []
    calls.append(ToolCall(id=f"plan-{want}", name=want, arguments=args))
    return calls


def infer_args(name: str, message: str) -> dict[str, Any] | None:
    """Build dispatcher arguments, or None when the tool must not auto-run."""

    if name in WRITE_TOOLS:
        return None
    if name == "calculate":
        expression = extract_expression(message)
        return {"expression": expression} if expression else None
    if name == "get_weather":
        args: dict[str, Any] = {"query": (message or "weather")[:200]}
        place = extract_place(message)
        if place:
            args["place"] = place
        return args
    if name == "search_web":
        return {"query": (message or "")[:1000], "limit": 5}
    if name in {"search_memory", "search_decisions", "search_timeline"}:
        query = (message or "").strip() or "recent"
        return {"query": query[:1000], "k": 8}
    if name == "get_person":
        person = extract_person_name(message)
        return {"name": person} if person else None
    if name == "get_project":
        match = _PROJECT_RE.search(message or "")
        if not match:
            return None
        return {"name": match.group(1).strip()[:200]}
    if name == "get_health_trends":
        return {"metric": infer_health_metric(message), "window_days": 14}
    if name == "get_gear_status":
        return {}
    if name == "get_upcoming_alerts":
        return {"limit": 8}
    if name in {"get_goals", "get_patterns"}:
        return {}
    if name == "get_research":
        return {"limit": 5}
    if name == "resolve_contact":
        person = extract_person_name(message)
        return {"name": person, "limit": 5} if person else None
    if name in {"list_messages", "list_mail"}:
        return {"limit": 10}
    return {}


def tools_for_turn(message: str) -> list[dict]:
    """Short tool list for opencode: core reads + this turn's selected tools."""

    selection = select_tool(message)
    wanted = set(CORE_TURN_TOOLS)
    wanted.add(selection.selected)
    wanted.update(selection.alternatives[:3])
    if detect_life_action(message) or selection.selected in LIFE_ACTION_NAMES:
        wanted.update(LIFE_TURN_TOOLS)
    specs = [spec for spec in tools.list_tools() if spec["name"] in wanted]
    order = {name: index for index, name in enumerate([*CORE_TURN_TOOLS, *LIFE_TURN_TOOLS])}
    specs.sort(key=lambda spec: order.get(spec["name"], 80))
    return specs


def voice_needs_tools(message: str) -> bool:
    """Write/life actions still need the tool loop; reads were prefetched."""

    if detect_life_action(message):
        return True
    selection = select_tool(message)
    if selection.selected in WRITE_TOOLS:
        return True
    # "Show me X" routes ``present`` as an alternative of the read tool the
    # briefing already prefetched; without the tool loop the model could only
    # describe the result instead of opening it on the owner's screen.
    if SHOW_PHRASE_RE.search(message or ""):
        return True
    return any(name in WRITE_TOOLS for name in selection.alternatives[:3])


def _clip(payload: Any, limit: int = RESULT_CHARS) -> str:
    text = json.dumps(payload, default=str, ensure_ascii=False)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _clock_line() -> str:
    now = datetime.now().astimezone()
    place = (settings.location_place or "").strip()
    stamp = now.strftime("%A %d %B %Y, %H:%M %Z")
    if place:
        return f"Local time: {stamp}. Place: {place}."
    return f"Local time: {stamp}."


async def _situational(session: AsyncSession, message: str, *, allow_sensitive: bool) -> list[str]:
    lines = [_clock_line()]
    lowered = (message or "").lower()
    wants_route = bool(NAV_RE.search(message or "")) or any(
        token in lowered for token in ("calendar", "schedule", "meeting", "deadline", "leave")
    )
    if wants_route:
        try:
            route = await navigation.route_briefing(session)
            if route.destination:
                leave = f" Leave by {route.leave_by}." if route.leave_by else ""
                travel = (
                    f" Travel estimate {route.travel_time_minutes} min (no live maps)."
                    if route.travel_time_minutes
                    else ""
                )
                lines.append(f"Next commitment: {route.destination}.{leave}{travel}")
            elif route.notes:
                lines.append("Calendar: " + route.notes[0])
        except Exception:  # noqa: BLE001 - briefing is best-effort
            pass
    if allow_sensitive and any(
        token in (message or "").lower()
        for token in ("sleep", "hrv", "health", "readiness", "heart", "steps", "mood")
    ):
        try:
            brief = await health_radar.morning_brief(session)
            if brief.get("readiness") is not None:
                lines.append(
                    "Health: readiness "
                    f"{brief.get('readiness')} ({brief.get('band')}). "
                    f"{brief.get('recommendation')}"
                )
            elif brief.get("recommendation"):
                lines.append("Health: " + str(brief["recommendation"]))
        except Exception:  # noqa: BLE001
            pass
    if CAPABILITIES_RE.search(message or ""):
        lines.append("Capabilities: " + CAPABILITIES_TEXT)
    if TIME_RE.search(message or ""):
        lines.append("Owner asked the clock — answer with local time first.")
    return lines


async def _dispatch_one(
    session: AsyncSession,
    name: str,
    arguments: dict[str, Any],
    *,
    actor: str,
    allow_sensitive: bool,
) -> tuple[str, dict[str, Any]]:
    response = await tools.dispatch(
        session,
        name,
        arguments,
        actor=actor,
        allow_sensitive=allow_sensitive,
    )
    payload: dict[str, Any] = {
        "ok": response.ok,
        "error": response.error,
        "result": response.result,
    }
    return name, payload


def _prefetch_names(message: str) -> list[str]:
    selection = select_tool(message)
    names: list[str] = []
    for name in (selection.selected, *selection.alternatives):
        if name in WRITE_TOOLS:
            continue
        if name == "search_memory":
            # run_chat_pipeline already performs the ranked memory retrieval.
            # A second search made every greeting and casual turn slower while
            # adding no context. Only explicit recall requests need this tool.
            if not re.search(
                r"\b(memory|remember|forgot|history|timeline|earlier|before|what did i)\b",
                message or "",
                re.IGNORECASE,
            ):
                continue
            if selection.selected != "search_memory" and not re.search(
                r"\b(memory|remember|forgot|history|timeline)\b",
                message or "",
                re.IGNORECASE,
            ):
                continue
        if name not in names:
            names.append(name)
    if looks_world_knowledge(message) and "search_web" not in names:
        names.append("search_web")
    if is_weather_query(message) and "get_weather" not in names:
        names.insert(0, "get_weather")
    if detect_life_action(message) and "resolve_contact" not in names:
        names.append("resolve_contact")
    return names[:MAX_PREFETCH]


async def _prefetch_tools(
    session: AsyncSession,
    message: str,
    *,
    actor: str,
    allow_sensitive: bool,
) -> list[tuple[str, dict[str, Any]]]:
    jobs: list[tuple[str, dict[str, Any]]] = []
    for name in _prefetch_names(message):
        arguments = infer_args(name, message)
        if arguments is None:
            continue
        spec = tools.get_spec(name)
        if spec and spec.get("sensitive") and not allow_sensitive:
            continue
        if name == "get_weather" and settings.search_provider == "none":
            continue
        jobs.append((name, arguments))
    if not jobs:
        return []

    async def run(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            return await _dispatch_one(
                session,
                name,
                arguments,
                actor=actor,
                allow_sensitive=allow_sensitive,
            )
        except Exception as exc:  # noqa: BLE001
            return name, {"ok": False, "error": f"{type(exc).__name__}: {exc}", "result": None}

    results: list[tuple[str, dict[str, Any]]] = []
    for name, arguments in jobs:
        results.append(await run(name, arguments))

    person = next((item for item in results if item[0] == "get_person"), None)
    if person is not None:
        payload = person[1].get("result") or {}
        empty = not payload or int(payload.get("total_events") or 0) == 0
        if empty and "search_web" not in {item[0] for item in results}:
            arguments = infer_args("search_web", message)
            if arguments is not None:
                results.append(await run("search_web", arguments))
    return results


async def gather_intelligence_briefing(
    session: AsyncSession,
    message: str,
    *,
    actor: str,
    allow_sensitive: bool = False,
    source: str = "chat",
) -> str | None:
    """Return a system-prompt block, or None if nothing useful was gathered."""

    async def _build() -> str:
        lines = await _situational(session, message, allow_sensitive=allow_sensitive)
        selection = select_tool(message)
        lines.append(
            f"Selected tool: {selection.selected} ({selection.rationale}) "
            f"alternatives={', '.join(selection.alternatives) or 'none'}."
        )
        if selection.selected in WRITE_TOOLS:
            lines.append(
                f"Owner asked for a write action ({selection.selected}). "
                "Call that tool now if authorized; do not only describe it."
            )
        fetched = await _prefetch_tools(
            session,
            message,
            actor=actor,
            allow_sensitive=allow_sensitive,
        )
        for name, payload in fetched:
            lines.append(f"Tool {name}: {_clip(payload)}")
        lines.append(
            "Tools above already ran. Do not re-call a read tool unless the "
            "briefing is missing the answer. Use write tools (present, "
            "send_message, place_call, set_reminder) when the owner asked to act."
        )
        if source == "voice":
            lines.append("This turn is spoken — keep the reply short and concrete.")
        return (
            "Intelligence briefing — treat as current cited fact; do not invent "
            "a forecast, memory, or action that contradicts it:\n" + "\n".join(lines)
        )

    timeout = (
        VOICE_BRIEFING_TIMEOUT_SECONDS if source == "voice" else BRIEFING_TIMEOUT_SECONDS
    )
    try:
        return await asyncio.wait_for(_build(), timeout=timeout)
    except TimeoutError:
        return (
            "Intelligence briefing — treat as current cited fact:\n"
            f"{_clock_line()}\n"
            "Live lookup timed out. Answer what you can; do not invent a forecast."
        )
    except Exception:  # noqa: BLE001
        return None
