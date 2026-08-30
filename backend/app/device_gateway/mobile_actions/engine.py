"""Native-shell action engine. Product path for phone_action."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

from . import ACTION_TTL_S, NATIVE_BROKER_VERSION, apps as app_registry, registry as reg, store
from .strategy import route
from .trust import (
    classify_utterance,
    confirmation_ttl_s,
    freeze_hash,
    is_negated,
    mutate_duration_seconds,
    pending_query_spoken,
    wants_draft,
)

_FAIL_SPOKEN = {
    "NATIVE_SHELL_REQUIRED": (
        "Open Evie as the iPhone app to do that on this phone. "
        "The Home Screen page can't run that native action."
    ),
    "NATIVE_ACTIONS_DISABLED": "Native iPhone actions are turned off right now. Voice still works.",
    "APP_UNSUPPORTED": "I can't open that app directly yet.",
    "CANCELLED": "I won't do that.",
}


def _svc():
    from . import service as svc

    return svc


def _fail(operation: str, failure: str, *, extra: dict[str, Any] | None = None, spoken: str | None = None) -> dict[str, Any]:
    return _svc()._fail(
        operation,
        failure,
        spoken=spoken or _FAIL_SPOKEN.get(failure),
        extra=extra,
    )


def _actions_enabled() -> bool:
    try:
        from app.config import get_settings

        return bool(getattr(get_settings(), "native_actions_enabled", True))
    except Exception:
        return True


def _public_action_result(row: dict[str, Any], launch: dict[str, Any], *, cap: Any) -> dict[str, Any]:
    svc = _svc()
    state = str(row.get("state") or "")
    native = row.get("method") == "native_broker"
    system_ui = bool(getattr(cap, "system_confirmation_required", False)) if cap is not None else False
    interaction = (not native) or system_ui or state in {"awaiting_confirmation", "draft"}
    return {
        "ok": True,
        "accepted": True,
        "executed": False,
        "verified": False,
        "operation": row.get("operation"),
        "action_id": row["action_id"],
        "spoken": svc._spoken(row, pending=state == "awaiting_confirmation"),
        "requires_user_interaction": interaction,
        "confirmation_required": state == "awaiting_confirmation",
        "confirmation_id": row.get("confirmation_id"),
        "native_execute": native and state == "authorized",
        "method": row.get("method"),
        "risk_class": row.get("risk_class"),
        "verification_quality": getattr(cap, "verification", None) or row.get("verification_quality"),
        "card": launch["card"],
        "launch_url": launch.get("launch_url"),
        "open_url": launch.get("open_url"),
        "receipt": store.public_row(row),
        "broker_version": NATIVE_BROKER_VERSION,
        "must_continue": True,
        "completion_claim_allowed": False,
    }


def enrich_normalized(
    operation: str,
    args: dict[str, Any],
    handshake: dict[str, Any],
    transcript: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    svc = _svc()
    if operation == "open_app":
        query = str(args.get("app_id") or args.get("title") or args.get("text") or args.get("name") or "").strip()
        entry = app_registry.resolve_app(query)
        if entry is None or not app_registry.launch_url_for(entry):
            return None, _fail(operation, "APP_UNSUPPORTED")
        return {
            "app_id": entry.app_id,
            "display_name": entry.display_name,
            "url": app_registry.launch_url_for(entry),
            "launch_strategy": entry.source,
            "app_query": query,
        }, None
    if operation == "current_location":
        return {"precision": "when_in_use"}, None
    if operation == "haptic":
        return {"event": str(args.get("event") or "action_success")[:40]}, None
    if operation == "direct_message":
        query = str(args.get("contact_query") or args.get("contact") or "").strip()[:80]
        message = str(args.get("message") or args.get("text") or "").strip()[:500]
        if not query or not message:
            return None, _fail(
                operation,
                "AMBIGUOUS",
                extra={"clarify": "Who should I send that to, and what should it say?"},
            )
        return {"contact_query": query, "message": message}, None
    if operation == "schedule_notification":
        title = str(args.get("title") or args.get("text") or "Evie").strip()[:200]
        when_iso = svc.parse_when_iso(args, handshake=handshake, transcript=transcript)
        if not when_iso:
            seconds = svc.parse_duration_seconds(args, transcript)
            if seconds:
                when_iso = (datetime.now(svc._tz(handshake)) + timedelta(seconds=seconds)).isoformat()
        if not when_iso:
            return None, _fail(operation, "AMBIGUOUS", extra={"clarify": "When should I notify you?"})
        return {"title": title, "when_iso": when_iso, "text": str(args.get("text") or title)[:400]}, None
    return None, _fail(operation, "ACTION_UNAVAILABLE")


def enrich_run(run: dict[str, Any], operation: str, args: dict[str, Any]) -> dict[str, Any]:
    if operation == "open_app":
        run["kind"] = "open_app"
        run["app_id"] = args.get("app_id")
        run["display_name"] = args.get("display_name")
        run["url"] = args.get("url")
        run["launch_strategy"] = args.get("launch_strategy")
    elif operation == "current_location":
        run["kind"] = "location"
    elif operation == "haptic":
        run["kind"] = "haptic"
        run["event"] = args.get("event") or "action_success"
    elif operation == "direct_message":
        run["kind"] = "direct_message"
        run["contact_query"] = args.get("contact_query")
        run["message"] = args.get("message")
        run["can_send_directly"] = True
    elif operation == "schedule_notification":
        run["kind"] = "notification"
        run["title"] = args.get("title") or "Evie"
        run["when_iso"] = args.get("when_iso")
    return run


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
    svc = _svc()
    if not _actions_enabled():
        return _fail(str(arguments.get("operation") or "unknown"), "NATIVE_ACTIONS_DISABLED")
    operation = str(arguments.get("operation") or "").strip()
    if reg.is_blocked(operation) or not operation or operation == "run_shortcut":
        return _fail(operation or "unknown", "HIGH_RISK" if operation else "ACTION_UNAVAILABLE")
    cap = reg.get_capability(operation)
    if cap is None:
        return _fail(operation, "ACTION_UNAVAILABLE")
    if is_negated(transcript) and operation in {
        "call_contact",
        "facetime_contact",
        "message_contact",
        "direct_message",
    } and not wants_draft(transcript):
        return _fail(operation, "CANCELLED")

    local, remote_err = svc._target_is_local(
        str(arguments.get("target_device") or "this_phone"),
        device_id=device_id,
        role=role,
    )
    if not local:
        return _fail(operation, remote_err or "REMOTE_PHONE_UNSUPPORTED")

    handshake = store.handshake_of(device_id)
    normalized, err = svc._normalize(operation, arguments, handshake=handshake, transcript=transcript)
    if err and err.get("failure") == "ACTION_UNAVAILABLE":
        normalized, err = enrich_normalized(operation, arguments, handshake, transcript)
    if err:
        return err
    assert normalized is not None

    decision = route(
        operation,
        handshake=handshake,
        has_explicit_number=bool(normalized.get("phone_number")),
        has_url=bool(normalized.get("url")),
    )
    if decision["method"] == "unsupported":
        return _fail(operation, str(decision.get("reason") or "ACTION_UNAVAILABLE"))

    confirm_id = str(arguments.get("confirm_action_id") or "").strip()
    if confirm_id:
        existing = store.get_action(confirm_id)
        if existing and existing.get("device_id") == device_id and existing.get("state") in {
            "awaiting_confirmation",
            "draft",
        }:
            return confirm_action(action_id=confirm_id, device_id=device_id, origin=origin)
        return _fail(operation, "EXPIRED")

    policy = cap.confirmation_policy
    draft = wants_draft(transcript) and operation in {"message_contact", "direct_message"}
    if draft:
        state = "draft"
    elif policy == "voice" and not confirm:
        state = "awaiting_confirmation"
    else:
        state = "authorized"
    ttl = confirmation_ttl_s(cap.risk_class) if state in {"awaiting_confirmation", "draft"} else ACTION_TTL_S
    row = store.create_action(
        {
            "device_id": device_id,
            "instance_id": (instance_id or "")[:64],
            "session_id": session_id or "",
            "operation": operation,
            "class_level": cap.class_level,
            "risk_class": cap.risk_class,
            "confirmation_policy": policy,
            "method": decision["method"],
            "verification_quality": cap.verification,
            "normalized": normalized,
            "state": state,
            "requires_user_interaction": (not (decision["method"] == "native_broker" and state == "authorized"))
            or cap.system_confirmation_required,
            "device_label": device_label,
            "ttl_s": ttl,
            "origin": origin.rstrip("/"),
            "freeze_hash": freeze_hash(normalized),
            "confirmation_id": "cf_" + str(time.time_ns()) if state in {"awaiting_confirmation", "draft"} else None,
            "owner_turn_id": (session_id or "")[:80],
            "broker_version": handshake.get("broker_version") or NATIVE_BROKER_VERSION,
        }
    )
    if state == "authorized":
        run = enrich_run(svc._authorized_run(row, origin=origin.rstrip("/")), operation, normalized)
        row = store.update_action(row["action_id"], authorized_run=run) or row
        launch = svc._launch_payload(row, origin=origin.rstrip("/"))
        if decision["method"] == "native_broker":
            launch["launch_url"] = None
            launch["card"] = svc._card(row, launch_url=None, open_url=launch.get("open_url"))
    else:
        launch = {
            "launch_url": None,
            "open_url": None,
            "run": None,
            "card": svc._card(row, launch_url=None, open_url=None),
        }
    return _public_action_result(row, launch, cap=cap)


def confirm_action(*, action_id: str, device_id: str, origin: str) -> dict[str, Any]:
    svc = _svc()
    row = store.get_action(action_id)
    if row is None or row.get("device_id") != device_id:
        return _fail("unknown", "EXPIRED")
    if float(row.get("exp") or 0) < time.time():
        store.update_action(action_id, state="expired", failure="EXPIRED")
        return _fail(str(row.get("operation")), "EXPIRED")
    if row.get("state") not in {"awaiting_confirmation", "authorized", "draft"}:
        if row.get("state") == "executed":
            return {
                "ok": True,
                "executed": True,
                "verified": bool((row.get("receipt") or {}).get("verified")),
                "spoken": svc._spoken(row),
                "receipt": store.public_row(row),
                "action_id": action_id,
            }
        return _fail(str(row.get("operation")), str(row.get("failure") or "EXPIRED"))
    run = enrich_run(
        svc._authorized_run(row, origin=origin.rstrip("/")),
        str(row.get("operation") or ""),
        dict(row.get("normalized") or {}),
    )
    row = store.update_action(
        action_id,
        state="authorized",
        authorized_run=run,
        confirmed_at=time.time(),
        confirmation_source="voice_or_touch",
    ) or row
    launch = svc._launch_payload(row, origin=origin.rstrip("/"))
    if row.get("method") == "native_broker":
        launch["launch_url"] = None
        launch["card"] = svc._card(row, launch_url=None, open_url=launch.get("open_url"))
    return _public_action_result(row, launch, cap=reg.get_capability(str(row.get("operation") or "")))


def apply_confirmation_utterance(
    *,
    device_id: str,
    origin: str,
    text: str,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    svc = _svc()
    row = store.pending_confirmation(device_id)
    if row is None:
        return None
    kind = classify_utterance(text)
    if kind == "unrelated":
        return None
    if float(row.get("exp") or 0) < time.time():
        store.update_action(str(row["action_id"]), state="expired", failure="EXPIRED")
        return _fail(str(row.get("operation")), "EXPIRED")
    if kind == "query":
        spoken = pending_query_spoken(text, row.get("normalized") if isinstance(row.get("normalized"), dict) else {})
        return {
            "ok": True,
            "accepted": True,
            "executed": False,
            "confirmation_required": True,
            "query": True,
            "action_id": row["action_id"],
            "spoken": spoken or "I still have that action waiting.",
            "card": svc._card(row, launch_url=None, open_url=None),
            "receipt": store.public_row(row),
        }
    if kind == "no":
        return svc.cancel_action(action_id=str(row["action_id"]), device_id=device_id)
    if kind == "mutate":
        args = dict(row.get("normalized") or {})
        seconds = mutate_duration_seconds(text)
        if seconds and str(row.get("operation")) in {"create_timer", "create_reminder", "create_alarm"}:
            args["duration_seconds"] = seconds
        elif seconds and str(row.get("operation")) in {"message_contact", "direct_message"}:
            mins = max(1, seconds // 60)
            words = {
                10: "ten",
                15: "fifteen",
                20: "twenty",
                30: "thirty",
                45: "forty-five",
            }
            label = words.get(mins, str(mins))
            args["message"] = f"I'll be {label} minutes late."
        else:
            return {
                "ok": True,
                "accepted": True,
                "executed": False,
                "confirmation_required": True,
                "action_id": row["action_id"],
                "spoken": svc._spoken(row, pending=True),
                "card": svc._card(row, launch_url=None, open_url=None),
            }
        updated = store.update_action(
            str(row["action_id"]),
            normalized=args,
            freeze_hash=freeze_hash(args),
            state="awaiting_confirmation",
            confirmation_id="cf_" + str(time.time_ns()),
        ) or row
        return {
            "ok": True,
            "accepted": True,
            "executed": False,
            "confirmation_required": True,
            "mutated": True,
            "action_id": updated["action_id"],
            "spoken": svc._spoken(updated, pending=True),
            "card": svc._card(updated, launch_url=None, open_url=None),
            "receipt": store.public_row(updated),
        }
    if kind == "yes":
        return confirm_action(action_id=str(row["action_id"]), device_id=device_id, origin=origin)
    return None


def native_execute_action(*, action_id: str, device_id: str) -> dict[str, Any]:
    row = store.get_action(action_id)
    if row is None or str(row.get("device_id")) != str(device_id):
        return {"ok": False, "error": "INVALID_TOKEN", "run": {"kind": "noop"}}
    if float(row.get("exp") or 0) < time.time():
        store.update_action(action_id, state="expired", failure="EXPIRED")
        return {"ok": False, "error": "EXPIRED", "run": {"kind": "noop"}}
    if row.get("state") in {"cancelled", "expired"}:
        return {"ok": False, "error": str(row.get("state") or "EXPIRED").upper(), "run": {"kind": "noop"}}
    if row.get("state") == "awaiting_confirmation":
        return {"ok": False, "error": "CONFIRMATION_REQUIRED", "run": {"kind": "noop"}}
    if row.get("state") == "draft":
        return {"ok": False, "error": "DRAFT", "run": {"kind": "noop"}}
    if row.get("state") in {"executed", "failed"} or row.get("claimed"):
        return {"ok": False, "error": "REPLAY", "run": {"kind": "noop"}}
    if row.get("method") != "native_broker":
        return {"ok": False, "error": "WRONG_METHOD", "run": {"kind": "noop"}}
    if row.get("state") not in {"authorized", "resolved", "executing"}:
        return {"ok": False, "error": "INVALID_TOKEN", "run": {"kind": "noop"}}
    svc = _svc()
    origin = str(row.get("origin") or "").rstrip("/")
    run = row.get("authorized_run") if isinstance(row.get("authorized_run"), dict) else svc._authorized_run(row, origin=origin)
    run = enrich_run(run, str(row.get("operation") or ""), dict(row.get("normalized") or {}))
    store.update_action(action_id, state="executing", resolved_at=time.time(), authorized_run=run, claimed=True)
    store.consume_action_token(str(row.get("action_token") or ""))
    return {"ok": True, "action_id": action_id, "run": dict(run), "receipt": store.public_row(store.get_action(action_id) or row)}


def status_snapshot(*, device_id: str, role: str, display_name: str) -> dict[str, Any]:
    handshake = store.handshake_of(device_id)
    native = bool(handshake.get("native_shell"))
    reported = set(handshake.get("capabilities") or [])
    rows = []
    for name in (*reg.CORE_V1_OPERATIONS, *sorted(reg.HANDSHAKE_ONLY_OPERATIONS)):
        cap = reg.get_capability(name)
        if cap is None:
            continue
        if name in reg.HANDSHAKE_ONLY_OPERATIONS and name not in reported:
            rows.append(reg.public_capability_row(cap, available=False, reason="not reported by this iPhone"))
            continue
        if cap.needs_native and not native:
            available = name in {
                "call_contact",
                "message_contact",
                "facetime_contact",
                "open_app",
                "start_directions",
                "open_maps",
                "share_content",
                "copy_to_clipboard",
            }
            reason = None
            if name in {"call_contact", "message_contact", "facetime_contact"}:
                reason = "names need the Evie iPhone app; a typed number can still open Phone/Messages"
            elif not available:
                reason = "Needs the Evie iPhone app"
            rows.append(reg.public_capability_row(cap, available=available, reason=reason))
            continue
        if native and reported and name not in reported and cap.needs_native:
            rows.append(reg.public_capability_row(cap, available=False, reason="not on this iPhone"))
            continue
        rows.append(reg.public_capability_row(cap, available=True, reason=None))
    return {
        "bridge_name": None,
        "bridge_installed": False,
        "legacy_shortcuts": False,
        "protocol": handshake.get("protocol") or 1,
        "compatible": handshake.get("compatible", True) if handshake else True,
        "this_device": display_name or role,
        "role": role,
        "timezone": handshake.get("timezone") or None,
        "locale": handshake.get("locale") or None,
        "capability_hash": store.capability_hash(handshake),
        "last_action": store.last_for_device(device_id),
        "callback_transport": "gateway_https",
        "native_shell": "INTEGRATED" if native else "SCAFFOLDED",
        "native_shell_connected": native,
        "broker_version": handshake.get("broker_version") or NATIVE_BROKER_VERSION,
        "os_version": handshake.get("os_version") or None,
        "permissions": handshake.get("permissions") or {},
        "native_actions_enabled": _actions_enabled(),
        "remote_unattended": False,
        "voice_backend": "pwa_golden",
        "capabilities": rows,
    }


_TEXT_TIMER = re.compile(r"\b(?:set|start|make)\s+(?:a\s+)?timer\b", re.I)
_TEXT_REMIND = re.compile(r"\bremind(?:er)?\s+me\b", re.I)
_TEXT_CALL = re.compile(r"\b(?:call|facetime)\s+([A-Za-z][A-Za-z0-9'+\- ]{1,40})\s*$", re.I)
_TEXT_MESSAGE = re.compile(
    r"\b(?:message|text)\s+([A-Za-z][A-Za-z0-9'+\- ]{1,40})\s+(?:that|saying|and say)\s+(.+)$",
    re.I,
)
_TEXT_MAPS = re.compile(
    r"\b(?:(?:give me |get )?directions? to|navigate to|take me to|open maps(?: to)?)\s+(.+)$",
    re.I,
)
_TEXT_OPEN = re.compile(r"\bopen\s+([A-Za-z0-9 .+\-]{2,40})\s*$", re.I)
_TEXT_WHERE = re.compile(r"\bwhere am i\b", re.I)


def infer_from_text(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if is_negated(raw) and not wants_draft(raw):
        return None
    if _TEXT_TIMER.search(raw):
        return {"operation": "create_timer"}
    if _TEXT_REMIND.search(raw):
        title = re.sub(r"^\s*remind(?:er)?\s+me\s+(?:to\s+)?", "", raw, flags=re.I).strip()[:200]
        return {"operation": "create_reminder", "title": title or "Reminder", "text": raw}
    if _TEXT_WHERE.search(raw):
        return {"operation": "current_location"}
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
    opened = _TEXT_OPEN.search(raw)
    if opened and not re.search(r"\bopen maps\b", raw, re.I):
        return {"operation": "open_app", "app_id": opened.group(1).strip()}
    return None
