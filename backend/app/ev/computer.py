"""Mac computer-control tools: resolve, act, observe, verify.

Live voice talks to EV.app through the existing websocket. Typed chat can
still open/close via EVLifeHelper. This is not a shell and not a second POL.
"""

from __future__ import annotations

import re
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.actuator import evidence_base
from app.ev.apps import (
    PROTECTED_QUIT,
    display_app_name,
    find_macos_life_integration,
    helper_path_for,
    parse_owner_url,
    resolve_app,
)
from app.ev.computer_runtime import (
    COMPUTER_AX_TOOLS,
    COMPUTER_SEMANTIC_TOOLS,
    COMPUTER_VISION_TOOLS,
    action_signature,
    cancel_computer_task,
    decode_screen_jpeg,
    ensure_state,
    guard_loop,
    log_computer,
    note_goal,
    remember_frame,
    remember_snapshot,
    stamp_computer_receipt,
    stash_screen_observation,
    state_for,
    validate_element_ref,
    validate_frame_click,
)
from app.ev.computer_strategy import (
    adapter_for,
    control_for_app,
    normalize_app_action,
)
from app.integrations.life_helper import (
    LifeHelperError,
    LifeHelperUnavailableError,
    LifePermissionDeniedError,
    run_life_helper,
)
from app.utils.text import utcnow

HIGH_RISK_ACTIONS = frozenset({"force_quit", "force-quit"})
HIGH_RISK_LABELS = (
    "send",
    "delete",
    "purchase",
    "buy",
    "empty trash",
    "don't save",
    "dont save",
    "don’t save",
    "erase",
    "remove account",
)
TOOL_COMMANDS = {
    "computer_status": "status",
    "list_apps": "list_apps",
    "open_app": "open_app",
    "activate_app": "activate_app",
    "close_app": "close_app",
    "inspect_ui": "inspect_ui",
    "ui_action": "ui_action",
    "screen_look": "screen_look",
    "app_action": "app_action",
}


def _goal_text(state) -> str:
    goal = getattr(state, "goal", None) if state is not None else None
    return str(getattr(goal, "owner_request", "") or "")


def _wants_open_first_result(state) -> bool:
    goal = getattr(state, "goal", None) if state is not None else None
    if goal is not None and getattr(goal, "find_only", False):
        return False
    lower = _goal_text(state).lower()
    if re.search(r"don't open|do not open|just tell|don't click|do not click", lower):
        return False
    return bool(
        re.search(
            r"first result|top result|open the first|click the (first|top)|open (the )?first",
            lower,
        )
    )


def _notes_body_from_goal(text: str) -> str:
    for pat in (
        r"containing:\s*(.+)$",
        r"write:?\s*(.+)$",
        r"jot this down[^:]*:\s*(.+)$",
        r"type\s+(.+)$",
    ):
        match = re.search(pat, text, re.I | re.S)
        if match:
            return match.group(1).strip().strip(" \"“”")
    return ""


def _calculator_expression(text: str) -> str:
    match = re.search(r"(\d+)\s*(?:times|x|×|\*)\s*(\d+)", text, re.I)
    if match:
        return f"{match.group(1)}*{match.group(2)}"
    match = re.search(r"(\d+)\s*([+\-÷/])\s*(\d+)", text)
    if match:
        op = match.group(2).replace("÷", "/")
        return f"{match.group(1)}{op}{match.group(3)}"
    return ""


def _unavailable(reason: str, *, spoken: str | None = None, next_step: str | None = None) -> dict:
    return {
        "ok": False,
        "degraded": True,
        "reason": reason,
        "error": reason,
        "next_step": next_step or reason,
        "spoken": spoken or f"I couldn't do that yet. {reason}",
    }


def _ok(*, spoken: str, source: str, **payload: object) -> dict:
    now = utcnow()
    return {
        "ok": True,
        "spoken": spoken,
        "evidence": evidence_base(source=source, accepted=True, observed=True, now=now),
        **payload,
    }


