"""Execute Muse semantic tools through existing adapters. Muse never gets credentials."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.cognitive import telemetry
from app.cognitive.capabilities import public_descriptors
from app.cognitive.session_store import (
    CognitiveSession,
    bump_steering,
    remember_effect,
    save,
    status_line,
)

MUTATING = frozenset(
    {
        "goal.ensure",
        "goal.patch",
        "goal.cancel",
        "digital.act",
        "life.send",
        "files.act",
        "code.act",
        "computer.perform_effect",
    }
)


def _strip_secrets(payload: Any) -> Any:
    try:
        from app.digital.vault_bound import strip_secrets

        if isinstance(payload, dict):
            return strip_secrets(payload)
    except Exception:
        pass
    if isinstance(payload, dict):
        banned = ("token", "cookie", "password", "secret", "authorization", "api_key")
        return {
            key: _strip_secrets(value)
            for key, value in payload.items()
            if not any(part in str(key).lower() for part in banned)
        }
    if isinstance(payload, list):
        return [_strip_secrets(item) for item in payload[:40]]
    return payload


def _surface_tool_result(result: Any) -> dict[str, Any]:
    """Lift ToolCallResponse.result so spoken/ok are on the body kernel reads.

    dispatch() wraps adapter payloads. Reading only the envelope made every
    send speak 'Okay.' while the real receipt sat unused in result.spoken.
    """

    if hasattr(result, "model_dump"):
        raw = result.model_dump()
    elif isinstance(result, dict):
        raw = dict(result)
    else:
        raw = {"ok": False, "error": "bad_tool_result"}
    raw = _strip_secrets(raw) if isinstance(raw, dict) else {}
    if not isinstance(raw, dict):
        return {"ok": False, "spoken": "I couldn't finish that on this Mac."}
    nested = raw.get("result")
    if isinstance(nested, dict):
        body = dict(nested)
        if "ok" not in body:
            body["ok"] = bool(raw.get("ok"))
        if raw.get("error") and not body.get("error"):
            body["error"] = raw["error"]
        if not str(body.get("spoken") or "").strip():
            body["spoken"] = str(raw.get("spoken") or raw.get("error") or "")
        return body
    return raw


def _failure(code: str, message: str, **extra: Any) -> dict[str, Any]:
    out = {"ok": False, "error": code, "diagnosis": code, "spoken": message, **extra}
    return out


async def execute_semantic(
    session: AsyncSession,
    name: str,
    arguments: dict[str, Any],
    *,
    cognition: CognitiveSession,
    actor: str,
    live_session_id: str | None,
    steering_seen: int,
    device_id: str | None = None,
) -> dict[str, Any]:
    args = dict(arguments or {})
    if int(cognition.steering_version) != int(steering_seen) and name in MUTATING:
        telemetry.inc("stale_mutations_blocked")
        return _failure(
            "STALE_PLAN",
            "Owner changed the plan. I will not apply that stale action.",
            diagnosis="STALE_PLAN",
        )
    if cognition.prepare_only and name in {
        "files.act",
        "code.act",
        "computer.perform_effect",
        "digital.act",
        "life.send",
    }:
        if name == "digital.act" and str(args.get("operation") or "").lower() in {
            "send",
            "reply",
            "create",
            "update",
            "delete",
            "trash",
        }:
            telemetry.inc("stale_mutations_blocked")
            return _failure(
                "POLICY_BLOCKED",
                "Prepare-only: I will not send or mutate that.",
                diagnosis="POLICY_BLOCKED",
            )
        if name == "life.send":
            telemetry.inc("stale_mutations_blocked")
            return _failure(
                "POLICY_BLOCKED",
                "Prepare-only: I will not send that.",
                diagnosis="POLICY_BLOCKED",
            )
        if name in {"files.act", "code.act", "computer.perform_effect"}:
            args["prepare_only"] = True
    if name == "capability.discover":
        return {"ok": True, "capabilities": public_descriptors(domain=str(args.get("domain") or ""))}
    if name == "memory.search":
        from app.memory.select import explicit_recall_payload

        payload = await explicit_recall_payload(
            session,
            str(args.get("query") or ""),
            k=int(args.get("k") or 8),
        )
        return _strip_secrets(payload if isinstance(payload, dict) else {"ok": True, "result": payload})
    if name == "life.mail":
        query = str(args.get("query") or args.get("q") or "").strip() or "any new email"
        return await _run_existing(
            session,
            "list_mail",
            {"query": query[:400]},
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind="life.mail",
        )
    if name == "life.messages":
        query = str(args.get("query") or args.get("q") or "").strip() or "any new messages"
        return await _run_existing(
            session,
            "list_messages",
            {"query": query[:400]},
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind="life.messages",
        )
    if name == "life.send":
        return await _life_send_on_mac(
            session,
            args,
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
        )
    if name == "people.lookup":
        from app.ev.tools import dispatch

        query = str(args.get("query") or args.get("name") or "").strip()
        if not query:
            return _failure("CAPABILITY_UNAVAILABLE", "Need a person name.")
        try:
            result = await dispatch(
                session,
                "get_person",
                {"name": query},
                actor=actor,
                allow_sensitive=True,
                live_session_id=live_session_id,
            )
        except Exception as exc:
            return _failure("CAPABILITY_UNAVAILABLE", "I couldn't look that person up.", diagnosis=type(exc).__name__)
        return _strip_secrets(result.model_dump() if hasattr(result, "model_dump") else dict(result))
    if name == "goal.status":
        return {"ok": True, "session": cognition.public(), "spoken": status_line(cognition)}
    if name == "goal.ensure":
        return await _ensure_goal(session, cognition, args, actor=actor)
    if name == "goal.patch":
        return await _patch_goal(session, cognition, args)
    if name == "goal.cancel":
        return await _cancel_goal(session, cognition)
    if name == "digital.discover":
        from app.digital.fabric import answer_can_you

        return answer_can_you(str(args.get("service") or args.get("q") or ""))
    if name == "digital.act":
        hub = _mac_hub_send_payload(args)
        if hub is not None:
            return await _life_send_on_mac(
                session,
                hub,
                actor=actor,
                live_session_id=live_session_id,
                cognition=cognition,
            )
        from app.digital.tools import handle_digital_tool

        raw = await handle_digital_tool(
            session,
            "digital_act",
            {
                "service": args.get("service"),
                "operation": args.get("operation"),
                "args": args.get("args") or {},
                "confirmed": bool(args.get("confirmed")),
            },
            actor=actor,
        )
        body = _strip_secrets(raw or {})
        remember_effect(cognition, {"kind": "digital.act", **{k: body.get(k) for k in ("status", "ok", "error")}})
        telemetry.inc("background_executions")
        return body
    if name == "research.search":
        from app.ev.tools import dispatch

        result = await dispatch(
            session,
            "search_web",
            {"query": str(args.get("query") or ""), "limit": int(args.get("limit") or 5)},
            actor=actor,
            allow_sensitive=True,
            live_session_id=live_session_id,
        )
        telemetry.inc("background_executions")
        return _strip_secrets(result.model_dump() if hasattr(result, "model_dump") else dict(result))
    if name == "files.act":
        if cognition.prepare_only:
            return {
                "ok": True,
                "prepare_only": True,
                "spoken": "I will prepare the file changes without writing them.",
                "effect": str(args.get("effect") or ""),
            }
        decided = _decide_file_computer(str(args.get("effect") or ""), cognition)
        if decided.get("skip"):
            from app.cognitive.artifact import mark_artifact_complete

            return mark_artifact_complete(dict(decided.get("body") or {}), cognition)
        payload: dict[str, Any] = {"goal": str(decided.get("goal") or args.get("effect") or "")}
        if decided.get("last_path"):
            payload["last_path"] = decided["last_path"]
        return await _run_existing(
            session,
            "computer",
            payload,
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind="files.act",
        )
    if name == "code.act":
        if cognition.prepare_only:
            return {
                "ok": True,
                "prepare_only": True,
                "spoken": "I will prepare that coding change without writing files yet.",
                "files_changed": [],
                "effect": str(args.get("effect") or ""),
            }
        effect = str(args.get("effect") or "")
        return await _run_existing(
            session,
            "code",
            {"goal": effect},
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind="code.act",
        )
    if name == "computer.observe":
        return await _run_existing(
            session,
            "computer",
            {"goal": f"observe only, do not mutate: {args.get('goal') or ''}"},
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind="computer.observe",
        )
    if name == "computer.perform_effect":
        if cognition.prepare_only:
            return {
                "ok": True,
                "prepare_only": True,
                "foreground_required": False,
                "spoken": "Prepare-only: I will not drive the Mac UI.",
            }
        effect = str(args.get("effect") or "")
        decided = _decide_file_computer(effect, cognition)
        if decided.get("skip"):
            from app.cognitive.artifact import mark_artifact_complete

            return mark_artifact_complete(dict(decided.get("body") or {}), cognition)
        effect = str(decided.get("goal") or effect)
        if args.get("background_preferred", True):
            effect = f"background preferred, do not steal focus unless FOREGROUND_REQUIRED: {effect}"
        payload = {"goal": effect}
        inject_path = True
        try:
            from app.ev.computer_strategy import looks_like_app_or_web_task

            inject_path = not looks_like_app_or_web_task(str(args.get("effect") or effect))
        except Exception:
            inject_path = True
        if inject_path and decided.get("last_path"):
            payload["last_path"] = decided["last_path"]
        result = await _run_existing(
            session,
            "computer",
            payload,
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind="computer.perform_effect",
        )
        if isinstance(result, dict):
            if result.get("verified") is False:
                result["completed_verified"] = False
            if result.get("foreground_required") or result.get("activated"):
                telemetry.inc("foreground_required")
                result["diagnosis"] = result.get("diagnosis") or "FOREGROUND_REQUIRED"
        return result
    if name == "look.capture":
        tool = "screen_look" if args.get("screen") else "look"
        return await _run_existing(
            session,
            tool,
            {"prompt": str(args.get("prompt") or "what is in view"), "focus": "auto"},
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind="look.capture",
        )
    if name == "timer.act":
        return await _timer_act(
            session,
            args,
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
        )
    if name == "weather.get":
        weather_args: dict[str, Any] = {}
        if args.get("place"):
            weather_args["place"] = str(args["place"])[:80]
        if args.get("query"):
            weather_args["query"] = str(args["query"])[:200]
        return await _backed_existing(
            session,
            "get_weather",
            weather_args,
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind="weather.get",
        )
    if name == "life.state":
        op = str(args.get("op") or "").strip()
        if op not in _LIFE_STATE_OPS:
            return _failure(
                "CAPABILITY_UNAVAILABLE",
                "That life operation is not on this kernel.",
                diagnosis="CAPABILITY_UNAVAILABLE",
            )
        if cognition.prepare_only and op in _LIFE_STATE_WRITES:
            telemetry.inc("stale_mutations_blocked")
            return _failure(
                "POLICY_BLOCKED",
                "Prepare-only: I will not change that.",
                diagnosis="POLICY_BLOCKED",
            )
        raw_args = args.get("args")
        op_args = dict(raw_args) if isinstance(raw_args, dict) else {
            key: value for key, value in args.items() if key not in {"op", "args"}
        }
        return await _backed_existing(
            session,
            op,
            op_args,
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind="life.state",
        )
    if name == "notify.schedule":
        return await _notify_schedule(session, args)
    if name == "phone.call":
        return await _phone_call(session, args, cognition=cognition)
    if name == "owner.profile":
        return await _owner_profile(session, args)
    if name == "home.act":
        return await _home_station_act(
            session,
            args,
            actor=actor,
            device_id=device_id,
            cognition=cognition,
        )
    return _failure("CAPABILITY_UNAVAILABLE", f"I don't have {name} on this kernel.")


async def _owner_profile(session: AsyncSession, args: dict[str, Any]) -> dict[str, Any]:
    """The owner's own name — read it, or store what they asked to be called.

    A name is only ever written from the owner's own statement in this turn.
    Contacts, device names, and other people's names are never used as a guess:
    answering "what's my name?" with an inference would be a fabricated fact.
    """

    from app.ev.assistant import get_profile, set_owner_preferred_name

    op = str(args.get("op") or "get").strip().lower()
    if op == "set":
        name = str(args.get("name") or "").strip()
        if not name:
            return _failure("CAPABILITY_UNAVAILABLE", "I didn't catch the name to use.")
        if len(name) > 80 or "\n" in name:
            return _failure("CAPABILITY_UNAVAILABLE", "That name is too long for me to keep.")
        profile = await set_owner_preferred_name(session, name)
        stored = str(getattr(profile, "owner_preferred_name", None) or name)
        return {
            "ok": True,
            "spoken": f"I'll call you {stored}.",
            "name": stored,
            "executed": True,
            "verified": True,
            "operation": "owner.profile",
        }
    profile = await get_profile(session)
    stored = str(getattr(profile, "owner_preferred_name", None) or "").strip()
    if not stored:
        return {
            "ok": True,
            "spoken": "I don't have your preferred name saved yet. Tell me what to call you and I'll keep it.",
            "name": None,
            "executed": False,
            "verified": True,
            "operation": "owner.profile",
        }
    return {
        "ok": True,
        "spoken": f"Your name is {stored}.",
        "name": stored,
        "executed": False,
        "verified": True,
        "operation": "owner.profile",
    }


async def _home_station_act(
    session: AsyncSession,
    args: dict[str, Any],
    *,
    actor: str,
    device_id: str | None,
    cognition: CognitiveSession,
) -> dict[str, Any]:
    """Universal Home Station route: the owner's words in, one honest result out.

    This is the general fallback behind every capability the calling device has
    no local path for. It reuses the existing Home Station broker (which already
    owns recipient confirmation, send approval parking, and evidence), so it adds
    a route rather than a second engine. The effect is labelled
    ``executed_on: home_station`` so nothing is ever reported as having run on
    the device that asked.
    """

    from uuid import UUID

    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.models import Device

    request = str(args.get("request") or args.get("text") or "").strip()
    if not request:
        return _failure(
            "CAPABILITY_UNAVAILABLE",
            "I need the request in your own words.",
            executed=False,
            verified=False,
            executed_on="home_station",
        )
    if not device_id:
        return _failure(
            "CAPABILITY_UNAVAILABLE",
            "That needs a bound device before I can route it to Home Station.",
            executed=False,
            verified=False,
            executed_on="home_station",
        )
    try:
        device = await session.get(Device, UUID(str(device_id)))
    except (TypeError, ValueError):
        device = None
    if device is None or device.revoked_at is not None:
        return _failure(
            "CAPABILITY_UNAVAILABLE",
            "That device is not available right now.",
            executed=False,
            verified=False,
            executed_on="home_station",
        )
    if cognition.prepare_only:
        telemetry.inc("stale_mutations_blocked")
        return _failure(
            "POLICY_BLOCKED",
            "Prepare-only: I will not run that yet.",
            executed=False,
            verified=False,
            executed_on="home_station",
        )
    acted = await maybe_phone_mac_act(session, device=device, text=request)
    if acted is None:
        return _failure(
            "CAPABILITY_UNAVAILABLE",
            "Home Station has no route for that request yet.",
            diagnosis="HOME_STATION_NO_PATH",
            executed=False,
            verified=False,
            executed_on="home_station",
        )
    ok = bool(acted.get("ok", True))
    return {
        "ok": ok,
        "spoken": str(acted.get("reply") or ""),
        "executed": bool(acted.get("executed")),
        "verified": bool(acted.get("verified")),
        "accepted": bool(acted.get("accepted")),
        "queued": bool(acted.get("queued")),
        "executed_on": "home_station",
        "operation": str(acted.get("tool") or acted.get("operation") or "home.act"),
        "route": acted.get("route"),
        "error_code": acted.get("error_code"),
        "diagnosis": None if ok else str(acted.get("error_code") or "HOME_STATION_FAILED"),
    }


def _mac_hub_send_payload(args: dict[str, Any]) -> dict[str, Any] | None:
    """Named to+body send on this Mac. chat_ref WhatsApp Web stays on digital.act."""

    operation = str(args.get("operation") or "").strip().lower()
    if operation not in {"send", "reply"}:
        return None
    service = str(args.get("service") or "").strip().lower()
    inner = dict(args.get("args") or {}) if isinstance(args.get("args"), dict) else {}
    to = str(inner.get("to") or inner.get("name") or "").strip()
    body = str(inner.get("text") or inner.get("body") or "").strip()
    if not to or not body:
        return None
    channel = str(inner.get("channel") or "").strip().lower()
    if service == "whatsapp":
        channel = channel or "whatsapp"
    elif service in {"gmail", "mail"}:
        channel = "mail"
    elif service in {"phone", "messages", "imessage"}:
        channel = channel or "messages"
    else:
        return None
    return {"to": to, "text": body, "channel": channel}


async def _life_send_on_mac(
    session: AsyncSession,
    args: dict[str, Any],
    *,
    actor: str,
    live_session_id: str | None,
    cognition: CognitiveSession,
) -> dict[str, Any]:
    from app.ev.messaging.channels import normalize_channel
    from app.ev.send_intent import channel_from_text, parse_send_intent

    utterance = str(cognition.constraints.get("owner_utterance") or "").strip()
    parsed = parse_send_intent(utterance) if utterance else None
    to = str(args.get("to") or args.get("name") or "").strip()
    body = str(args.get("text") or args.get("body") or "").strip()
    if parsed and to.lower() in {
        "message",
        "messages",
        "whatsapp",
        "text",
        "mail",
        "email",
        "note",
    }:
        to = str(parsed.get("to") or to)
        body = str(parsed.get("text") or body)
    if parsed:
        to = to or str(parsed.get("to") or "")
        body = body or str(parsed.get("text") or "")
    if not to or not body:
        return _failure(
            "CAPABILITY_UNAVAILABLE",
            "I need who to message and what to say.",
        )
    channel = normalize_channel(args.get("channel"))
    if not channel and parsed:
        channel = normalize_channel(parsed.get("channel"))
    if not channel:
        channel = normalize_channel(channel_from_text(utterance))
    payload: dict[str, Any] = {"to": to, "text": body[:500]}
    if channel:
        payload["channel"] = channel
    tool = "send_mail" if channel in {"mail", "email"} else "send_message"
    return await _run_existing(
        session,
        tool,
        payload,
        actor=actor,
        live_session_id=live_session_id,
        cognition=cognition,
        kind="life.send",
    )


async def _run_existing(
    session: AsyncSession,
    name: str,
    arguments: dict[str, Any],
    *,
    actor: str,
    live_session_id: str | None,
    cognition: CognitiveSession,
    kind: str,
) -> dict[str, Any]:
    from app.cognitive.edge import execute_on_mac
    from app.cognitive.mode import is_kernel_process
    from app.voice.live.layer import active_lives

    lives = []
    try:
        lives = active_lives()
    except Exception:
        lives = []
    if lives or not is_kernel_process():
        from app.ev.tools import dispatch

        result = await dispatch(
            session,
            name,
            arguments,
            actor=actor,
            allow_sensitive=True,
            live_session_id=live_session_id or (str(lives[0].session_id) if lives else None),
        )
        body = _surface_tool_result(result)
    else:
        body = await execute_on_mac(name, arguments, live_session_id=live_session_id)
        body = _surface_tool_result(body)
    top: dict[str, Any] = {}
    msgs = body.get("messages") if isinstance(body, dict) else None
    if isinstance(msgs, list) and msgs and isinstance(msgs[0], dict):
        top = msgs[0]
    remember_effect(
        cognition,
        {
            "kind": kind,
            "ok": body.get("ok"),
            "verified": body.get("verified"),
            "error": body.get("error"),
            "path": body.get("path"),
            "action": body.get("action"),
            "spoken": str(body.get("spoken") or "")[:240] or None,
            "who": top.get("sender") or top.get("handle"),
            "when": top.get("when"),
            "subject": top.get("subject"),
        },
    )
    if kind in {"files.act", "computer.perform_effect"}:
        from app.cognitive.artifact import bind_written_artifact, mark_artifact_complete

        bind_written_artifact(cognition, body)
        body = mark_artifact_complete(body, cognition)
    if body.get("verified") is False and body.get("ok") is True:
        telemetry.inc("false_completions", 0)
        body["completed_verified"] = False
    telemetry.inc("background_executions")
    return body


async def _ensure_goal(
    session: AsyncSession,
    cognition: CognitiveSession,
    args: dict[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    del actor
    objective = str(args.get("objective") or "").strip()
    if not objective:
        return _failure("CAPABILITY_UNAVAILABLE", "Need an objective to keep.")
    from app.presence.service import create_contract, get_contract

    if cognition.focused_goal_id:
        existing = await get_contract(session, cognition.focused_goal_id)
        if existing is not None and str(existing.state) not in {"COMPLETED", "CANCELLED", "FAILED", "EXPIRED"}:
            cognition.semantic_objective = objective
            if args.get("prepare_only") is not None:
                cognition.prepare_only = bool(args.get("prepare_only"))
            if isinstance(args.get("constraints"), dict):
                cognition.constraints.update(args["constraints"])
            save(cognition)
            return {"ok": True, "goal_id": cognition.focused_goal_id, "reused": True}
    row = await create_contract(
        session,
        objective=objective,
        constraints={
            **(args.get("constraints") or {}),
            "cognitive": True,
            "prepare_only": bool(args.get("prepare_only")),
            "steering_version": cognition.steering_version,
        },
        activate=True,
    )
    cognition.focused_goal_id = str(row.id)
    cognition.semantic_objective = objective
    cognition.prepare_only = bool(args.get("prepare_only"))
    cognition.parked = False
    save(cognition)
    telemetry.inc("goal_contracts_created")
    return {"ok": True, "goal_id": str(row.id), "state": row.state}


async def _patch_goal(
    session: AsyncSession,
    cognition: CognitiveSession,
    args: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(args.get("constraints"), dict):
        cognition.constraints.update(args["constraints"])
    if args.get("objective"):
        cognition.semantic_objective = str(args.get("objective"))
    prepare = args.get("prepare_only")
    bump_steering(cognition, prepare_only=bool(prepare) if prepare is not None else None)
    telemetry.inc("replans")
    if cognition.focused_goal_id:
        from app.presence.service import get_contract

        row = await get_contract(session, cognition.focused_goal_id)
        if row is not None:
            constraints = dict(row.constraints or {})
            constraints.update(cognition.constraints)
            constraints["steering_version"] = cognition.steering_version
            constraints["prepare_only"] = cognition.prepare_only
            row.constraints = constraints
            if args.get("objective"):
                row.objective = str(args.get("objective"))[:2000]
            await session.flush()
    return {
        "ok": True,
        "steering_version": cognition.steering_version,
        "prepare_only": cognition.prepare_only,
        "goal_id": cognition.focused_goal_id,
    }


async def _cancel_goal(session: AsyncSession, cognition: CognitiveSession) -> dict[str, Any]:
    bump_steering(cognition)
    if cognition.focused_goal_id:
        from app.presence.contract import GoalState
        from app.presence.service import get_contract, transition

        row = await get_contract(session, cognition.focused_goal_id)
        if row is not None:
            await transition(session, row, GoalState.CANCELLED.value, reason="owner_stop")
    cognition.focused_goal_id = None
    cognition.semantic_objective = ""
    cognition.parked = False
    save(cognition)
    return {"ok": True, "cancelled": True}


def dump_tool_json(payload: dict[str, Any]) -> str:
    return json.dumps(_strip_secrets(payload), default=str)[:16_000]


def _decide_file_computer(effect: str, cognition: CognitiveSession) -> dict[str, Any]:
    from app.cognitive.artifact import decide_file_effect

    return decide_file_effect(effect, cognition)


_LIFE_STATE_OPS = frozenset(
    {
        "life_project_create",
        "life_project_update",
        "life_project_query",
        "life_goal_create",
        "life_goal_update",
        "life_goal_add_step",
        "life_goal_query",
        "life_commitment_create",
        "life_commitment_update",
        "life_commitment_query",
        "life_relationship_set",
        "mission_control",
    }
)
_LIFE_STATE_WRITES = frozenset(
    {
        "life_project_create",
        "life_project_update",
        "life_goal_create",
        "life_goal_update",
        "life_goal_add_step",
        "life_commitment_create",
        "life_commitment_update",
        "life_relationship_set",
    }
)
_PHONE_OP_MAP = {
    "call": "call_contact",
    "facetime": "facetime_contact",
    "message": "message_contact",
}


async def _backed_existing(
    session: AsyncSession,
    name: str,
    arguments: dict[str, Any],
    *,
    actor: str,
    live_session_id: str | None,
    cognition: CognitiveSession,
    kind: str,
) -> dict[str, Any]:
    """Route one mapped capability through the existing ev dispatch, fail-closed."""

    try:
        return await _run_existing(
            session,
            name,
            arguments,
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind=kind,
        )
    except Exception as exc:  # noqa: BLE001 - tool boundary, same as people.lookup
        return _failure(
            "CAPABILITY_UNAVAILABLE",
            "That capability is not available right now.",
            diagnosis=type(exc).__name__,
        )


async def _timer_act(
    session: AsyncSession,
    args: dict[str, Any],
    *,
    actor: str,
    live_session_id: str | None,
    cognition: CognitiveSession,
) -> dict[str, Any]:
    op = str(args.get("op") or "").strip().lower()
    label = str(args.get("label") or "").strip()[:500]
    timer_id = str(args.get("timer_id") or "").strip()[:64] or None
    minutes: float | None = None
    raw_seconds = args.get("seconds")
    if raw_seconds is not None and str(raw_seconds).strip():
        try:
            minutes = float(raw_seconds) / 60.0
        except (TypeError, ValueError):
            minutes = None
    when = str(args.get("when") or "").strip()[:64]
    if op == "list":
        return await _backed_existing(
            session,
            "list_timers",
            {},
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind="timer.act",
        )
    if cognition.prepare_only and op in {"start", "cancel", "snooze"}:
        telemetry.inc("stale_mutations_blocked")
        return _failure(
            "POLICY_BLOCKED",
            "Prepare-only: I will not change your timers.",
            diagnosis="POLICY_BLOCKED",
        )
    if op == "start":
        payload: dict[str, Any] = {"text": label}
        if minutes is not None:
            payload["minutes"] = minutes
        elif when:
            payload["at"] = when
        return await _backed_existing(
            session,
            "start_timer",
            payload,
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind="timer.act",
        )
    if op == "cancel":
        return await _backed_existing(
            session,
            "cancel_timer",
            {"id": timer_id, "text": label or None},
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind="timer.act",
        )
    if op == "snooze":
        return await _backed_existing(
            session,
            "snooze_timer",
            {"id": timer_id, "text": label or None, "minutes": minutes if minutes is not None else 5},
            actor=actor,
            live_session_id=live_session_id,
            cognition=cognition,
            kind="timer.act",
        )
    return _failure(
        "CAPABILITY_UNAVAILABLE",
        "That timer op is not on this kernel.",
        diagnosis="CAPABILITY_UNAVAILABLE",
    )


async def _notify_schedule(session: AsyncSession, args: dict[str, Any]) -> dict[str, Any]:
    """Durable owner notification: presence contract with a NOTIFY node."""

    from app.ev.resolve import parse_owner_when
    from app.presence.service import (
        add_condition,
        create_contract,
        get_contract,
        list_contracts,
        set_wait,
        upsert_node,
    )
    from app.utils.text import utcnow

    op = str(args.get("op") or "").strip().lower()
    if op == "schedule":
        objective = str(args.get("objective") or "").strip()
        if not objective:
            return _failure(
                "MISSING_OBJECTIVE",
                "Tell me what the reminder should cover.",
                diagnosis="MISSING_OBJECTIVE",
            )
        when = str(args.get("when") or "").strip()[:128]
        deadline = parse_owner_when(when) if when else None
        if when and deadline is None:
            return _failure(
                "BAD_WHEN",
                "I couldn't read that time.",
                diagnosis="BAD_WHEN",
            )
        channel = str(args.get("channel") or "").strip()[:64]
        try:
            row = await create_contract(
                session,
                objective=objective[:2000],
                deadline_at=deadline,
                constraints={"channel": channel} if channel else {},
                risk_ceiling="R1",
            )
            node = await upsert_node(
                session,
                row,
                node_id="notify:owner",
                kind="NOTIFY",
                target="CORE",
                effect=f"Notify owner: {objective[:180]}",
                risk="R1",
            )
        except Exception as exc:  # noqa: BLE001 - tool boundary
            return _failure(
                "CAPABILITY_UNAVAILABLE",
                "I couldn't schedule that notification.",
                diagnosis=type(exc).__name__,
            )
        scheduled_for = None
        if deadline is not None and deadline > utcnow():
            scheduled_for = deadline.isoformat()
            try:
                await add_condition(
                    session,
                    row,
                    cond_class="TIME",
                    payload={"at": scheduled_for},
                    source="notify.schedule",
                    strategy="tick",
                    frequency_s=60,
                )
                await set_wait(
                    session,
                    row,
                    wait_state="WAITING_FOR_CONDITION",
                    condition={"expect": "time", "at": scheduled_for},
                    reason="scheduled notification",
                )
            except Exception as exc:  # noqa: BLE001 - tool boundary
                return _failure(
                    "CAPABILITY_UNAVAILABLE",
                    "I couldn't schedule that notification.",
                    diagnosis=type(exc).__name__,
                )
        return {
            "ok": True,
            "contract_id": str(row.id),
            "state": row.state,
            "scheduled_for": scheduled_for,
            "channel": channel or "inbox",
            "notify_node": str(node.get("node_id") or ""),
            "spoken": f"Noted. I'll remind you: {objective[:160]}"[:220],
        }
    if op == "cancel":
        contract_id = str(args.get("contract_id") or "").strip()
        if not contract_id:
            return _failure(
                "MISSING_CONTRACT_ID",
                "Which scheduled reminder should I cancel?",
                diagnosis="MISSING_CONTRACT_ID",
            )
        contract_row = await get_contract(session, contract_id)
        if contract_row is None:
            return _failure(
                "CONTRACT_NOT_FOUND",
                "That scheduled reminder is not there anymore.",
                diagnosis="CONTRACT_NOT_FOUND",
            )
        from app.presence.runner import cancel_contract

        summary = await cancel_contract(session, contract_row, "owner cancel via notify.schedule")
        return {"ok": True, "contract_id": str(contract_row.id), "objective": contract_row.objective[:160], **summary}
    if op == "list":
        rows = await list_contracts(session, limit=20)
        return {
            "ok": True,
            "contracts": [
                {
                    "contract_id": str(item.id),
                    "objective": item.objective[:160],
                    "state": item.state,
                    "deadline_at": item.deadline_at.isoformat() if item.deadline_at else None,
                }
                for item in rows
            ],
        }
    return _failure(
        "CAPABILITY_UNAVAILABLE",
        "That notify op is not on this kernel.",
        diagnosis="CAPABILITY_UNAVAILABLE",
    )


async def _phone_call(
    session: AsyncSession,
    args: dict[str, Any],
    *,
    cognition: CognitiveSession,
) -> dict[str, Any]:
    """Paired-iPhone action through the device gateway. Identity never from args."""

    del session  # dispatch_phone_action opens its own session from the live lease
    op = str(args.get("op") or "").strip().lower()
    if op not in _PHONE_OP_MAP:
        return _failure(
            "CAPABILITY_UNAVAILABLE",
            "That phone op is not on this kernel.",
            diagnosis="CAPABILITY_UNAVAILABLE",
        )
    contact = str(args.get("contact") or args.get("to") or "").strip()[:80]
    if not contact:
        return _failure(
            "MISSING_CONTACT",
            "Who should I reach?",
            diagnosis="MISSING_CONTACT",
        )
    arguments: dict[str, Any] = {"operation": _PHONE_OP_MAP[op], "contact_query": contact}
    if op == "message":
        message = str(args.get("message") or "").strip()[:500]
        if not message:
            return _failure(
                "MISSING_MESSAGE",
                "Tell me what to send.",
                diagnosis="MISSING_MESSAGE",
            )
        arguments["message"] = message
    from app.voice.live.layer import active_lives

    try:
        lives = active_lives()
    except Exception:
        lives = []
    live = next(
        (item for item in lives if getattr(item, "device_id", None)),
        None,
    )
    if live is None or cognition.prepare_only:
        if cognition.prepare_only:
            telemetry.inc("stale_mutations_blocked")
            return _failure(
                "POLICY_BLOCKED",
                "Prepare-only: I will not touch the phone.",
                diagnosis="POLICY_BLOCKED",
            )
        return _failure(
            "PHONE_NOT_CONNECTED",
            "I need a paired iPhone in a live session to do that.",
            diagnosis="PHONE_NOT_CONNECTED",
        )
    from app.device_gateway.mobile_actions.tool import dispatch_phone_action

    try:
        payload = await dispatch_phone_action(
            device_id=str(getattr(live, "device_id", None) or ""),
            role=str(getattr(live, "device_role", None) or "companion"),
            instance_id=str(getattr(live, "instance_id", None) or ""),
            session_id=str(getattr(live, "session_id", None) or ""),
            origin=str(getattr(live, "gateway_origin", None) or ""),
            arguments=arguments,
            transcript="",
            device_label=str(getattr(live, "device_label", None) or "This iPhone"),
        )
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return _failure(
            "CAPABILITY_UNAVAILABLE",
            "The phone action did not go through.",
            diagnosis=type(exc).__name__,
        )
    body = _strip_secrets(payload if isinstance(payload, dict) else {})
    remember_effect(
        cognition,
        {
            "kind": "phone.call",
            "ok": body.get("ok"),
            "error": body.get("error") or body.get("failure"),
            "operation": arguments.get("operation"),
            "who": contact,
            "spoken": str(body.get("spoken") or "")[:240] or None,
        },
    )
    telemetry.inc("background_executions")
    return body
