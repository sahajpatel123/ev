"""Trusted iPhone → Home Station action plane.

Safari Evie cannot run native Clock, Reminders, Mail, or Mac apps. Those
jobs run on Home Station through the same `dispatch` path Mac Talk uses.
Muse Spark 1.3 may choose the tool when the phrase book misses.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device

from .sandbox import is_sandbox_device

_CAMERA = frozenset({"look", "observe_camera", "capture_photo", "record_video"})
_BLOCKED = frozenset(
    {
        "execute_command",
        "drone",
        "ui_action",
        "inspect_ui",
        "screen_look",
        "actuate",
        "print_start",
        "camera_replay",
        "app_action",
    }
)
_NEGATED_RE = re.compile(
    r"\b(?:don'?t|do not|never|not)\s+(?:open|close|start|set|send|call|launch|remind)\b",
    re.I,
)
_HEARING_RE = re.compile(
    r"after i finish this sentence|my test phrase is|are you listening|"
    r"say exactly:|what did i just ask|repeat after me",
    re.I,
)
_WORD_MINUTES = {
    "a": 1.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
    "eleven": 11.0,
    "twelve": 12.0,
    "fifteen": 15.0,
    "twenty": 20.0,
    "thirty": 30.0,
    "forty": 40.0,
    "forty-five": 45.0,
    "sixty": 60.0,
}
_TIMER_WORD_RE = re.compile(
    r"\b(?:start |set )?(?:a )?timer (?:for )?(?P<word>"
    + "|".join(re.escape(w) for w in sorted(_WORD_MINUTES, key=len, reverse=True))
    + r")\s*(?:min|mins|minute|minutes)\b",
    re.I,
)
_OPEN_CALC_RE = re.compile(
    r"\b(?:open|launch|start|bring up)\s+(?:the\s+)?calculator\b",
    re.I,
)
_CLOSE_CALC_RE = re.compile(r"\b(?:close|quit)\s+(?:the\s+)?(?:calculator|calc)\b", re.I)


def _action_status(*, ok: bool, executed: bool, queued: bool) -> str:
    if not ok:
        return "FAILED"
    if queued and not executed:
        return "QUEUED"
    if executed:
        return "COMPLETED"
    return "ACCEPTED"


def _ok(
    reply: str,
    *,
    route: str,
    tool: str,
    executed: bool,
    accepted: bool = True,
    verified: bool | None = None,
    queued: bool = False,
    ok: bool = True,
    error_code: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    verified_value = executed if verified is None else bool(verified)
    payload: dict[str, Any] = {
        "reply": reply,
        "ok": bool(ok),
        "accepted": bool(accepted),
        "route": route,
        "operation": tool,
        "tool": tool,
        "status": _action_status(ok=bool(ok), executed=executed, queued=queued),
        "turn_id": None,
        "executed": bool(executed),
        "verified": verified_value,
        "queued": bool(queued),
        "error_code": error_code,
        "conversational": False,
        "provenance": "home_station.dispatch",
    }
    if extra:
        payload.update(extra)
    return payload


def _phrase_action(text: str) -> tuple[str, dict[str, Any]] | None:
    word = _TIMER_WORD_RE.search(text)
    if word:
        minutes = _WORD_MINUTES.get(word.group("word").lower())
        if minutes:
            return "start_timer", {"minutes": minutes}
    if _OPEN_CALC_RE.search(text):
        return "open_app", {"name": "Calculator"}
    if _CLOSE_CALC_RE.search(text):
        return "close_app", {"name": "Calculator"}
    return None


async def _phone_reminder_action(
    session: AsyncSession,
    *,
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    from app.ev import alert_radar

    reminders = await alert_radar.list_alerts(
        session,
        status="pending",
        kind="reminder",
        limit=50,
    )
    if name == "list_reminders":
        items = [
            {
                "id": str(row.id),
                "text": str(row.body or row.title or "untitled"),
                "status": str(row.status or "pending"),
            }
            for row in reminders
        ]
        if not items:
            spoken = "No pending reminders."
        elif len(items) == 1:
            spoken = f"One pending reminder: {items[0]['text']}."
        else:
            spoken = f"{len(items)} pending reminders. Next: {items[0]['text']}."
        return _ok(
            spoken,
            route="HOME_STATION",
            tool=name,
            executed=True,
            verified=True,
            extra={"reminders": items, "count": len(items)},
        )

    target = str(args.get("text") or args.get("query") or "").strip().lower()
    matches = (
        [
            row
            for row in reminders
            if target in str(row.body or row.title or "").lower()
        ]
        if target
        else reminders
    )
    if len(matches) > 1:
        labels = [str(row.body or row.title or "untitled") for row in matches[:3]]
        return _ok(
            "I found multiple matching reminders: "
            + ", ".join(labels)
            + ". Tell me which one to cancel.",
            route="HOME_STATION",
            tool=name,
            executed=False,
            ok=False,
            error_code="AMBIGUOUS",
            extra={"candidates": [str(row.id) for row in matches[:3]]},
        )
    if not matches:
        return _ok(
            "I don't have a matching pending reminder.",
            route="HOME_STATION",
            tool=name,
            executed=False,
            ok=False,
            error_code="NOT_FOUND",
        )
    row = await alert_radar.dismiss_alert(
        session,
        matches[0].id,
        reason="phone_owner",
    )
    await session.flush()
    return _ok(
        f"Reminder cancelled: {row.body or row.title}.",
        route="HOME_STATION",
        tool=name,
        executed=True,
        verified=True,
        extra={"reminder_id": str(row.id)},
    )


def _spoken_from_dispatch(response: Any, name: str, payload: dict[str, Any]) -> str:
    spoken = str(payload.get("spoken") or payload.get("owner_message") or "").strip()
    if spoken:
        if name in {"list_mail", "calendar_read", "list_messages"}:
            return spoken[:1600]
        return spoken
    if name == "calendar_read" and payload.get("error") == "not_connected":
        return (
            "I checked Home Station Calendar — it isn't connected yet. "
            "Safari Evie also can't read Apple Calendar on this iPhone."
        )
    if name == "list_mail" and payload.get("error") == "not_connected":
        return "Home Station Mail isn't connected yet, so I can't read the inbox from this iPhone."
    if name == "list_messages" and payload.get("error") == "not_connected":
        return "Home Station Messages isn't connected yet."
    next_step = str(payload.get("next_step") or payload.get("error") or response.error or "").strip()
    if not response.ok or payload.get("ok") is False or payload.get("degraded"):
        if next_step:
            return f"I couldn't complete that on Home Station. {next_step}"[:400]
        return "I couldn't complete that on Home Station."
    if name in {"set_reminder", "send_message", "place_call", "present", "code"}:
        from app.ev.tools import life_success_reply

        shaped = life_success_reply(payload, tool_name=name).strip()
        if shaped and not shaped.startswith("Sent to the recipient"):
            return shaped
    return "Done on Home Station."


def utterance_from_phone_action(arguments: dict[str, Any], transcript: str) -> str:
    raw = (transcript or "").strip()
    if raw:
        return raw
    args = arguments if isinstance(arguments, dict) else {}
    op = str(args.get("operation") or "").strip()
    if op in {"create_timer", "start_timer"}:
        minutes = args.get("duration_minutes")
        if minutes is None and args.get("duration_seconds"):
            try:
                minutes = float(args["duration_seconds"]) / 60.0
            except (TypeError, ValueError):
                minutes = None
        if minutes:
            return f"set a timer for {minutes} minutes"
    if op in {"create_reminder", "set_reminder"}:
        title = str(args.get("title") or args.get("text") or args.get("message") or "").strip()
        if title:
            return f"remind me to {title}"
    if op == "open_app":
        name = str(args.get("app_id") or args.get("title") or args.get("name") or "").strip()
        if name:
            return f"open {name}"
    if op in {"call_contact", "place_call"}:
        who = str(args.get("contact_query") or args.get("name") or "").strip()
        if who:
            return f"call {who}"
    if op in {"message_contact", "send_message"}:
        who = str(args.get("contact_query") or args.get("to") or "").strip()
        body = str(args.get("message") or args.get("text") or "").strip()
        if who and body:
            return f"text {who} {body}"
    return str(args.get("text") or "").strip()


async def maybe_phone_mac_act(
    session: AsyncSession,
    *,
    device: Device,
    text: str,
    idempotency_key: str | None = None,
) -> dict[str, Any] | None:
    if is_sandbox_device(device) or device.revoked_at is not None:
        return None
    raw = (text or "").strip()
    if not raw:
        return None
    if _NEGATED_RE.search(raw) or _HEARING_RE.search(raw):
        return None

    from app.ev.spark_phone import looks_like_phone_chat, spark_phone_tool
    from app.ev.tool_select import resolve_live_action

    if looks_like_phone_chat(raw):
        return None

    resolved = _phrase_action(raw)
    if resolved is None:
        resolved = resolve_live_action(raw)
    if resolved is None:
        resolved = await spark_phone_tool(raw)
    if resolved is None:
        return None
    name, args = resolved
    if name in _CAMERA or name in _BLOCKED:
        return None
    args = dict(args or {})
    if name in {"list_reminders", "cancel_reminder"}:
        return await _phone_reminder_action(session, name=name, args=args)
    if name == "send_message":
        if not args.get("text") and args.get("message"):
            args["text"] = args["message"]
        if not args.get("to") and args.get("contact_query"):
            args["to"] = args["contact_query"]
        if not str(args.get("to") or "").strip() or not str(args.get("text") or "").strip():
            return _ok(
                "I need both the message recipient and the message text.",
                route="HOME_STATION",
                tool=name,
                executed=False,
                ok=False,
                error_code="MISSING_MESSAGE_FIELDS",
            )
    if name == "place_call" and re.search(r"\bfacetime\b", raw, re.I):
        args.setdefault("kind", "facetime")
    if idempotency_key and name in {"start_timer", "set_reminder", "send_message"} and "idempotency_key" not in args:
        args["idempotency_key"] = idempotency_key[:80]

    if idempotency_key and name in {"start_timer", "set_reminder", "send_message"}:
        from app.ev.actuator import prior_result

        prior = await prior_result(
            session,
            name=name,
            key=idempotency_key[:128],
        )
        if prior is not None:
            replayed_ok = bool(prior.get("ok", True))
            replayed_executed = bool(prior.get("executed", replayed_ok))
            return _ok(
                str(prior.get("spoken") or "Done on Home Station."),
                route="HOME_STATION",
                tool=name,
                executed=replayed_executed,
                ok=replayed_ok,
                queued=bool(prior.get("queued")),
                verified=bool(prior.get("verified", replayed_executed)),
                error_code=str(prior.get("error") or "").strip() or None,
                extra={"idempotent_replay": True, "idempotency_key": idempotency_key[:128]},
            )

    if name == "cancel_timer" and not args.get("id") and not args.get("text"):
        from app.ev.timers import list_timers

        pending = await list_timers(session)
        items = pending.get("timers") if isinstance(pending, dict) else []
        if isinstance(items, list) and len(items) > 1:
            labels = [
                str(item.get("text") or "untitled")
                for item in items[:3]
                if isinstance(item, dict)
            ]
            suffix = ", ".join(labels)
            return _ok(
                f"I found multiple pending timers: {suffix}. Tell me which one to cancel.",
                route="HOME_STATION",
                tool=name,
                executed=False,
                ok=False,
                error_code="AMBIGUOUS",
                extra={"candidates": items[:3]},
            )
        if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
            args["id"] = str(items[0].get("id") or "")

    from app.ev.tools import dispatch

    # Computer tools must run on Home Station's Mac helper, not this iPhone.
    computerish = name in {
        "open_app",
        "close_app",
        "activate_app",
        "list_apps",
        "computer_status",
        "open_url",
        "computer",
        "code",
    }
    response = await dispatch(
        session,
        name,
        args,
        actor="voice",
        allow_sensitive=True,
        request_id=idempotency_key,
        device_id=None if computerish else device.id,
        live_session_id=None,
        channel="voice",
        audit_endpoint="POST /v1/device-gateway/text",
    )
    payload = response.result if isinstance(response.result, dict) else {}
    if (
        name == "send_message"
        and idempotency_key
        and response.ok
        and payload.get("sent") is True
    ):
        from app.ev.actuator import record_actuator

        await record_actuator(
            session,
            name=name,
            actor="voice",
            key=idempotency_key,
            result=payload,
            target=str(args.get("to") or ""),
        )
    spoken = _spoken_from_dispatch(response, name, payload)
    queued = bool(
        payload.get("queued")
        or payload.get("status") in {"QUEUED", "ROUTED"}
    )
    executed = bool(
        response.ok
        and payload.get("ok", True) is not False
        and not payload.get("degraded")
    )
    error_code = str(
        payload.get("error_code")
        or payload.get("error")
        or response.error
        or ""
    ).strip() or None
    if error_code == "not_connected":
        executed = False
    action_ok = bool(response.ok and payload.get("ok", True) is not False and not payload.get("degraded"))
    if error_code == "not_connected" and not queued:
        action_ok = False
    if name == "send_message":
        sent = payload.get("sent") is True
        executed = bool(executed and sent)
        action_ok = bool(action_ok and sent)
    connected = payload.get("connected") is True or payload.get("answered") is True
    if name == "place_call":
        initiated = payload.get("opened") is True or payload.get("dialed") is True
        executed = bool(executed and initiated)
        action_ok = bool(action_ok and initiated)
        verified = bool(connected)
    else:
        verified = bool(payload.get("verified", executed))
    return _ok(
        spoken,
        route="HOME_STATION",
        tool=name,
        executed=executed,
        queued=queued,
        ok=action_ok,
        error_code=error_code,
        verified=verified,
        extra={
            "tool_ok": bool(response.ok),
            "tool_error": response.error,
            "to": str(args.get("to") or "").strip() or None,
            "channel": payload.get("channel") or args.get("channel"),
            "sent": payload.get("sent"),
            "opened": payload.get("opened"),
            "connected": payload.get("connected"),
            "answered": payload.get("answered"),
            "kind": payload.get("kind") or args.get("kind"),
        },
    )