def _live(live_session_id: str | None, device_id: str | None):
    from app.voice.live.layer import live_for_device, live_for_session

    return live_for_session(live_session_id) or live_for_device(device_id)


def classify_ui_risk(arguments: dict[str, Any], element: dict[str, Any] | None = None) -> str:
    action = str(arguments.get("action") or "").strip().lower()
    if action in HIGH_RISK_ACTIONS or bool(arguments.get("force")):
        return "high"
    hay = " ".join(
        str(part or "")
        for part in (
            action,
            arguments.get("value"),
            (element or {}).get("title"),
            (element or {}).get("label"),
            (element or {}).get("role"),
        )
    ).lower()
    if any(token in hay for token in HIGH_RISK_LABELS):
        return "high"
    return "low"


async def handle_computer_tool(
    session: AsyncSession,
    name: str,
    args: dict,
    *,
    actor: str,
    live_session_id: str | None = None,
    device_id: str | None = None,
    request_id: str | None = None,
) -> dict:
    del actor
    arguments = dict(args or {})
    live = _live(live_session_id, str(device_id) if device_id else None)
    state = ensure_state(getattr(live, "session_id", None) or live_session_id)
    if name == "computer_status":
        status = await computer_status(session, live=live, state=state)
        return stamp_computer_receipt(
            status, state, name=name, executed=True, request_id=request_id
        )
    note_goal(state, str(arguments.get("goal") or arguments.get("context") or "") or None)
    blocked = guard_loop(state, name, arguments)
    if blocked is not None:
        return stamp_computer_receipt(
            blocked, state, name=name, executed=False, verified=False, request_id=request_id
        )
    started = time.monotonic()
    if name == "app_action":
        if live is None:
            missing = _unavailable(
                "EV.app is not connected for Mac app actions",
                spoken="I need the EV app live to control Music and other apps.",
            )
            missing = stamp_computer_receipt(
                missing, state, name=name, executed=False, request_id=request_id
            )
            _record(state, name, arguments, missing, started, request_id=request_id)
            return missing
        if state is not None and state.goal is not None:
            if not arguments.get("playlist") and state.goal.playlist:
                arguments["playlist"] = state.goal.playlist
            if arguments.get("index") is None and state.goal.ordinal is not None:
                arguments["index"] = state.goal.ordinal
            if not arguments.get("app") and state.goal.target_apps:
                arguments["app"] = state.goal.target_apps[0]
            if state.goal.find_only:
                action = normalize_app_action(arguments.get("action"))
                arguments["action"] = action
                if action in {"play", "play_track", "play_playlist", "play_playlist_track"}:
                    blocked_play = {
                        "ok": False,
                        "executed": False,
                        "verified": False,
                        "error": "find_only",
                        "spoken": "You asked me to find it, not play it.",
                    }
                    blocked_play = stamp_computer_receipt(
                        blocked_play, state, name=name, executed=False, request_id=request_id
                    )
                    _record(state, name, arguments, blocked_play, started, request_id=request_id)
                    return blocked_play
                if not arguments.get("action"):
                    arguments["action"] = "find_playlist"
        if arguments.get("action"):
            arguments["action"] = normalize_app_action(arguments.get("action"))
        app_name = str(arguments.get("app") or "").lower()
        action = str(arguments.get("action") or "")
        if "notes" in app_name and action in {"create", "append"}:
            if not arguments.get("value") and not arguments.get("query") and not arguments.get("text"):
                body = _notes_body_from_goal(_goal_text(state))
                if body:
                    arguments["value"] = body
        if "calculator" in app_name and action not in {"status", "read"}:
            if not arguments.get("query") and not arguments.get("value"):
                expr = _calculator_expression(_goal_text(state))
                if expr:
                    arguments["query"] = expr
                    arguments["action"] = "search"
        result = await _live_command(
            live,
            "app_action",
            arguments,
            request_id=request_id,
            timeout=22.0,
        )
        shaped = _shape_app_action(arguments, result, state=state)
        if (
            str(arguments.get("action") or "") == "search"
            and "safari" in str(arguments.get("app") or "").lower()
            and _wants_open_first_result(state)
            and state is not None
            and state.goal is not None
            and not state.goal.find_only
            and shaped.get("verified") is not True
        ):
            nav_args = {"app": arguments.get("app") or "Safari", "action": "navigate"}
            nav = await _live_command(
                live,
                "app_action",
                nav_args,
                request_id=f"{request_id}-nav" if request_id else None,
                timeout=32.0,
            )
            shaped = _shape_app_action(nav_args, nav, state=state)
        shaped = stamp_computer_receipt(
            shaped,
            state,
            name=name,
            executed=bool(shaped.get("executed")),
            request_id=request_id,
        )
        _record(state, name, arguments, shaped, started, request_id=request_id)
        return shaped
    if name in {"open_app", "activate_app", "close_app", "list_apps"} and live is not None:
        result = await _live_command(
            live,
            TOOL_COMMANDS[name],
            arguments,
            request_id=request_id,
            timeout=18.0,
        )
        shaped = _shape_lifecycle(name, arguments, result, source="mac_control")
        shaped = stamp_computer_receipt(
            shaped, state, name=name, executed=bool(shaped.get("ok")), request_id=request_id
        )
        expr = _calculator_expression(_goal_text(state))
        opened_name = str(
            arguments.get("name") or shaped.get("app") or shaped.get("name") or ""
        ).lower()
        if (
            name == "open_app"
            and expr
            and "calculator" in opened_name
            and state is not None
            and state.goal is not None
            and not state.goal.lifecycle_only
        ):
            calc_args = {"app": "Calculator", "action": "search", "query": expr}
            calc = await _live_command(
                live,
                "app_action",
                calc_args,
                request_id=f"{request_id}-calc" if request_id else None,
                timeout=12.0,
            )
            shaped = _shape_app_action(calc_args, calc, state=state)
            shaped = stamp_computer_receipt(
                shaped,
                state,
                name="app_action",
                executed=bool(shaped.get("executed")),
                request_id=request_id,
            )
        _record(state, name, arguments, shaped, started, request_id=request_id)
        return shaped
    if name in {"open_app", "close_app", "list_apps"}:
        fallback = await _helper_lifecycle(session, name, arguments)
        fallback = stamp_computer_receipt(
            fallback, state, name=name, executed=bool(fallback.get("ok")), request_id=request_id
        )
        _record(state, name, arguments, fallback, started, request_id=request_id)
        return fallback
    if name == "activate_app":
        opened = await _helper_lifecycle(session, "open_app", arguments)
        opened = stamp_computer_receipt(
            opened, state, name=name, executed=bool(opened.get("ok")), request_id=request_id
        )
        _record(state, name, arguments, opened, started, request_id=request_id)
        return opened
    if name in COMPUTER_AX_TOOLS or name in COMPUTER_VISION_TOOLS or name in COMPUTER_SEMANTIC_TOOLS:
        if live is None:
            missing = _unavailable(
                "EV.app is not connected for Mac UI control",
                spoken="I can open and close apps, but I need the EV app live to operate the UI.",
            )
            missing = stamp_computer_receipt(
                missing, state, name=name, executed=False, request_id=request_id
            )
            _record(state, name, arguments, missing, started, request_id=request_id)
            return missing
        if name == "ui_action":
            action = str(arguments.get("action") or "").strip().lower()
            if action in {"click_at", "screen_click"}:
                stale = validate_frame_click(
                    state,
                    frame_id=str(arguments.get("frame_id") or ""),
                    bundle_id=str(arguments.get("bundle_id") or "") or None,
                )
                if stale is not None:
                    stale = stamp_computer_receipt(
                        stale, state, name=name, executed=False, request_id=request_id
                    )
                    _record(state, name, arguments, stale, started, request_id=request_id)
                    return stale
            elif arguments.get("element_ref"):
                stale = validate_element_ref(state, str(arguments.get("element_ref")))
                if stale is not None:
                    stale = stamp_computer_receipt(
                        stale, state, name=name, executed=False, request_id=request_id
                    )
                    _record(state, name, arguments, stale, started, request_id=request_id)
                    return stale
            if classify_ui_risk(arguments, state.elements.get(str(arguments.get("element_ref") or ""))) == "high":
                # Explicit owner command still proceeds; record the risk in the result.
                arguments = {**arguments, "risk": "high"}
        result = await _live_command(
            live,
            TOOL_COMMANDS[name],
            arguments,
            request_id=request_id,
            timeout=14.0 if name != "screen_look" else 10.0,
        )
        if name == "screen_look":
            shaped = _shape_screen(result, call_id=request_id, state=state)
        else:
            shaped = _shape_ui(name, arguments, result, state=state, call_id=request_id)
        query_miss = (
            name == "inspect_ui"
            and arguments.get("query")
            and not (shaped.get("elements") or shaped.get("compact"))
        )
        if query_miss:
            shaped.setdefault(
                "next_hint",
                "Target not in this snapshot. Try app_action for Music, scroll, or screen_look.",
            )
        shaped = stamp_computer_receipt(
            shaped, state, name=name, executed=bool(shaped.get("ok")), request_id=request_id
        )
        _record(state, name, arguments, shaped, started, request_id=request_id)
        return shaped
    return stamp_computer_receipt(
        _unavailable(f"unknown computer tool {name}"),
        state,
        name=name,
        executed=False,
        request_id=request_id,
    )


