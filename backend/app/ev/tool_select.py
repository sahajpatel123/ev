"""Tool selection intelligence: rule-based routing of simple intents."""

from __future__ import annotations

import re

from app.schemas import ToolSelectionResponse

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


def select_tool(message: str) -> ToolSelectionResponse:
    lowered = message.lower()
    scores: list[tuple[str, int, str]] = []

    def add(name: str, weight: int, why: str) -> None:
        scores.append((name, weight, why))

    if ARITHMETIC_RE.search(message) or PERCENT_OF_RE.search(message) or CALC_PHRASE_RE.search(message):
        add("calculate", 4, "The message contains an arithmetic expression.")
    if any(pattern.search(message) for pattern in PERSON_PATTERNS) or re.search(
        r"\bwhere(?:'s| is) [A-Z][a-z]+\b", message
    ):
        add("get_person", 3, "The message names or asks about a person.")
    if any(t in lowered for t in ("project", "print", "bom", "wrist", "maker")):
        add("get_project", 3, "The message mentions a maker project, BOM, or print job.")
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
