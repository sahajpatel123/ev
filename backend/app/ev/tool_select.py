"""Tool selection intelligence: rule-based routing of simple intents."""

from __future__ import annotations

import re

from app.schemas import ToolSelectionResponse
from app.search.live import is_weather_query, looks_world_knowledge

ARITHMETIC_RE = re.compile(r"\d+\s*[\+\-\*/%^]\s*\d")
PERCENT_OF_RE = re.compile(r"\b\d[\d,]*\s*%\s+of\b")
CALC_PHRASE_RE = re.compile(
    r"\b(?:calculate|compute|math)\b|"
    r"\bwhat(?:'s| is)\b.*\b(?:plus|minus|times|multiplied by|divided by)\b",
    re.IGNORECASE,
)
PERSON_PATTERNS = [
    re.compile(r"\b(?:my|our)\s+(?:friend|colleague|boss|manager|mom|dad|mother|father|brother|sister|wife|husband|partner|girlfriend|boyfriend|roommate|neighbor)\s+[A-Z]", re.IGNORECASE),
    re.compile(r"\bwhere(?:'s| is)\b.*\b(friend|colleague|boss|partner|mom|dad)\b", re.IGNORECASE),
    re.compile(r"\bwho(?:'s| is)\s+[A-Z][a-z]+\b", re.IGNORECASE),
]
TEXT_PHRASE_RE = re.compile(
    r"\b(?:text|message|imessage|whatsapp|ping)\b|"
    r"\bsend(?: a)? (?:text|message|note|sms)\b|"
    r"\bsend \w+ a (?:text|message|note|sms)\b",
    re.IGNORECASE,
)
CALL_PHRASE_RE = re.compile(
    r"\b(?:call|phone|facetime|ring)\b",
    re.IGNORECASE,
)
MAIL_PHRASE_RE = re.compile(r"\b(?:mail|email|inbox)\b", re.IGNORECASE)
OPEN_URL_RE = re.compile(
    r"\bopen\b.*\b(?:url|link|website|page|http)\b",
    re.IGNORECASE,
)
REMINDER_RE = re.compile(
    r"\b(?:remind(?:er)?|set a reminder|nag me|don'?t (?:let me )?forget)\b",
    re.IGNORECASE,
)
SHOW_PHRASE_RE = re.compile(
    r"\b(?:"
    r"show (?:me|us|that|this|it)\b|"
    r"show .{0,48} on (?:(?:my |the )?screen|the overlay|the visor)\b|"
    r"put (?:that|it|this) (?:up\b|on (?:screen|the screen))|"
    r"pull up|bring up|"
    r"open a (?:window|card|lookout)|"
    r"on (?:my )?(?:screen|overlay|visor)|"
    r"\bhud\b|lookout|keep an eye|full hud|command center|suit hud"
    r")",
    re.IGNORECASE,
)
MESSAGES_LIST_RE = re.compile(
    r"\b(?:messages|texts|who texted|new messages)\b",
    re.IGNORECASE,
)
SEARCH_WEB_RE = re.compile(
    r"\b(?:search the web|look (?:this|it )?up|google |wikipedia|"
    r"headline|stock price|who won|capital of|population of|"
    r"latest news|current events|define )\b",
    re.IGNORECASE,
)
NAV_RE = re.compile(
    r"\b(?:leave by|when (?:should|do) i (?:leave|go)|directions|"
    r"route to|how (?:long|far) to get|next (?:meeting|appointment|event)|"
    r"on my (?:calendar|schedule)|what(?:'s| is) on my calendar)\b",
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r"\b(?:what(?:'s| is) the (?:time|date)|what time is it|what day is it|"
    r"today'?s date|current time)\b",
    re.IGNORECASE,
)
CAPABILITIES_RE = re.compile(
    r"\b(?:what can you do|who are you|what are you|"
    r"your capabilities|what do you (?:do|know)|introduce yourself)\b",
    re.IGNORECASE,
)