async def computer_status(session: AsyncSession, *, live, state) -> dict:
    helper = await find_macos_life_integration(session)
    helper_path = helper_path_for(helper)
    payload: dict[str, Any] = {
        "ok": True,
        "mac_client_connected": live is not None,
        "helper_ready": bool(helper_path),
        "spoken": "Mac control is connected." if live is not None else "Mac UI control is not connected.",
    }
    if live is not None and hasattr(live, "computer_readiness"):
        ready = live.computer_readiness()
        payload.update(ready.as_dict())
        if ready.generic_ui_control_ready:
            payload["spoken"] = "I can operate apps on this Mac."
        elif ready.app_lifecycle_ready and ready.accessibility_permission == "denied":
            payload["spoken"] = "I can open and close apps, but Mac control needs Accessibility permission enabled for EV."
        elif ready.app_lifecycle_ready:
            payload["spoken"] = "I can open and close apps. Generic UI control is not fully ready yet."
        elif ready.mac_client_connected:
            payload["spoken"] = "EV is connected, but Mac control is not ready."
    if state is not None:
        payload["working"] = state.working_context()
        if state.cancelled:
            payload["spoken"] = "The last computer task was stopped."
    payload["ok"] = True
    payload["evidence"] = evidence_base(
        source="mac_control",
        accepted=True,
        observed=True,
        now=utcnow(),
    )
    log_computer("computer.state_observed", extra={"connected": payload.get("mac_client_connected")})
    return payload


