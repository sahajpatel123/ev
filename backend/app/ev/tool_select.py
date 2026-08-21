"""Tool selection intelligence: rule-based routing of simple intents."""

from __future__ import annotations

import re

from app.ev.continuity import classify_memory_intent
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
    r"\bopen\b.*\b(?:url|link|website|page|http)\b|"
    r"\bopen\s+(?:https?://|www\.)",
    re.IGNORECASE,
)
OPEN_APP_RE = re.compile(
    r"\b(?:open|launch)\s+(?:up\s+)?(?:the\s+)?(?P<name>"
    r"safari|messages|mail|calendar|finder|notes|music|photos|maps|"
    r"facetime|reminders|settings|terminal|chrome|arc|slack|spotify|"
    r"textedit|text edit|calculator|calc|cursor|vscode|code|"
    r"google chrome|system settings|system preferences|imessage)"
    r"(?:\.app)?\b",
    re.IGNORECASE,
)
CLOSE_APP_RE = re.compile(
    r"\b(?:close|quit)\s+(?:up\s+)?(?:the\s+)?(?P<name>"
    r"safari|messages|mail|calendar|notes|music|photos|maps|"
    r"facetime|reminders|settings|terminal|chrome|arc|slack|spotify|"
    r"google chrome|system settings|system preferences|imessage)"
    r"(?:\.app)?\b",
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
CALENDAR_READ_RE = re.compile(
    r"\b(?:on my (?:calendar|schedule)|what(?:'s| is) on my calendar|"
    r"next (?:meeting|appointment|event)|anything on my calendar|"
    r"my calendar (?:today|tomorrow)|calendar today|calendar tomorrow)\b",
    re.IGNORECASE,
)
LEAVE_BY_RE = re.compile(
    r"\b(?:leave by|when (?:should|do) i (?:leave|go)|directions|"
    r"route to|how (?:long|far) to get)\b",
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
TIMER_RE = re.compile(
    r"\b(?:start |set )?(?:a )?timer (?:for )?(\d+)\s*(?:min|mins|minute|minutes)\b",
    re.IGNORECASE,
)
CALL_TARGET_RE = re.compile(
    r"\b(?:call|phone|ring|facetime)\s+(?:my\s+)?([A-Za-z][A-Za-z'-]+)",
    re.IGNORECASE,
)
REMIND_TARGET_RE = re.compile(
    r"\b(?:remind me to|set a reminder to|reminder to)\s+(.+)$",
    re.IGNORECASE,
)
TEXT_TARGET_RE = re.compile(
    r"\b(?:text|message|i'?m message)\s+([A-Za-z][A-Za-z'-]+)\s+(.+)$",
    re.IGNORECASE,
)

# S2S live models may call these. R4/shell, drone, print, and camera replay
# stay off the realtime catalog so the audio loop never owns a durable actuator.
# `look` is a one-shot observation, not an NVR replay.
LIVE_VOICE_TOOLS = frozenset(
    {
        "search_memory",
        "search_web",
        "get_weather",
        "set_reminder",
        "send_message",
        "place_call",
        "present",
        "calculate",
        "get_person",
        "resolve_contact",
        "calendar_read",
        "calendar_add",
        "start_timer",
        "list_messages",
        "list_protocols",
        "get_health_trends",
        "get_gear_status",
        "brief_me",
        "home_status",
        "calibrate",
        "home_act",
        "list_mail",
        "open_url",
        "open_app",
        "close_app",
        "activate_app",
        "list_apps",
        "computer_status",
        "inspect_ui",
        "ui_action",
        "screen_look",
        "app_action",
        "look",
        "observe_camera",
        "phone_action",
    }
)

# Realtime models often refuse Mac open/close in speech even when the
# function is advertised. The live session executes these from the owner
# transcript through dispatch; it does not invent success.
DETERMINISTIC_LIVE_ACTIONS = frozenset({"open_app", "close_app", "open_url"})

LOOK_RE = re.compile(
    r"\b(?:"
    r"what do you see|"
    r"what(?:'s| is) (?:that|this|on (?:the |my )?camera|in (?:the )?frame|in front(?: of (?:you|the camera))?|on my desk)|"
    r"what am i holding|"
    r"what(?:'s| is) this (?:one|now)|"
    r"what color is this|"
    r"look at (?:this|that|the camera|me|my desk|the (?:label|sign|screen|photo|picture))|"
    r"take a (?:quick )?look|"
    r"read (?:this|that|the (?:text|label|sign|screen|menu))|"
    r"can you (?:see|read) (?:this|that|me)|"
    r"does this look|"
    r"which (?:port|cable|one)|"
    r"is this (?:plugged|on|connected|damaged)|"
    r"who(?:'s| is) (?:that|this)(?: person)?|"
    r"identify (?:this|that|what(?:'s| is) this)|"
    r"use (?:the |your )?camera"
    r")\b",
    re.IGNORECASE,
)
OBSERVE_RE = re.compile(
    r"\b(?:"
    r"watch (?:this|that|what i(?:'| a)?m doing)|"
    r"look while|"
    r"tell me when|"
    r"for a few seconds|"
    r"which direction am i turning"
    r")\b",
    re.IGNORECASE,
)


def _look_intent(message: str, lowered: str) -> bool:
    if "look up" in lowered or "look this up" in lowered or "look it up" in lowered:
        return False
    if any(p in lowered for p in ("how do i look", "how do i feel", "you look wrecked")):
        return False
    return bool(LOOK_RE.search(message)) or (
        "camera" in lowered and any(p in lowered for p in ("look", "see", "read", "what's on"))
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
        add("open_url", 6, "The message asks to open a URL.")
    open_app = OPEN_APP_RE.search(message)
    if open_app:
        add("open_app", 7, "The message asks to open an app.")
    close_app = CLOSE_APP_RE.search(message)
    if close_app:
        add("close_app", 7, "The message asks to close an app.")
    if REMINDER_RE.search(message):
        add("set_reminder", 5, "The message asks to set a reminder.")
    if TIMER_RE.search(message):
        add("start_timer", 7, "The message asks to start a timer.")
    if MESSAGES_LIST_RE.search(message):
        add("list_messages", 4, "The message asks about recent messages.")
    if SHOW_PHRASE_RE.search(message):
        add("present", 5, "The message asks EVIE to show something on screen.")
    person_request = any(pattern.search(message) for pattern in PERSON_PATTERNS) or re.search(
        r"\bwhere(?:'s| is) [A-Z][a-z]+\b", message
    )
    if person_request:
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
    if any(p in lowered for p in ("calibrat", "check the calibration", "run diagnostics", "checkup")):
        add("calibrate", 7, "The owner asked to check calibration.")
    if lowered.startswith("research ") or "cite sources" in lowered or "cite your sources" in lowered:
        add("research", 7, "The owner asked for cited research.")
    if any(p in lowered for p in ("print the", "start the print", "start printing")):
        add("print_start", 7, "The owner asked to start a print.")
    if "how long will this take" in lowered or "estimate print" in lowered:
        add("estimate_print", 6, "The owner asked for a print estimate.")
    if any(p in lowered for p in ("phone battery", "watch battery", "gear power", "what's the battery", "hows the battery", "how's the battery")):
        add("gear_power", 7, "The owner asked about device or telemetry battery.")
    if any(p in lowered for p in ("how do i look", "how do i feel", "you look wrecked", "readiness")):
        add("health_how_do_i_look", 7, "The owner asked how they look.")
    if any(p in lowered for p in ("hit my head", "i hit my head", "concussion", "head injury")):
        add("head_injury_screen", 8, "The owner reported a head injury.")
    if lowered.strip() in {"brief me", "brief us"} or lowered.startswith("brief me"):
        add("brief_me", 7, "The owner asked for a tactical brief.")
    if (
        not person_request
        and ("where is " in lowered or lowered.startswith("where's ") or "whereabouts" in lowered)
    ):
        add("where_is", 6, "The owner asked where someone is.")
    if "camera" in lowered and any(p in lowered for p in ("show", "replay", "from ")):
        add("camera_replay", 6, "The owner asked to replay an owner camera.")
    if OBSERVE_RE.search(message):
        add("observe_camera", 9, "The owner asked to watch a visual change over time.")
    if _look_intent(message, lowered):
        add("look", 8, "The owner asked the assistant to look at the camera or a photo.")
    if any(p in lowered for p in ("subscribe", "watchlist", "watch for")):
        add("watchlist_add", 6, "The owner asked to add a watchlist item.")
    if "digest" in lowered and "alert" in lowered:
        add("alerts_digest", 6, "The owner asked for an alerts digest.")
    if any(p in lowered for p in ("likely fake", "is this real", "deepfake", "media check")):
        add("media_check", 7, "The owner asked about media authenticity.")
    if any(p in lowered for p in ("lower voice", "slower voice", "use the lower", "change your voice")):
        add("set_voice", 7, "The owner asked to change TTS voice.")
    if any(p in lowered for p in ("what's public", "public record", "sec filing")):
        add("public_lookup", 6, "The owner asked for public records.")
    if any(p in lowered for p in ("where's my", "find my ", "backpack tag", "airtag")):
        add("find_gear", 6, "The owner asked to find their gear.")
    if "why did you ping" in lowered or "why'd you ping" in lowered:
        add("why_did_you_ping", 8, "The owner asked why they were pinged.")
    if any(p in lowered for p in ("on my plate", "what's on my plate", "whats on my plate")):
        add("whats_on_my_plate", 7, "The owner asked for the daily plate.")
    if "draft" in lowered and any(p in lowered for p in ("reply", "email", "mail")):
        add("draft_reply", 6, "The owner asked to draft a reply.")
    if any(p in lowered for p in ("take off", "takeoff", "hover", "land the drone", "return to launch")):
        add("drone", 7, "The owner issued a leashed drone command.")
    if is_weather_query(message):
        add("get_weather", 6, "The message asks for live weather or a forecast.")
    if SEARCH_WEB_RE.search(message) or looks_world_knowledge(message):
        add("search_web", 5, "The message asks for a public/web fact.")
    if CALENDAR_READ_RE.search(message):
        add("calendar_read", 7, "The message asks to read the owner's calendar.")
    if LEAVE_BY_RE.search(message):
        add("get_upcoming_alerts", 6, "The message asks for leave-by or route timing.")
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
    if classify_memory_intent(message) == "explicit_recall":
        add("search_memory", 9, "The owner asked Evie to recall prior conversations or decisions.")
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


def resolve_live_action(message: str) -> tuple[str, dict] | None:
    """High-precision transcript → POL tool for the pipeline live path.

    Returns None for ordinary chat. Never selects execute_command, drone,
    print_start, or camera_replay. S2S models with tools must not call this
    on the same turn — they already own function calling.
    """

    text = (message or "").strip()
    if not text or len(text) > 240:
        return None
    timer = TIMER_RE.search(text)
    if timer:
        return "start_timer", {"minutes": int(timer.group(1))}
    weather = is_weather_query(text)
    if weather:
        return "get_weather", {}
    call = CALL_TARGET_RE.search(text)
    if call and not re.search(r"\bremind(?:er)?\b.{0,48}\bcall\b", text, re.IGNORECASE):
        return "place_call", {"name": call.group(1)}
    remind = REMIND_TARGET_RE.search(text)
    if remind:
        return "set_reminder", {"text": remind.group(1).strip()[:200]}
    send = TEXT_TARGET_RE.search(text)
    if send:
        return "send_message", {"to": send.group(1), "text": send.group(2).strip()[:500]}
    open_app = OPEN_APP_RE.search(text)
    if open_app:
        return "open_app", {"name": open_app.group("name")}
    close_app = CLOSE_APP_RE.search(text)
    if close_app:
        return "close_app", {"name": close_app.group("name")}
    url_open = OPEN_URL_RE.search(text)
    if url_open:
        found = re.search(r"(https?://\S+|www\.\S+)", text, re.IGNORECASE)
        url = found.group(1) if found else ""
        if url.lower().startswith("www."):
            url = "https://" + url
        if url:
            return "open_url", {"url": url}
    selection = select_tool(text)
    name = selection.selected
    if name not in LIVE_VOICE_TOOLS or name == "search_memory":
        return None
    if name in {
        "calendar_read",
        "calibrate",
        "list_messages",
        "home_status",
        "calculate",
        "list_mail",
        "look",
        "observe_camera",
    }:
        if name == "calculate":
            return name, {"expression": text}
        if name == "look":
            return name, {"prompt": text[:400], "focus": "auto"}
        if name == "observe_camera":
            return name, {"objective": text[:400], "duration_seconds": 4}
        return name, {}
    return None
