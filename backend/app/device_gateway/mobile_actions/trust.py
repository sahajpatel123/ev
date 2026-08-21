"""EvieTrustEngine: risk classes, one-confirmation law, voice yes/no, freeze.

Server is authority. UI preference cannot promote T4 → allowed.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

RiskClass = Literal["T0", "T1", "T2", "T3", "T4"]
ConfirmPolicy = Literal["none", "voice", "system_ui", "block"]
UtteranceKind = Literal["yes", "no", "mutate", "query", "unrelated"]

YES_PHRASES = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yup",
        "do it",
        "send it",
        "go ahead",
        "sure",
        "correct",
        "ok",
        "okay",
        "confirm",
        "confirmed",
        "please do",
        "do that",
        "send",
        "call",
    }
)
NO_PHRASES = frozenset(
    {
        "no",
        "nope",
        "don't",
        "dont",
        "do not",
        "cancel",
        "never mind",
        "nevermind",
        "stop",
        "nah",
        "no thanks",
        "don't send",
        "dont send",
        "don't call",
        "dont call",
    }
)

_QUERY_WHO = re.compile(r"\b(who|whom|which (?:one|person)|who(?:'re| are) you sending)\b", re.I)
_QUERY_WHAT = re.compile(
    r"\b(what (?:exactly )?(?:are you|will you) (?:sending|saying)|what(?:'s| is) the message|read it back)\b",
    re.I,
)
_MUTATE_DURATION = re.compile(
    r"\b(?:change|make|actually|instead|say)\b.{0,40}?\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|forty-five|sixty)"
    r"\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)?\b",
    re.I,
)
_DRAFT_HOLD = re.compile(
    r"\b(?:don'?t send(?: it)? yet|not yet|save (?:it )?as a draft|prepare(?: it)? (?:only|first)|don'?t call)\b",
    re.I,
)
_NEGATION = re.compile(
    r"\b(?:don'?t|do not|never|stop)\b.{0,48}\b(?:call|message|text|send|facetime|open|set|remind)\b",
    re.I,
)
_NEGATION_LEAD = re.compile(
    r"\b(?:don'?t|do not|never)\s+(?:call|message|text|send|facetime)\b",
    re.I,
)


def risk_for_operation(operation: str, *, can_send_directly: bool = False) -> tuple[RiskClass, ConfirmPolicy]:
    op = (operation or "").strip()
    if op in {"current_location", "contacts_search", "capability_status"}:
        return "T0", "none"
    if op in {
        "create_timer",
        "create_alarm",
        "create_reminder",
        "open_app",
        "start_directions",
        "open_maps",
        "copy_to_clipboard",
        "schedule_notification",
        "haptic",
        "self_test",
    }:
        return "T1", "none"
    if op in {"share_content"}:
        return "T1", "system_ui"
    if op == "create_calendar_event":
        return "T2", "none"
    if op in {"call_contact", "facetime_contact"}:
        return "T3", "system_ui"
    if op == "message_contact":
        if can_send_directly:
            return "T3", "voice"
        return "T3", "system_ui"
    if op in {"direct_send", "direct_message"}:
        return "T3", "voice"
    if op in {"set_focus", "media_play_pause"}:
        return "T2", "voice"
    return "T4", "block"


def confirmation_ttl_s(risk: RiskClass) -> float:
    if risk in {"T3"}:
        return 45.0
    if risk == "T2":
        return 90.0
    return 90.0


def freeze_hash(normalized: dict[str, Any] | None) -> str:
    args = normalized if isinstance(normalized, dict) else {}
    keys = (
        "contact_query",
        "contact_ref",
        "message",
        "phone_number",
        "duration_seconds",
        "title",
        "when_iso",
        "destination",
        "app_id",
        "text",
    )
    blob = json.dumps({key: args.get(key) for key in keys}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def is_negated(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    return bool(_NEGATION_LEAD.search(raw) or _NEGATION.search(raw) or _DRAFT_HOLD.search(raw))


def wants_draft(text: str) -> bool:
    return bool(_DRAFT_HOLD.search(text or ""))


def _normalize_utterance(text: str) -> str:
    raw = (text or "").strip().lower()
    raw = re.sub(r"[.!?]+$", "", raw).strip()
    raw = re.sub(r"^(?:ok[, ]+|okay[, ]+|please[, ]+)+", "", raw).strip()
    return raw


def classify_utterance(text: str) -> UtteranceKind:
    raw = _normalize_utterance(text)
    if not raw:
        return "unrelated"
    if _QUERY_WHO.search(raw):
        return "query"
    if _QUERY_WHAT.search(raw):
        return "query"
    if _MUTATE_DURATION.search(raw) and any(
        token in raw for token in ("change", "make it", "actually", "instead", "say ")
    ):
        return "mutate"
    if raw in NO_PHRASES or raw.startswith("don't") or raw.startswith("dont ") or raw.startswith("cancel"):
        return "no"
    if raw in YES_PHRASES or raw in {"okay send it", "ok send it", "yes send it", "yeah send it"}:
        return "yes"
    if raw.startswith("yes ") or raw.startswith("yeah ") or raw.endswith(" go ahead"):
        if any(token in raw for token in ("not", "don't", "dont", "cancel")):
            return "no"
        return "yes"
    return "unrelated"


def mutate_duration_seconds(text: str) -> int | None:
    match = _MUTATE_DURATION.search(text or "")
    if not match:
        return None
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
        "twenty": 20,
        "thirty": 30,
        "forty": 40,
        "forty-five": 45,
        "sixty": 60,
    }
    amount = match.group(1).lower().replace(" ", "-")
    n = int(amount) if amount.isdigit() else words.get(amount)
    if not n:
        return None
    unit = (match.group(2) or "minutes").lower()
    if unit.startswith("sec"):
        return max(1, n)
    if unit.startswith("hour") or unit.startswith("hr"):
        return max(1, n * 3600)
    return max(1, n * 60)


def pending_query_spoken(text: str, normalized: dict[str, Any] | None) -> str | None:
    args = normalized if isinstance(normalized, dict) else {}
    raw = text or ""
    if _QUERY_WHO.search(raw):
        who = args.get("contact_query") or args.get("display_name") or "the person I already named"
        return str(who)
    if _QUERY_WHAT.search(raw):
        body = args.get("message") or args.get("text")
        if body:
            return str(body)
        return "I haven't drafted a message body yet."
    return None