async def _live_command(live, command: str, arguments: dict, *, request_id: str | None, timeout: float) -> dict:
    log_computer(
        "computer.action_requested",
        extra={"command": command, "request_id": request_id, "signature": action_signature(command, arguments)},
    )
    try:
        return await live.request_computer(
            command,
            arguments,
            timeout=timeout,
            request_id=request_id,
        )
    except Exception as exc:  # noqa: BLE001 - native failure must not kill audio
        log_computer("computer.action_failed", extra={"command": command, "error": type(exc).__name__})
        return {
            "ok": False,
            "error": "computer_bridge_failed",
            "reason": type(exc).__name__,
            "spoken": "The Mac control action failed.",
        }


async def _helper_lifecycle(session: AsyncSession, name: str, arguments: dict) -> dict:
    from app.ev.apps import close_app, open_app

    if name == "list_apps":
        return await list_apps_via_helper(session, arguments)
    if name == "close_app":
        return await close_app(session, arguments, actor="master")
    return await open_app(session, arguments, actor="master")


async def list_apps_via_helper(session: AsyncSession, arguments: dict) -> dict:
    row = await find_macos_life_integration(session)
    path = helper_path_for(row)
    if row is None or not path:
        return _unavailable("no Mac helper is installed")
    query = str(arguments.get("query") or arguments.get("name") or "").strip()
    running_only = bool(arguments.get("running_only") or arguments.get("running"))
    try:
        result = await run_life_helper(
            "apps.list",
            {"query": query, "running": "true" if running_only else "false"},
            helper_path=path,
        )
    except LifeHelperUnavailableError as exc:
        return _unavailable("app list helper is unavailable", next_step=str(exc))
    except LifePermissionDeniedError as exc:
        return _unavailable("app list permission denied", next_step=str(exc))
    except LifeHelperError as exc:
        return _unavailable("app list helper failed", next_step=str(exc))
    apps = result.data.get("apps") if isinstance(result.data, dict) else []
    if not isinstance(apps, list):
        apps = []
    return _ok(
        spoken=f"I found {len(apps)} apps." if apps else "I didn't find a matching app.",
        source="macos_life",
        apps=apps[:40],
        count=len(apps),
    )


