"""Create, authorize, resolve, and complete mobile actions.

The Shortcut must execute only the server-normalized `run` object returned
by resolve. Client-supplied operation arguments in the shortcuts:// payload
are ignored.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from . import ACTION_TTL_S, BRIDGE_PROTOCOL, BRIDGE_VERSION, store
from . import registry as reg
from .strategy import route

_DURATION_WORDS = {
    "a": 1,
    "an": 1,
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
    "forty five": 45,
    "sixty": 60,
}
_DURATION_RE = re.compile(
    r"\b(?:for|in)?\s*(?:a |an )?(\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty(?:[ -]five)?|sixty)\s*"
    r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 .\-()]{6,20}$")
_DIGIT_RE = re.compile(r"\d+")
SAFE_COMPLETE_KEYS = frozenset(
    {
        "status",
        "result",
        "verified",
        "failure",
        "display_name",
        "masked_destination",
        "choices",
        "requires_user_interaction",
        "permission",
        "system_ui_presented",
        "timer_kind",
    }
)
ALLOWED_RESULTS = frozenset(
    {
        "CREATED",
        "EXECUTED",
        "PREPARED",
        "SYSTEM_UI_OPENED",
        "SENT",
        "FAILED",
        "CANCELLED",
        "PERMISSION_REQUIRED",
        "PERMISSION_DENIED",
        "CONTACT_NOT_FOUND",
        "CONTACT_AMBIGUOUS",
        "ACTION_UNAVAILABLE",
        "USER_CANCELLED",
        "EXECUTION_FAILED",
        "VERIFICATION_UNAVAILABLE",
        "SELF_TEST_OK",
    }
)


def _tz(handshake: dict[str, Any]) -> ZoneInfo:
    name = str(handshake.get("timezone") or "").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def parse_duration_seconds(args: dict[str, Any], transcript: str = "") -> int | None:
    if args.get("duration_seconds"):
        try:
            value = int(args["duration_seconds"])
            return value if 1 <= value <= 86400 else None
        except (TypeError, ValueError):
            return None
    if args.get("duration_minutes"):
        try:
            value = int(float(args["duration_minutes"]) * 60)
            return value if 1 <= value <= 86400 else None
        except (TypeError, ValueError):
            return None
    blob = " ".join(
        str(part)
        for part in (args.get("duration"), args.get("text"), transcript)
        if part
    )
    match = _DURATION_RE.search(blob)
    if not match:
        return None
    raw, unit = match.group(1).lower(), match.group(2).lower()
    amount = int(raw) if raw.isdigit() else _DURATION_WORDS.get(raw)
    if amount is None:
        return None
    if unit.startswith("sec"):
        seconds = amount
    elif unit.startswith("hour") or unit.startswith("hr"):
        seconds = amount * 3600
    else:
        seconds = amount * 60
    return seconds if 1 <= seconds <= 86400 else None


def parse_when_iso(args: dict[str, Any], *, handshake: dict[str, Any], transcript: str = "") -> str | None:
    if args.get("when_iso"):
        return str(args["when_iso"])[:40]
    blob = " ".join(str(part) for part in (args.get("when"), args.get("date_time"), transcript) if part)
    if not blob.strip():
        return None
    now = datetime.now(_tz(handshake))
    parsed = None
    try:
        from app.ev.resolve import parse_owner_when

        parsed = parse_owner_when(blob, now=now)
    except Exception:
        pass
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_tz(handshake))
    return parsed.isoformat()


def _digits(value: str) -> str:
    return "".join(_DIGIT_RE.findall(value or ""))


def normalize_phone_number(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text or not _PHONE_RE.match(text):
        return None
    digits = _digits(text)
    if digits in reg.EMERGENCY_NUMBERS or len(digits) < 7:
        return None
    return text


def is_emergency(value: str) -> bool:
    digits = _digits(value)
    lowered = (value or "").strip().lower()
    if digits in reg.EMERGENCY_NUMBERS:
        return True
    return any(token in lowered for token in ("emergency", "poison control"))


def mask_destination(number: str) -> str:
    digits = _digits(number)
    if len(digits) < 4:
        return "••••"
    return "••••" + digits[-4:]


def maps_url(destination: str, *, directions: bool) -> str:
    query = quote(destination.strip()[:200])
    if directions:
        return f"https://maps.apple.com/?daddr={query}&dirflg=d"
    return f"https://maps.apple.com/?q={query}"


def tel_url(number: str) -> str:
    return "tel:" + quote(_digits(number), safe="+")


def sms_url(number: str, body: str) -> str:
    return "sms:" + quote(_digits(number), safe="+") + "&body=" + quote(body[:500])


def facetime_url(number: str) -> str:
    return "facetime:" + quote(_digits(number), safe="+")


def sanitize_complete(payload: dict[str, Any]) -> dict[str, Any]:
    """Phone contacts stay on the phone. Only minimal resolution fields return."""

    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in SAFE_COMPLETE_KEYS:
            continue
        if key == "result":
            text = str(value or "").upper()
            clean[key] = text if text in ALLOWED_RESULTS else "VERIFICATION_UNAVAILABLE"
        elif key == "choices":
            choices = []
            for item in value if isinstance(value, list) else []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("display_name") or "").strip()[:80]
                if name:
                    choices.append({"name": name})
                if len(choices) >= 4:
                    break
            clean[key] = choices
        elif key == "display_name":
            clean[key] = str(value or "").strip()[:80]
        elif key == "masked_destination":
            clean[key] = str(value or "").strip()[:16]
        elif key == "permission":
            clean[key] = str(value or "").strip()[:40]
        elif key == "failure":
            clean[key] = str(value or "").strip()[:64]
        elif key in {"verified", "requires_user_interaction"}:
            clean[key] = bool(value)
        elif key == "status":
            clean[key] = str(value or "").strip()[:32]
    return clean


def _spoken(row: dict[str, Any], *, pending: bool = False) -> str:
    operation = str(row.get("operation") or "")
    failure = str(row.get("failure") or "")
    receipt_raw = row.get("receipt")
    receipt: dict[str, Any] = receipt_raw if isinstance(receipt_raw, dict) else {}
    result = str(receipt.get("result") or row.get("result") or "")
    normalized_raw = row.get("normalized")
    args: dict[str, Any] = normalized_raw if isinstance(normalized_raw, dict) else {}
    if failure == "NATIVE_SHELL_REQUIRED":
        return "Open Evie as the iPhone app to do that on this phone. The Home Screen page can't run that native action."
    if failure == "NATIVE_ACTIONS_DISABLED":
        return "Native iPhone actions are turned off right now. Voice still works."
    if failure == "APP_UNSUPPORTED":
        return "I can't open that app directly yet."
    if failure == "BRIDGE_REQUIRED":
        return "Open Evie as the iPhone app to do that on this phone."
    if failure == "BRIDGE_UPDATE_REQUIRED":
        return "Mobile Actions needs an update on this iPhone."
    if failure == "HIGH_RISK":
        return "I won't do that from here."
    if failure == "REMOTE_PHONE_UNSUPPORTED":
        return "I can only do that on the iPhone you're using right now."
    if failure == "HOME_ADDRESS_UNAVAILABLE":
        return "Where should I take you? I don't look up a home address from memory for this."
    if failure == "EMERGENCY_BLOCKED":
        return "I won't place emergency calls automatically. Use the iPhone's Emergency SOS if you need that."
    if failure == "AMBIGUOUS":
        return str(row.get("clarify") or "Which one did you mean?")
    if failure == "PERMISSION_REQUIRED":
        perm = args.get("permission") or "that"
        return f"Your iPhone needs permission to let Evie use {perm} for this action."
    if failure == "CONTACT_AMBIGUOUS":
        names = row.get("choices") or []
        labels = [
            str(name)
            for item in (names if isinstance(names, list) else [])
            if isinstance(item, dict)
            for name in [item.get("name")]
            if name
        ]
        if labels:
            return "I found more than one match. Did you mean " + " or ".join(labels[:3]) + "?"
        return "I found more than one contact. Which one?"
    if failure == "CONTACT_NOT_FOUND":
        return f"I couldn't find {args.get('contact_query') or 'that contact'} on this iPhone."
    if failure == "EXPIRED":
        return "That action expired. Ask me again if you still want it."
    if failure == "CANCELLED":
        return "I won't do that."
    if result == "CANCELLED":
        return "Cancelled."
    if row.get("state") == "draft":
        return "I've prepared it and won't send yet."
    if row.get("state") == "awaiting_confirmation" or pending:
        if operation in {"message_contact", "direct_message"}:
            return (
                f"Send '{args.get('message')}' to {args.get('contact_query') or 'them'}?"
            )
        if operation == "create_calendar_event":
            return f"Add {args.get('title') or 'that event'} to your calendar?"
        return "Do you want me to do that on this iPhone?"
    if result == "SYSTEM_UI_OPENED" or (
        row.get("state") in {"authorized", "resolved", "executing"} and operation in {
            "call_contact",
            "facetime_contact",
            "start_directions",
            "open_maps",
        }
    ):
        if operation == "call_contact":
            who = args.get("contact_query") or "them"
            return f"I've opened the call for {who}."
        if operation == "facetime_contact":
            who = args.get("contact_query") or "them"
            return f"I've opened FaceTime for {who}."
        if operation in {"start_directions", "open_maps"}:
            dest = args.get("destination") or "that place"
            return f"I've opened Maps for {dest}."
    if row.get("state") == "executed" and result in {"CREATED", "EXECUTED", "SENT", "SELF_TEST_OK"}:
        if operation == "create_timer":
            mins = max(1, int((args.get("duration_seconds") or 60) / 60))
            label = "minute" if mins == 1 else "minutes"
            return f"Your {mins}-{label} timer is set." if mins * 60 == args.get("duration_seconds") else "Your timer is set."
        if operation == "create_reminder":
            return "Reminder saved."
        if operation == "create_alarm":
            return "Alarm set."
        if operation == "create_calendar_event":
            return "That's on your calendar."
        if operation == "message_contact" and result == "SENT":
            return f"Message sent to {args.get('contact_query') or 'them'}."
        if operation == "message_contact":
            return f"I've opened the message to {args.get('contact_query') or 'them'}."
        if operation == "set_focus":
            return "Focus updated."
        if operation == "copy_to_clipboard":
            return "Copied."
        if operation == "share_content":
            return "I've opened the share sheet."
        if operation == "self_test":
            return "Evie Mobile Bridge is ready."
        return "Done."
    if row.get("state") in {"authorized", "resolved", "executing"}:
        if row.get("method") == "native_broker":
            if operation == "create_timer":
                return "Setting that timer now."
            if operation == "create_reminder":
                return "Saving that reminder now."
            if operation == "create_alarm":
                return "Setting that alarm now."
            if operation == "create_calendar_event":
                return "Adding that to your calendar now."
            if operation == "message_contact":
                return f"I've prepared the message for {args.get('contact_query') or 'them'}."
            if operation == "call_contact":
                return f"I've opened the call for {args.get('contact_query') or 'them'}."
            if operation == "open_app":
                return f"Opening {args.get('display_name') or args.get('app_id') or 'that app'}."
            if operation == "current_location":
                return "Checking where you are."
            return "Running that on this iPhone."
        if operation == "create_timer":
            return "Tap Start timer on this iPhone."
        if operation == "create_reminder":
            return "Tap Save reminder on this iPhone."
        if operation == "message_contact":
            return "Tap Send on this iPhone."
        return "Tap the card on this iPhone to finish that."
    if failure:
        return "I couldn't complete that on this iPhone."
    return "I prepared that for this iPhone."


def _card(row: dict[str, Any], *, launch_url: str | None, open_url: str | None) -> dict[str, Any]:
    normalized_raw = row.get("normalized")
    args: dict[str, Any] = normalized_raw if isinstance(normalized_raw, dict) else {}
    operation = str(row.get("operation") or "")
    title = {
        "create_timer": "TIMER",
        "create_reminder": "REMINDER",
        "create_alarm": "ALARM",
        "call_contact": "CALL",
        "facetime_contact": "FACETIME",
        "message_contact": "MESSAGE",
        "start_directions": "DIRECTIONS",
        "open_maps": "MAPS",
        "create_calendar_event": "CALENDAR",
        "set_focus": "FOCUS",
        "share_content": "SHARE",
        "copy_to_clipboard": "COPY",
        "media_play_pause": "MEDIA",
        "self_test": "BRIDGE",
    }.get(operation, operation.replace("_", " ").upper())
    target = (
        args.get("contact_query")
        or args.get("destination")
        or args.get("title")
        or args.get("label")
        or ""
    )
    if operation == "create_timer" and args.get("duration_seconds"):
        seconds = int(args["duration_seconds"])
        if seconds % 3600 == 0:
            target = f"{seconds // 3600} hour" if seconds == 3600 else f"{seconds // 3600} hours"
        elif seconds % 60 == 0:
            mins = seconds // 60
            target = f"{mins} minute" if mins == 1 else f"{mins} minutes"
        else:
            target = f"{seconds} seconds"
    body = args.get("message") if operation == "message_contact" else None
    go = {
        "create_timer": "Start timer",
        "create_reminder": "Save reminder",
        "create_alarm": "Set alarm",
        "call_contact": "Call now",
        "facetime_contact": "FaceTime",
        "message_contact": "Send" if row.get("state") != "awaiting_confirmation" else "Send",
        "start_directions": "Open Maps",
        "open_maps": "Open Maps",
        "create_calendar_event": "Add event",
        "share_content": "Share",
        "copy_to_clipboard": "Copy",
        "self_test": "Run self-test",
        "set_focus": "Change Focus",
        "media_play_pause": "Control media",
    }.get(operation, "Run")
    if row.get("state") == "awaiting_confirmation":
        go = "Confirm"
    device_label = row.get("device_label") or "This iPhone"
    return {
        "kind": "phone_action",
        "title": title,
        "target": target,
        "body": body,
        "status": row.get("state"),
        "device_label": device_label,
        "action_id": row.get("action_id"),
        "go_label": go,
        "launch_url": launch_url,
        "open_url": open_url,
        "method": row.get("method"),
        "requires_tap": True,
        "pwa_kind": args.get("pwa_kind"),
        "share_text": args.get("share_text") or args.get("message") or args.get("text"),
        "copy_text": args.get("copy_text") or args.get("text"),
    }


def _launch_payload(row: dict[str, Any], *, origin: str) -> dict[str, Any]:
    from .bridge import build_run_url, callback_urls

    authorized_raw = row.get("authorized_run")
    run: dict[str, Any] = authorized_raw if isinstance(authorized_raw, dict) else {}
    launch_url = None
    if row.get("method") == "shortcuts_bridge":
        launch_url = build_run_url(
            action_id=str(row["action_id"]),
            token=str(row["action_token"]),
            origin=origin,
            callbacks=callback_urls(origin),
        )
    open_url = run.get("url") if row.get("method") in {"web_handoff", "app_url"} else run.get("url")
    if row.get("method") == "web_handoff":
        open_url = run.get("url")
        launch_url = None
    return {
        "launch_url": launch_url,
        "open_url": open_url,
        "run": {k: v for k, v in run.items() if k != "completion_token"},
        "card": _card(row, launch_url=launch_url, open_url=open_url),
    }


def _authorized_run(row: dict[str, Any], *, origin: str) -> dict[str, Any]:
    args = dict(row.get("normalized") or {})
    operation = str(row["operation"])
    kind_map = {
        "create_timer": "timer",
        "create_reminder": "reminder",
        "create_alarm": "alarm",
        "call_contact": "call",
        "facetime_contact": "facetime",
        "message_contact": "message",
        "start_directions": "open_url",
        "open_maps": "open_url",
        "create_calendar_event": "calendar",
        "set_focus": "focus",
        "share_content": "share",
        "copy_to_clipboard": "clipboard",
        "media_play_pause": "media",
        "self_test": "self_test",
    }
    run: dict[str, Any] = {
        "kind": kind_map.get(operation, "noop"),
        "operation": operation,
        "protocol": BRIDGE_PROTOCOL,
        "action_id": row["action_id"],
        "complete_url": f"{origin}/v1/device-gateway/mobile-actions/{row['action_id']}/complete",
        "claim_url": f"{origin}/v1/device-gateway/mobile-actions/{row['action_id']}/claim",
        "completion_token": row["completion_token"],
        "device_id": row["device_id"],
    }
    if operation == "create_timer":
        seconds = int(args["duration_seconds"])
        run["duration_seconds"] = seconds
        run["duration_minutes"] = max(1, (seconds + 59) // 60)
        run["label"] = args.get("title") or "Timer"
    elif operation == "create_reminder":
        run["title"] = args.get("title") or "Reminder"
        run["when_iso"] = args.get("when_iso")
        run["list"] = args.get("list")
    elif operation == "create_alarm":
        run["when_iso"] = args.get("when_iso")
        run["label"] = args.get("title") or "Alarm"
    elif operation in {"call_contact", "facetime_contact", "message_contact"}:
        run["contact_query"] = args.get("contact_query")
        if args.get("phone_number"):
            run["phone_number"] = args["phone_number"]
            run["url"] = (
                facetime_url(args["phone_number"])
                if operation == "facetime_contact"
                else sms_url(args["phone_number"], args.get("message") or "")
                if operation == "message_contact"
                else tel_url(args["phone_number"])
            )
            if args.get("phone_number"):
                run["kind"] = "open_url" if not args.get("contact_query") else run["kind"]
        if operation == "message_contact":
            run["message"] = args.get("message")
    elif operation in {"start_directions", "open_maps"}:
        run["url"] = args.get("url")
        run["destination"] = args.get("destination")
        run["kind"] = "open_url"
    elif operation == "create_calendar_event":
        run["title"] = args.get("title")
        run["when_iso"] = args.get("when_iso")
        run["end_iso"] = args.get("end_iso")
        run["location"] = args.get("location")
    elif operation == "set_focus":
        run["focus"] = args.get("focus")
    elif operation == "share_content":
        run["text"] = args.get("text") or args.get("share_text")
        run["kind"] = "share"
    elif operation == "copy_to_clipboard":
        run["text"] = args.get("text") or args.get("copy_text")
        run["kind"] = "clipboard"
    elif operation == "media_play_pause":
        run["media_action"] = args.get("media_action") or "pause"
    return run


def _fail(operation: str, failure: str, *, spoken: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "ok": False,
        "executed": False,
        "verified": False,
        "accepted": False,
        "operation": operation,
        "failure": failure,
        "spoken": spoken or _spoken({"operation": operation, "failure": failure, "normalized": extra or {}}),
        "requires_user_interaction": False,
    }
    if extra:
        payload.update(extra)
    return payload


async def _push_live(session_id: str | None, card: dict[str, Any]) -> None:
    if not session_id:
        return
    try:
        from app.voice.live.layer import live_for_session
    except Exception:
        return
    live = live_for_session(session_id)
    if live is None:
        return
    try:
        await live.push_hud(card, kind="phone_action")
    except Exception:
        return


def _normalize(
    operation: str,
    args: dict[str, Any],
    *,
    handshake: dict[str, Any],
    transcript: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    cap = reg.get_capability(operation)
    if cap is None:
        return None, _fail(operation, "ACTION_UNAVAILABLE")
    out: dict[str, Any] = {}
    if operation == "create_timer":
        seconds = parse_duration_seconds(args, transcript)
        if not seconds:
            return None, _fail(
                operation,
                "AMBIGUOUS",
                extra={"clarify": "For how long should I set the timer?"},
            )
        out["duration_seconds"] = seconds
        out["title"] = str(args.get("title") or args.get("label") or "")[:80]
    elif operation == "create_reminder":
        title = str(args.get("title") or args.get("text") or "").strip()[:200]
        if not title:
            title = "Reminder"
        when_iso = parse_when_iso(args, handshake=handshake, transcript=transcript)
        if not when_iso:
            seconds = parse_duration_seconds(args, transcript)
            if seconds:
                when_iso = (datetime.now(_tz(handshake)) + timedelta(seconds=seconds)).isoformat()
        if not when_iso:
            return None, _fail(
                operation,
                "AMBIGUOUS",
                extra={"clarify": "When should I remind you?"},
            )
        out["title"] = title
        out["when_iso"] = when_iso
        out["list"] = str(args.get("list") or "")[:80] or None
    elif operation == "create_alarm":
        when_iso = parse_when_iso(args, handshake=handshake, transcript=transcript)
        if not when_iso:
            return None, _fail(
                operation,
                "AMBIGUOUS",
                extra={"clarify": "What time should I set the alarm?"},
            )
        out["when_iso"] = when_iso
        out["title"] = str(args.get("title") or args.get("label") or "Alarm")[:80]
    elif operation in {"call_contact", "facetime_contact", "message_contact"}:
        query = str(args.get("contact_query") or args.get("contact") or args.get("name") or "").strip()[:80]
        number = normalize_phone_number(
            str(args.get("phone_number") or args.get("number") or "")
        )
        if is_emergency(query) or is_emergency(str(args.get("phone_number") or "")):
            return None, _fail(operation, "EMERGENCY_BLOCKED")
        if number and not query:
            out["phone_number"] = number
            out["contact_query"] = number
        elif query and _PHONE_RE.match(query):
            if is_emergency(query):
                return None, _fail(operation, "EMERGENCY_BLOCKED")
            parsed = normalize_phone_number(query)
            if not parsed:
                return None, _fail(operation, "EMERGENCY_BLOCKED")
            out["phone_number"] = parsed
            out["contact_query"] = query
        elif query:
            out["contact_query"] = query
        else:
            return None, _fail(
                operation,
                "AMBIGUOUS",
                extra={"clarify": "Who should I contact?"},
            )
        if operation == "message_contact":
            message = str(args.get("message") or args.get("text") or "").strip()[:500]
            if not message:
                return None, _fail(
                    operation,
                    "AMBIGUOUS",
                    extra={"clarify": "What should the message say?"},
                )
            out["message"] = message
    elif operation in {"start_directions", "open_maps"}:
        dest = str(args.get("destination") or args.get("query") or args.get("text") or "").strip()[:200]
        if not dest:
            return None, _fail(
                operation,
                "AMBIGUOUS",
                extra={"clarify": "Where should I open Maps to?"},
            )
        if dest.lower() in reg.HOME_QUERIES:
            return None, _fail(operation, "HOME_ADDRESS_UNAVAILABLE")
        out["destination"] = dest
        out["url"] = maps_url(dest, directions=operation == "start_directions")
    elif operation == "create_calendar_event":
        title = str(args.get("title") or args.get("text") or "").strip()[:200]
        when_iso = parse_when_iso(args, handshake=handshake, transcript=transcript)
        if not title or not when_iso:
            return None, _fail(
                operation,
                "AMBIGUOUS",
                extra={"clarify": "What should I add, and when?"},
            )
        out["title"] = title
        out["when_iso"] = when_iso
        if args.get("duration_minutes"):
            try:
                start = datetime.fromisoformat(when_iso)
                out["end_iso"] = (start + timedelta(minutes=int(args["duration_minutes"]))).isoformat()
            except (TypeError, ValueError):
                out["end_iso"] = None
        out["location"] = str(args.get("location") or "")[:120] or None
    elif operation == "set_focus":
        focus = str(args.get("focus") or args.get("value") or args.get("name") or "").strip()[:40]
        if not focus:
            return None, _fail(
                operation,
                "AMBIGUOUS",
                extra={"clarify": "Which Focus — Do Not Disturb, Work, Sleep?"},
            )
        out["focus"] = focus
    elif operation in {"share_content", "copy_to_clipboard"}:
        text = str(args.get("text") or args.get("message") or args.get("value") or "").strip()[:4000]
        if not text:
            return None, _fail(
                operation,
                "AMBIGUOUS",
                extra={"clarify": "What should I copy or share?"},
            )
        out["text"] = text
        out["share_text"] = text
        out["copy_text"] = text
        out["pwa_kind"] = "share" if operation == "share_content" else "clipboard"
    elif operation == "media_play_pause":
        action = str(args.get("media_action") or args.get("value") or "pause").strip().lower()
        if action in {"play", "resume"}:
            action = "play"
        elif action in {"pause", "stop"}:
            action = "pause"
        elif action in {"next", "previous"}:
            pass
        else:
            action = "pause"
        out["media_action"] = action
    elif operation == "self_test":
        out["pwa_kind"] = "bridge"
    else:
        return None, _fail(operation, "ACTION_UNAVAILABLE")
    return out, None


def _target_is_local(
    target_device: str,
    *,
    device_id: str,
    role: str,
) -> tuple[bool, str | None]:
    want = (target_device or "this_phone").strip().lower()
    if want in {"this_phone", "this", "here", ""}:
        return True, None
    if want == "primary" and role == "primary_companion":
        return True, None
    if want == "secondary" and role == "secondary_companion":
        return True, None
    return False, "REMOTE_PHONE_UNSUPPORTED"


def create_phone_action(
    *,
    device_id: str,
    role: str,
    instance_id: str,
    session_id: str | None,
    origin: str,
    arguments: dict[str, Any],
    transcript: str = "",
    device_label: str = "This iPhone",
    confirm: bool = False,
) -> dict[str, Any]:
    from .engine import create_phone_action as _engine_create

    return _engine_create(
        device_id=device_id,
        role=role,
        instance_id=instance_id,
        session_id=session_id,
        origin=origin,
        arguments=arguments,
        transcript=transcript,
        device_label=device_label,
        confirm=confirm,
    )


def _legacy_create_phone_action_unused(
    *,
    device_id: str,
    role: str,
    instance_id: str,
    session_id: str | None,
    origin: str,
    arguments: dict[str, Any],
    transcript: str = "",
    device_label: str = "This iPhone",
    confirm: bool = False,
) -> dict[str, Any]:
    operation = str(arguments.get("operation") or "").strip()
    if reg.is_blocked(operation) or not operation:
        return _fail(operation or "unknown", "HIGH_RISK" if operation else "ACTION_UNAVAILABLE")
    if operation == "run_shortcut":
        return _fail(operation, "HIGH_RISK")
    cap = reg.get_capability(operation)
    if cap is None:
        return _fail(operation, "ACTION_UNAVAILABLE")

    local, remote_err = _target_is_local(
        str(arguments.get("target_device") or "this_phone"),
        device_id=device_id,
        role=role,
    )
    if not local:
        return _fail(operation, remote_err or "REMOTE_PHONE_UNSUPPORTED")

    handshake = store.handshake_of(device_id)
    normalized, err = _normalize(operation, arguments, handshake=handshake, transcript=transcript)
    if err:
        return err
    assert normalized is not None

    has_number = bool(normalized.get("phone_number"))
    has_url = bool(normalized.get("url"))
    decision = route(
        operation,
        handshake=handshake,
        has_explicit_number=has_number,
        has_url=has_url,
    )
    if decision["method"] == "unsupported":
        return _fail(operation, str(decision.get("reason") or "ACTION_UNAVAILABLE"))

    confirm_needed = cap.confirmation == "required" and not confirm
    confirm_id = str(arguments.get("confirm_action_id") or "").strip()
    if confirm_id:
        existing = store.get_action(confirm_id)
        if (
            existing
            and existing.get("device_id") == device_id
            and existing.get("operation") == operation
            and existing.get("state") == "awaiting_confirmation"
        ):
            return confirm_action(
                action_id=confirm_id,
                device_id=device_id,
                origin=origin,
            )
        return _fail(operation, "EXPIRED")

    state = "awaiting_confirmation" if confirm_needed else "authorized"
    row = store.create_action(
        {
            "device_id": device_id,
            "instance_id": instance_id[:64],
            "session_id": session_id or "",
            "operation": operation,
            "class_level": cap.class_level,
            "method": decision["method"],
            "verification_quality": cap.verification,
            "normalized": normalized,
            "state": state,
            "requires_user_interaction": True,
            "device_label": device_label,
            "ttl_s": ACTION_TTL_S,
            "origin": origin.rstrip("/"),
        }
    )
    if state == "authorized":
        run = _authorized_run(row, origin=origin.rstrip("/"))
        row = store.update_action(row["action_id"], authorized_run=run) or row
    launch = _launch_payload(row, origin=origin.rstrip("/")) if state == "authorized" else {
        "launch_url": None,
        "open_url": None,
        "run": None,
        "card": _card(row, launch_url=None, open_url=None),
    }
    spoken = _spoken(row, pending=state == "awaiting_confirmation")
    public = store.public_row(row)
    result = {
        "ok": True,
        "accepted": True,
        "executed": False,
        "verified": False,
        "operation": operation,
        "action_id": row["action_id"],
        "spoken": spoken,
        "requires_user_interaction": True,
        "confirmation_required": state == "awaiting_confirmation",
        "method": row.get("method"),
        "verification_quality": cap.verification,
        "card": launch["card"],
        "launch_url": launch.get("launch_url"),
        "open_url": launch.get("open_url"),
        "receipt": public,
        "bridge_name": "Evie Mobile Bridge",
        "must_continue": True,
        "completion_claim_allowed": False,
    }
    return result


def confirm_action(*, action_id: str, device_id: str, origin: str) -> dict[str, Any]:
    from .engine import confirm_action as _engine_confirm

    return _engine_confirm(action_id=action_id, device_id=device_id, origin=origin)


def _legacy_confirm_action_unused(*, action_id: str, device_id: str, origin: str) -> dict[str, Any]:
    row = store.get_action(action_id)
    if row is None or row.get("device_id") != device_id:
        return _fail("unknown", "EXPIRED")
    if row.get("state") not in {"awaiting_confirmation", "authorized"}:
        if row.get("state") == "executed":
            return {
                "ok": True,
                "executed": True,
                "verified": bool((row.get("receipt") or {}).get("verified")),
                "spoken": _spoken(row),
                "receipt": store.public_row(row),
                "action_id": action_id,
            }
        return _fail(str(row.get("operation")), str(row.get("failure") or "EXPIRED"))
    run = _authorized_run(row, origin=origin.rstrip("/"))
    row = store.update_action(action_id, state="authorized", authorized_run=run) or row
    launch = _launch_payload(row, origin=origin.rstrip("/"))
    return {
        "ok": True,
        "accepted": True,
        "executed": False,
        "verified": False,
        "operation": row.get("operation"),
        "action_id": action_id,
        "spoken": _spoken(row),
        "requires_user_interaction": True,
        "confirmation_required": False,
        "method": row.get("method"),
        "card": launch["card"],
        "launch_url": launch.get("launch_url"),
        "open_url": launch.get("open_url"),
        "receipt": store.public_row(row),
        "must_continue": True,
        "completion_claim_allowed": False,
    }


def cancel_action(*, action_id: str, device_id: str) -> dict[str, Any]:
    row = store.get_action(action_id)
    if row is None or row.get("device_id") != device_id:
        return _fail("unknown", "EXPIRED")
    if row.get("state") in {"executed"}:
        return {
            "ok": False,
            "failure": "ALREADY_EXECUTED",
            "spoken": "That already ran.",
            "action_id": action_id,
        }
    store.update_action(
        action_id,
        state="cancelled",
        failure="CANCELLED",
        receipt={"result": "CANCELLED", "verified": True},
    )
    store.consume_action_token(str(row.get("action_token") or ""))
    store.consume_completion_token(str(row.get("completion_token") or ""))
    return {
        "ok": True,
        "executed": False,
        "failure": "CANCELLED",
        "spoken": "Cancelled.",
        "action_id": action_id,
        "receipt": store.public_row(store.get_action(action_id) or row),
    }


def resolve_action(*, token: str, device_id: str | None) -> dict[str, Any]:
    """Shortcut asks Core for the authoritative action. Input arguments are not used."""

    row = store.action_by_token(token)
    if row is None:
        return {"ok": False, "error": "INVALID_TOKEN", "run": {"kind": "noop"}}
    if device_id and str(row.get("device_id")) != str(device_id):
        return {"ok": False, "error": "WRONG_DEVICE", "run": {"kind": "noop"}}
    now = time.time()
    if float(row.get("exp") or 0) < now:
        store.update_action(row["action_id"], state="expired", failure="EXPIRED")
        return {"ok": False, "error": "EXPIRED", "run": {"kind": "noop"}}
    if row.get("state") in {"cancelled"}:
        return {"ok": False, "error": "CANCELLED", "run": {"kind": "noop"}}
    if row.get("state") in {"executed", "failed"}:
        return {"ok": False, "error": "REPLAY", "run": {"kind": "noop"}}
    if row.get("state") == "awaiting_confirmation":
        return {"ok": False, "error": "CONFIRMATION_REQUIRED", "run": {"kind": "noop"}}
    if row.get("claimed"):
        return {"ok": False, "error": "REPLAY", "run": {"kind": "noop"}}
    if row.get("state") not in {"authorized", "resolved"}:
        return {"ok": False, "error": "INVALID_TOKEN", "run": {"kind": "noop"}}
    if not store.resolve_window_ok(row, now) and row.get("resolved_at"):
        return {"ok": False, "error": "REPLAY", "run": {"kind": "noop"}}
    origin = str(row.get("origin") or "").rstrip("/")
    run = row.get("authorized_run") or _authorized_run(row, origin=origin)
    store.update_action(
        row["action_id"],
        state="resolved",
        resolved_at=row.get("resolved_at") or now,
        authorized_run=run,
    )
    return {
        "ok": True,
        "action_id": row["action_id"],
        "operation": row["operation"],
        "run": run,
        "protocol": BRIDGE_PROTOCOL,
        "bridge_version": BRIDGE_VERSION,
    }


def claim_action(*, action_id: str, completion_token: str) -> dict[str, Any]:
    row = store.get_action(action_id)
    if row is None or str(row.get("completion_token")) != completion_token:
        return {"ok": False, "error": "INVALID_TOKEN"}
    if row.get("state") in {"executed", "failed", "cancelled", "expired"}:
        return {"ok": False, "error": "REPLAY"}
    if row.get("claimed"):
        return {"ok": False, "error": "REPLAY"}
    if float(row.get("exp") or 0) < time.time():
        store.update_action(action_id, state="expired", failure="EXPIRED")
        return {"ok": False, "error": "EXPIRED"}
    store.update_action(action_id, claimed=True, state="executing")
    store.consume_action_token(str(row.get("action_token") or ""))
    return {"ok": True, "action_id": action_id, "operation": row.get("operation")}


def complete_action(
    *,
    action_id: str,
    completion_token: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    row = store.get_action(action_id)
    if row is None or str(row.get("completion_token")) != completion_token:
        return {"ok": False, "error": "INVALID_TOKEN"}
    if row.get("state") in {"executed", "failed"}:
        return {"ok": True, "idempotent": True, "receipt": store.public_row(row)}
    if row.get("state") in {"cancelled", "expired"}:
        return {"ok": False, "error": str(row.get("failure") or row.get("state"))}
    cap = reg.get_capability(str(row.get("operation") or ""))
    if cap and cap.class_level >= 2 and not row.get("claimed") and not payload.get("allow_unclaimed"):
        return {"ok": False, "error": "NOT_CLAIMED"}
    clean = sanitize_complete(payload)
    status = str(clean.get("status") or payload.get("status") or "executed").lower()
    result = str(clean.get("result") or "").upper()
    failure = str(clean.get("failure") or "")
    if status in {"cancelled", "cancel"} or result == "USER_CANCELLED":
        state = "cancelled"
        failure = failure or "USER_CANCELLED"
        result = "CANCELLED"
        verified = True
        executed = False
    elif result in {"PERMISSION_REQUIRED", "PERMISSION_DENIED"}:
        state = "failed"
        failure = result
        verified = True
        executed = False
    elif result in {"CONTACT_NOT_FOUND", "CONTACT_AMBIGUOUS", "ACTION_UNAVAILABLE", "EXECUTION_FAILED"}:
        state = "failed"
        failure = result
        verified = True
        executed = False
        if result == "CONTACT_AMBIGUOUS":
            store.update_action(action_id, choices=clean.get("choices") or [])
    elif status in {"failed", "error"}:
        state = "failed"
        failure = failure or "EXECUTION_FAILED"
        result = result or "FAILED"
        verified = False
        executed = False
    else:
        state = "executed"
        executed = True
        if not result:
            if str(row.get("operation")) in {"call_contact", "facetime_contact", "start_directions", "open_maps"} or str(row.get("operation")) == "message_contact":
                result = "SYSTEM_UI_OPENED"
            elif str(row.get("operation")) in {"create_timer", "create_reminder", "create_alarm", "create_calendar_event"}:
                result = "CREATED"
            else:
                result = "EXECUTED"
        if str(row.get("operation")) == "message_contact" and result == "SENT":
            result = "SYSTEM_UI_OPENED"
        verified = bool(clean.get("verified"))
        if result in {"CREATED", "SENT", "SELF_TEST_OK"}:
            verified = True
        if result == "SYSTEM_UI_OPENED":
            verified = False
            clean["requires_user_interaction"] = True
            clean["system_ui_presented"] = True
    receipt = {
        "result": result,
        "verified": verified,
        "failure": failure or None,
        "display_name": clean.get("display_name"),
        "masked_destination": clean.get("masked_destination"),
        "choices": clean.get("choices") or [],
        "requires_user_interaction": bool(clean.get("requires_user_interaction")),
        "system_ui_presented": bool(clean.get("system_ui_presented") or result == "SYSTEM_UI_OPENED"),
    }
    updated = store.update_action(
        action_id,
        state=state,
        failure=failure or None,
        completed=True,
        receipt=receipt,
        result=result,
    ) or row
    store.consume_action_token(str(row.get("action_token") or ""))
    store.consume_completion_token(completion_token)
    public = store.public_row(updated)
    spoken = _spoken(updated)
    return {"ok": True, "receipt": public, "spoken": spoken, "executed": executed, "verified": verified}


def status_snapshot(*, device_id: str, role: str, display_name: str) -> dict[str, Any]:
    from .engine import status_snapshot as _engine_status

    return _engine_status(device_id=device_id, role=role, display_name=display_name)


def _legacy_status_snapshot_unused(*, device_id: str, role: str, display_name: str) -> dict[str, Any]:
    handshake = store.handshake_of(device_id)
    installed = bool(handshake.get("bridge_installed"))
    compatible = handshake.get("compatible", True) if handshake else True
    reported = set(handshake.get("capabilities") or [])
    rows = []
    for name in (*reg.CORE_V1_OPERATIONS, *sorted(reg.HANDSHAKE_ONLY_OPERATIONS)):
        cap = reg.get_capability(name)
        if cap is None:
            continue
        if name in reg.HANDSHAKE_ONLY_OPERATIONS and name not in reported:
            rows.append(reg.public_capability_row(cap, available=False, reason="not reported by Bridge"))
            continue
        if cap.needs_bridge and not installed:
            available = name in {"call_contact", "message_contact", "facetime_contact"}
            reason = None if available else "Evie Mobile Bridge not installed"
            # Number-only handoff still possible; contact-by-name is not.
            if name in {"call_contact", "message_contact", "facetime_contact"}:
                reason = "contact names need Bridge; numbers can use Phone/Messages"
                available = True
            rows.append(reg.public_capability_row(cap, available=available, reason=reason))
            continue
        if cap.needs_bridge and not compatible:
            rows.append(reg.public_capability_row(cap, available=False, reason="Mobile Actions needs an update"))
            continue
        if installed and reported and name not in reported and cap.needs_bridge:
            rows.append(reg.public_capability_row(cap, available=False, reason="not on this iPhone"))
            continue
        rows.append(reg.public_capability_row(cap, available=True, reason=None))
    last = store.last_for_device(device_id)
    return {
        "bridge_name": "Evie Mobile Bridge",
        "bridge_installed": installed,
        "bridge_version": handshake.get("bridge_version") or None,
        "protocol": handshake.get("protocol") or BRIDGE_PROTOCOL,
        "compatible": compatible,
        "this_device": display_name or role,
        "role": role,
        "timezone": handshake.get("timezone") or None,
        "locale": handshake.get("locale") or None,
        "capability_hash": store.capability_hash(handshake),
        "last_action": last,
        "callback_transport": "gateway_https",
        "native_shell": "DEFERRED",
        "remote_unattended": False,
        "capabilities": rows,
    }


_TEXT_TIMER = re.compile(
    r"\b(?:set|start|make)\s+(?:a\s+)?timer\b",
    re.IGNORECASE,
)
_TEXT_REMIND = re.compile(r"\bremind(?:er)?\s+me\b", re.IGNORECASE)
_TEXT_CALL = re.compile(r"\b(?:call|facetime)\s+([A-Za-z][A-Za-z0-9'+\- ]{1,40})\s*$", re.IGNORECASE)
_TEXT_MESSAGE = re.compile(
    r"\b(?:message|text)\s+([A-Za-z][A-Za-z0-9'+\- ]{1,40})\s+(?:that|saying|and say)\s+(.+)$",
    re.IGNORECASE,
)
_TEXT_MAPS = re.compile(
    r"\b(?:(?:give me |get )?directions? to|navigate to|take me to|open maps(?: to)?)\s+(.+)$",
    re.IGNORECASE,
)


def infer_from_text(text: str) -> dict[str, Any] | None:
    from .engine import infer_from_text as _engine_infer

    return _engine_infer(text)


def _legacy_infer_from_text_unused(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if _TEXT_TIMER.search(raw):
        return {"operation": "create_timer"}
    if _TEXT_REMIND.search(raw):
        title = re.sub(r"^\s*remind(?:er)?\s+me\s+(?:to\s+)?", "", raw, flags=re.I).strip()[:200]
        return {"operation": "create_reminder", "title": title or "Reminder", "text": raw}
    facetime = bool(re.search(r"\bfacetime\b", raw, re.I))
    call = _TEXT_CALL.search(raw)
    if call and not re.search(r"\bremind(?:er)?\b.{0,40}\bcall\b", raw, re.I):
        return {
            "operation": "facetime_contact" if facetime else "call_contact",
            "contact_query": call.group(1).strip(),
        }
    message = _TEXT_MESSAGE.search(raw)
    if message:
        return {
            "operation": "message_contact",
            "contact_query": message.group(1).strip(),
            "message": message.group(2).strip().strip("\"'"),
        }
    maps = _TEXT_MAPS.search(raw)
    if maps:
        dest = maps.group(1).strip().rstrip(".")
        op = "start_directions" if re.search(r"\b(directions?|navigate|take me)\b", raw, re.I) else "open_maps"
        return {"operation": op, "destination": dest}
    return None


def client_complete(
    *,
    action_id: str,
    device_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Paired PWA may complete Layer A actions it executed itself (share/clipboard/open_url)."""

    row = store.get_action(action_id)
    if row is None or str(row.get("device_id")) != str(device_id):
        return {"ok": False, "error": "INVALID_TOKEN"}
    if row.get("method") not in {"web_handoff", "app_url"}:
        return {"ok": False, "error": "USE_COMPLETION_TOKEN"}
    return complete_action(
        action_id=action_id,
        completion_token=str(row.get("completion_token") or ""),
        payload={**payload, "allow_unclaimed": True},
    )


def apply_handshake(*, device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = store.put_handshake(device_id, payload)
    return {"ok": True, "handshake": row, "status": status_snapshot(device_id=device_id, role="", display_name="")}