def select_tool(message: str) -> ToolSelectionResponse:
    lowered = message.lower()
    scores: list[tuple[str, int, str]] = []

    def add(name: str, weight: int, why: str) -> None:
        scores.append((name, weight, why))

    if ARITHMETIC_RE.search(message) or PERCENT_OF_RE.search(message) or CALC_PHRASE_RE.search(message):
        add("calculate", 4, "The message contains an arithmetic expression.")
    if TEXT_PHRASE_RE.search(message):
        add("send_message", 6, "The message asks to send a text/message.")
        add("resolve_contact", 4, "Life sends should resolve the recipient first.")
    if CALL_PHRASE_RE.search(message) and not re.search(
        r"\bremind(?:er)?\b.{0,48}\bcall\b", lowered
    ):
        add("place_call", 6, "The message asks to place a call.")
        add("resolve_contact", 4, "Life calls should resolve the recipient first.")
    if MAIL_PHRASE_RE.search(message):
        add("list_mail", 5, "The message asks about mail/email.")
    if OPEN_URL_RE.search(message):
        add("open_url", 5, "The message asks to open a URL.")
    if REMINDER_RE.search(message):
        add("set_reminder", 5, "The message asks to set a reminder.")
    if MESSAGES_LIST_RE.search(message):
        add("list_messages", 4, "The message asks about recent messages.")
    if SHOW_PHRASE_RE.search(message):
        add("present", 5, "The message asks EVIE to show something on screen.")
    if any(pattern.search(message) for pattern in PERSON_PATTERNS) or re.search(
        r"\bwhere(?:'s| is) [A-Z][a-z]+\b", message
    ):
        add("get_person", 3, "The message names or asks about a person.")
    if any(t in lowered for t in ("project", "print", "bom", "wrist", "maker")):
        # "show me the <project>" means fetch the project, not just echo the
        # words on screen — a project explicitly asked for beats generic present.
        project_weight = 6 if SHOW_PHRASE_RE.search(message) else 3
        add("get_project", project_weight, "The message mentions a maker project, BOM, or print job.")
    if "goal" in lowered:
        add("get_goals", 2, "The message asks about goals.")
    if any(t in lowered for t in ("sleep", "hrv", "readiness", "steps", "health")):
        add("get_health_trends", 3, "The message asks about health metrics.")
    if any(t in lowered for t in ("battery", "gear", "device")):
        add("get_gear_status", 2, "The message asks about device/gear telemetry.")
    if any(t in lowered for t in ("alert", "deadline", "remind", "reminder", "calendar", "schedule")):
        add("get_upcoming_alerts", 3, "The message asks about alerts or deadlines.")
    if "research" in lowered:
        add("get_research", 2, "The message asks about research sessions.")
    if any(t in lowered for t in ("pattern", "habit")):
        add("get_patterns", 2, "The message asks about behavior patterns.")
    if any(t in lowered for t in ("when did", "history", "timeline", "what happened")):
        add("search_timeline", 2, "The message asks about past events.")
    if any(t in lowered for t in ("decision", "decided", "what did i decide")):
        add("search_decisions", 3, "The message asks about past decisions.")
    if is_weather_query(message):
        add("get_weather", 6, "The message asks for live weather or a forecast.")
    if SEARCH_WEB_RE.search(message) or looks_world_knowledge(message):
        add("search_web", 5, "The message asks for a public/web fact.")
    if NAV_RE.search(message):
        add("get_upcoming_alerts", 5, "The message asks for calendar, leave-by, or the next commitment.")
    if TIME_RE.search(message):
        add("get_upcoming_alerts", 2, "Clock/date questions still benefit from today's commitments.")
    if CAPABILITIES_RE.search(message) or "protocol" in lowered:
        add("list_protocols", 6, "The owner asked what protocols they have.")
    if any(
        phrase in lowered
        for phrase in ("call yourself", "your name is", "reset your name", "go back to evie")
    ):
        add("set_assistant_name", 6, "The owner is setting the spoken nickname.")
    if any(phrase in lowered for phrase in ("be funnier", "more formal", "less formal", "more concise")):
        add("update_personality", 5, "The owner is changing personality sliders.")
    if "quiet until" in lowered or "go quiet" in lowered:
        add("set_quiet_hours", 6, "The owner is setting quiet hours.")
    if "what just happened" in lowered:
        add("list_callouts", 6, "The owner asked what just happened.")
    add("search_memory", 1, "Default: personal memory lookup.")

    best = max(scores, key=lambda item: item[1])
    alternatives = [
        name
        for name, weight, _why in sorted(scores, key=lambda item: item[1], reverse=True)
        if name != best[0]
    ][:3]
    return ToolSelectionResponse(
        message=message,
        selected=best[0],
        alternatives=alternatives,
        rationale=best[2],
    )