def _shape_lifecycle(name: str, arguments: dict, result: dict, *, source: str) -> dict:
    body = dict(result or {})
    ok = body.get("ok") is True
    app_name = str(body.get("name") or body.get("app") or arguments.get("name") or "")
    display = display_app_name(app_name) if app_name else "the app"
    if name == "list_apps":
        apps = body.get("apps") if isinstance(body.get("apps"), list) else []
        spoken = body.get("spoken") or (f"I found {len(apps)} apps." if ok else "I couldn't list apps.")
        return {
            **body,
            "ok": ok,
            "spoken": spoken,
            "apps": apps[:40],
            "count": body.get("count", len(apps)),
            "evidence": evidence_base(source=source, accepted=ok, observed=ok, now=utcnow()),
        }
    if name == "close_app":
        protected = str(body.get("bundle_id") or "").lower() in PROTECTED_QUIT
        if protected:
            return {
                "ok": False,
                "error": "protected",
                "spoken": f"I won't quit {display}.",
            }
        spoken = body.get("spoken")
        if not spoken:
            if body.get("already_closed"):
                spoken = f"{display} wasn't open."
            else:
                spoken = f"Closed {display}." if ok else f"I couldn't close {display}."
        return {
            **body,
            "ok": ok,
            "closed": ok and body.get("closed", True),
            "spoken": spoken,
            "evidence": evidence_base(source=source, accepted=ok, observed=ok, now=utcnow()),
        }
    spoken = body.get("spoken")
    if not spoken:
        spoken = f"Opened {display}." if ok else f"I couldn't open {display}."
        if name == "activate_app" and ok:
            spoken = f"{display} is in front."
    shaped = {
        **body,
        "ok": ok,
        "opened": ok if name == "open_app" else body.get("opened"),
        "activated": ok if name == "activate_app" else body.get("activated"),
        "spoken": spoken,
        "verification_hint": f"{display} should be running" if ok else None,
        "evidence": evidence_base(source=source, accepted=ok, observed=ok, now=utcnow()),
        "goal_complete": False,
    }
    if name in {"open_app", "activate_app"} and ok:
        control = body.get("control") if isinstance(body.get("control"), dict) else None
        shaped["control"] = control or control_for_app(
            str(body.get("app") or body.get("name") or arguments.get("name") or ""),
            str(body.get("bundle_id") or "") or None,
        )
        if shaped["control"].get("preferred") == "semantic_adapter":
            shaped["suggested_fallbacks"] = ["app_action"]
            shaped["method"] = "semantic"
            adapter = shaped["control"].get("semantic_adapter")
            actions = ", ".join(shaped["control"].get("supported_actions") or [])
            shaped["spoken"] = (
                f"{display} is open. Preferred control is the {adapter} adapter "
                f"via app_action ({actions}). Opening it is not the full request."
            )
    return shaped


