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
        effect = str(args.get("effect") or "")
        if cognition.prepare_only and "PREPARE_ONLY" not in effect.upper():
            effect = f"[PREPARE_ONLY — do not write or replace files; describe the patch.] {effect}"
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
        if decided.get("last_path"):
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
    return _failure("CAPABILITY_UNAVAILABLE", f"I don't have {name} on this kernel.")


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
    to = str(args.get("to") or args.get("name") or "").strip()
    body = str(args.get("text") or args.get("body") or "").strip()
    if not to or not body:
        return _failure(
            "CAPABILITY_UNAVAILABLE",
            "I need who to message and what to say.",
        )
    channel = str(args.get("channel") or "").strip().lower()
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
        body = _strip_secrets(result.model_dump() if hasattr(result, "model_dump") else dict(result))
    else:
        body = await execute_on_mac(name, arguments, live_session_id=live_session_id)
        body = _strip_secrets(body)
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