def _shape_ui(name: str, arguments: dict, result: dict, *, state, call_id: str | None = None) -> dict:
    body = dict(result or {})
    ok = body.get("ok") is True
    remember_snapshot(state, body)
    compact = str(body.get("compact") or "")
    if len(compact) > 3500:
        body["compact"] = compact[:3500] + "\n…"
    spoken = str(body.get("spoken") or "")
    if not spoken:
        if name == "inspect_ui":
            app = body.get("app") or "the app"
            spoken = f"I'm looking at {app}." if ok else "I couldn't inspect that UI."
            if body.get("dialog_present"):
                spoken = f"{app} is showing a dialog."
        else:
            target = body.get("target") or arguments.get("element_ref") or arguments.get("action")
            spoken = "Done." if ok else f"That UI action failed on {target}."
    out = {
        **body,
        "ok": ok,
        "spoken": spoken,
        "working": state.working_context() if state is not None else None,
        "evidence": evidence_base(source="mac_control", accepted=ok, observed=ok, now=utcnow()),
    }
    if name == "inspect_ui":
        elements = out.get("elements") if isinstance(out.get("elements"), list) else []
        if len(elements) > 12:
            out["elements"] = elements[:12]
        compact_live = str(out.get("compact") or "")
        if len(compact_live) > 1800:
            out["compact"] = compact_live[:1800] + "\n…"
        out.pop("walked", None)
        query = str(arguments.get("query") or body.get("query") or "").strip()
        hay = " ".join(
            [
                compact_live,
                " ".join(str(item.get("title") or "") for item in elements if isinstance(item, dict)),
            ]
        ).lower()
        target_found = bool(query) and query.lower() in hay
        if query:
            out["target_found"] = bool(body.get("target_found", target_found))
        else:
            out["target_found"] = None
        app_name = str(body.get("app") or arguments.get("app") or arguments.get("name") or "")
        adapter = adapter_for(app_name, str(body.get("bundle_id") or "") or None)
        out["semantic_adapter_available"] = adapter is not None
        out["screen_fallback_available"] = True
        out["searched_scope"] = str(body.get("window") or body.get("app") or "front_window")
        out["scrollable_regions"] = body.get("scrollable_regions") or []
        if adapter is not None:
            out.setdefault("suggested_fallbacks", ["app_action", "screen_look"])
            out["method"] = "accessibility"
            if query and not out.get("target_found"):
                out["spoken"] = (
                    f"I didn't find “{query}” in this Accessibility snapshot. "
                    f"{adapter['app']} has a semantic adapter — use app_action."
                )
        elif query and not out.get("target_found"):
            out.setdefault("suggested_fallbacks", ["inspect_ui", "screen_look"])
    if name == "ui_action":
        out.pop("elements", None)
        out.setdefault("action", arguments.get("action"))
        out.setdefault("target", body.get("target") or arguments.get("element_ref"))
        out.setdefault("active_app", body.get("app") or (state.app_name if state else None))
        out.setdefault("ui_changed", body.get("ui_changed", ok))
        out.setdefault("dialog_present", body.get("dialog_present", False))
        compact_after = str(out.get("compact") or "")
        if len(compact_after) > 1800:
            out["compact"] = compact_after[:1800] + "\n…"
        jpeg, width, height, _err = decode_screen_jpeg(body)
        if jpeg is not None:
            remember_frame(state, {**body, "width": width, "height": height})
            stash_screen_observation(
                call_id=call_id,
                request_id=str(body.get("verify_frame_id") or body.get("frame_id") or "click"),
                jpeg=jpeg,
                width=width,
                height=height,
                app_name=str(body.get("app") or "") or None,
            )
        out.pop("jpeg_b64", None)
        out.pop("jpeg", None)
        if body.get("verify_frame_id") or jpeg is not None:
            out["verify_frame_id"] = body.get("verify_frame_id") or body.get("frame_id")
    log_computer(
        "computer.action_executed" if name == "ui_action" else "computer.ui_snapshot",
        extra={
            "ok": ok,
            "action": arguments.get("action"),
            "app": body.get("app") or body.get("bundle_id"),
            "dialog": body.get("dialog_present"),
        },
    )
    return out


def _shape_screen(result: dict, *, call_id: str | None, state) -> dict:
    body = dict(result or {})
    jpeg, width, height, decode_error = decode_screen_jpeg(body)
    ok = body.get("ok") is True and jpeg is not None
    error = decode_error or body.get("error")
    if jpeg is not None:
        remember_frame(state, {**body, "width": width, "height": height})
        stash_screen_observation(
            call_id=call_id,
            request_id=str(body.get("request_id") or body.get("frame_id") or call_id or "screen"),
            jpeg=jpeg,
            width=width,
            height=height,
            app_name=str(body.get("app") or "") or None,
        )
        log_computer(
            "computer.screen_observed",
            extra={
                "frame_id": body.get("frame_id"),
                "width": width,
                "height": height,
                "encoded_bytes": len(jpeg),
                "app": body.get("app"),
            },
        )
    spoken = body.get("spoken")
    if not spoken:
        if ok:
            spoken = "A current window observation was submitted as an image in this conversation. Describe only what you can actually see."
        elif str(error or "") in {"denied", "permission_denied"}:
            spoken = "Mac screen vision needs Screen Recording permission enabled for EV."
        else:
            spoken = "I couldn't capture the window."
    compact = {
        "ok": ok,
        "spoken": spoken,
        "error": None if ok else (error or "screen_capture_failed"),
        "frame_id": body.get("frame_id"),
        "width": width,
        "height": height,
        "app": body.get("app"),
        "window": body.get("window") or body.get("window_title"),
        "bundle_id": body.get("bundle_id"),
        "encoded_bytes": len(jpeg) if jpeg else 0,
        "request_id": body.get("request_id"),
        "working": state.working_context() if state is not None else None,
        "evidence": evidence_base(source="mac_control", accepted=ok, observed=ok, now=utcnow()),
    }
    return compact


def _shape_app_action(arguments: dict, result: dict, *, state) -> dict:
    body = dict(result or {})
    error = str(body.get("error") or "")
    search_done = error in {"playlist_not_found", "not_found", "ambiguous", "ordinal_mismatch"}
    executed = body.get("executed")
    if executed is None:
        executed = body.get("ok") is True or search_done
    verified = body.get("verified") is True
    action = str(arguments.get("action") or body.get("action") or "status").lower()
    if search_done:
        ok = False
        verified = False
    elif action in {"play", "play_track", "play_playlist", "play_playlist_track"}:
        ok = verified
    else:
        ok = body.get("ok") is True
    spoken = str(body.get("spoken") or "")
    if not spoken:
        if verified:
            track = body.get("track") or "the track"
            spoken = f"Playing {track}."
        elif error == "playlist_not_found":
            spoken = f"I couldn't find a playlist named {arguments.get('playlist') or 'that'}."
        elif error == "ambiguous":
            spoken = "Which playlist do you mean?"
        else:
            spoken = "That Music action didn't complete."
    tracks = body.get("tracks") if isinstance(body.get("tracks"), list) else None
    if tracks is not None:
        body["tracks"] = tracks[:40]
    playlists = body.get("playlists") if isinstance(body.get("playlists"), list) else None
    if playlists is not None:
        body["playlists"] = playlists[:40]
    return {
        **body,
        "ok": ok,
        "executed": executed,
        "verified": verified,
        "spoken": spoken,
        "adapter": body.get("adapter") or "music",
        "method": body.get("method") or "semantic",
        "working": state.working_context() if state is not None else None,
        "evidence": evidence_base(
            source="mac_control",
            accepted=ok,
            observed=verified,
            now=utcnow(),
        ),
    }


def _record(
    state,
    name: str,
    arguments: dict,
    result: dict,
    started: float,
    request_id: str | None = None,
) -> None:
    if state is None:
        return
    state.last_result = {
        "ok": result.get("ok"),
        "executed": result.get("executed"),
        "verified": result.get("verified"),
        "error": result.get("error"),
        "app": result.get("app") or result.get("name"),
    }
    latency_ms = int((time.monotonic() - started) * 1000)
    state.traces.append(
        f"{name} ok={result.get('ok')} executed={result.get('executed')} "
        f"verified={result.get('verified')} err={result.get('error')} {latency_ms}ms"
    )
    remember_snapshot(state, result)
    extra = {
        "name": name,
        "ok": result.get("ok"),
        "executed": result.get("executed"),
        "verified": result.get("verified"),
        "latency_ms": latency_ms,
        "call_id": request_id,
    }
    if result.get("verified"):
        log_computer("computer.action_verified", extra=extra)
    elif result.get("executed"):
        log_computer("computer.action_executed", extra=extra)
    if name == "open_app" and result.get("ok"):
        log_computer(
            "computer.app_opened",
            extra={"app": result.get("name") or result.get("app")},
        )
    goal_meta = result.get("goal") if isinstance(result.get("goal"), dict) else {}
    if result.get("verified") and goal_meta.get("status") == "complete":
        log_computer(
            "computer.goal_completed",
            extra={"app": result.get("name") or result.get("app"), "goal_id": goal_meta.get("goal_id")},
        )


async def open_url_via_live_or_helper(
    session: AsyncSession,
    args: dict,
    *,
    actor: str,
    live_session_id: str | None = None,
    device_id: str | None = None,
) -> dict:
    from app.ev.apps import open_url

    url = parse_owner_url(str(args.get("url") or ""))
    if url is None:
        return {
            "ok": False,
            "error": "invalid_url",
            "spoken": "I can only open http or https links.",
        }
    live = _live(live_session_id, str(device_id) if device_id else None)
    if live is not None:
        result = await _live_command(live, "open_url", {"url": url}, request_id=None, timeout=12.0)
        if result.get("ok") is True:
            return _ok(spoken=f"Opened {url}.", source="mac_control", url=url, opened=True)
        if result.get("error") not in {"unsupported", "unknown_command"}:
            spoken = result.get("spoken") or f"I couldn't open {url}."
            return {**result, "ok": False, "spoken": spoken, "url": url}
    return await open_url(session, args, actor=actor)


def stop_computer_task(live_session_id: str | None) -> dict:
    state = cancel_computer_task(live_session_id)
    live = _live(live_session_id, None)
    if live is not None and hasattr(live, "cancel_computer_requests"):
        try:
            import asyncio

            loop = asyncio.get_running_loop()
            loop.create_task(live.cancel_computer_requests(reason="owner_stop"))
        except RuntimeError:
            pass
    return {
        "ok": True,
        "cancelled": True,
        "spoken": "Stopped.",
        "had_task": state is not None,
    }


def current_working_state(live_session_id: str | None) -> dict[str, Any]:
    state = state_for(live_session_id)
    return state.working_context() if state is not None else {}


def resolved_app_name(raw: str) -> str:
    resolved = resolve_app(raw)
    if resolved is not None:
        return resolved[0]
    return str(raw or "").strip()
