"""Sandbox text pipeline. Never run_chat_pipeline / Memory OS / relationship attach."""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device

from . import SANDBOX_NAMESPACE
from .handoff import current_state, record_turn, state_public
from .mac import run_mac_canary
from .routing import resolve_target, wants_camera, wants_mac_action
from .sandbox import (
    extract_remember,
    looks_like_personal_probe,
    production_memory_leak_probe,
    recall_fact,
    remember_fact,
    wants_code_recall,
)

_PIPELINE_PRIMARY = re.compile(r"\bpipeline-primary\b", re.IGNORECASE)
_PIPELINE_SECONDARY = re.compile(r"\bpipeline-secondary\b", re.IGNORECASE)
_SAY_PRIMARY = re.compile(r"\bsay primary pipeline works\b", re.IGNORECASE)
_SAY_SECONDARY = re.compile(r"\bsay secondary pipeline works\b", re.IGNORECASE)
_CROSSPLATFORM = re.compile(
    r"\b(?:return |say )?CROSSPLATFORM-(PRIMARY|SECONDARY)-([A-Za-z0-9_-]+)\b",
    re.IGNORECASE,
)
_CONTINUE_HERE = re.compile(r"\bcontinue here\b", re.IGNORECASE)
_HANDOFF = re.compile(
    r"\b(continue what i was saying|where was i|what were we (?:just )?discussing|what was i talking about)\b",
    re.IGNORECASE,
)
_DISCUSSING = re.compile(
    r"\b(?:we(?:'re| are) discussing|let'?s talk about)\s+(.+?)\.?\s*$",
    re.IGNORECASE,
)
_PROJECT = re.compile(r"\bproject\s+([A-Za-z0-9][\w \-]{1,80})", re.IGNORECASE)


async def handle_user_text(
    session: AsyncSession,
    *,
    device: Device,
    text: str,
    request_id: str | None = None,
    instance_id: str = "",
    origin: str = "",
) -> dict[str, Any]:
    raw = (text or "").strip()
    rid = request_id or uuid4().hex
    origin_id = device.id
    target = await resolve_target(session, raw, origin=device)
    role = (device.role or "companion").strip().lower()
    topic = _topic(raw)

    xp = _CROSSPLATFORM.search(raw)
    if xp:
        kind = xp.group(1).upper()
        nonce = xp.group(2)
        reply = f"CROSSPLATFORM-{kind}-{nonce}"
        return await _finish(
            session,
            device=device,
            target=target,
            request_id=rid,
            text=raw,
            reply=reply,
            topic=topic,
        )

    if _CONTINUE_HERE.search(raw):
        return await _finish(
            session,
            device=device,
            target=target,
            request_id=rid,
            text=raw,
            reply="Continuing here.",
            topic=topic,
        )

    if _SAY_PRIMARY.search(raw) or _PIPELINE_PRIMARY.search(raw):
        reply = "PIPELINE-PRIMARY" if _PIPELINE_PRIMARY.search(raw) else "primary pipeline works"
        if role == "secondary_companion" and _PIPELINE_PRIMARY.search(raw):
            reply = "PIPELINE-PRIMARY"
        return await _finish(
            session,
            device=device,
            target=target,
            request_id=rid,
            text=raw,
            reply=reply,
            topic=topic,
        )
    if _SAY_SECONDARY.search(raw) or _PIPELINE_SECONDARY.search(raw):
        reply = "PIPELINE-SECONDARY" if _PIPELINE_SECONDARY.search(raw) else "secondary pipeline works"
        return await _finish(
            session,
            device=device,
            target=target,
            request_id=rid,
            text=raw,
            reply=reply,
            topic=topic,
        )

    remembered = extract_remember(raw)
    if remembered is not None:
        key, value = remembered
        await remember_fact(session, key=key, value=value, device_id=device.id)
        reply = f"Sandbox remembered {key.replace('_', ' ')} as {value}."
        return await _finish(
            session,
            device=device,
            target=target,
            request_id=rid,
            text=raw,
            reply=reply,
            topic=topic or "sandbox satellite",
        )

    if wants_code_recall(raw):
        value = await recall_fact(session, "satellite_code")
        reply = value if value else "No sandbox satellite code is stored yet."
        return await _finish(
            session,
            device=device,
            target=target,
            request_id=rid,
            text=raw,
            reply=reply,
            topic=topic,
        )

    if _HANDOFF.search(raw):
        state = await current_state(session)
        public = state_public(state)
        if public is None:
            reply = "No active sandbox conversation to continue."
        else:
            topic_line = public.get("topic") or "the current sandbox topic"
            last = ""
            turns = public.get("turns") or []
            if turns:
                last = str(turns[-1].get("text") or "")
            reply = f"We were discussing {topic_line}."
            if last:
                reply += f" Last: {last}"
        return await _finish(
            session,
            device=device,
            target=target,
            request_id=rid,
            text=raw,
            reply=reply,
            topic=topic,
        )

    if looks_like_personal_probe(raw):
        probe = await production_memory_leak_probe(session, raw)
        reply = (
            "Sandbox pipeline has no access to production Memory OS. "
            "I only have isolated test facts in this environment."
        )
        return await _finish(
            session,
            device=device,
            target=target,
            request_id=rid,
            text=raw,
            reply=reply,
            topic=topic,
            extra={"privacy": probe},
        )

    mac_action = wants_mac_action(raw)
    if mac_action:
        mac = await run_mac_canary(
            session,
            action=mac_action,
            actor=f"device:{device.name}",
            idempotency_key=rid,
        )
        reply = str(mac.get("spoken") or "Mac Control is unavailable.")
        if not mac.get("ok"):
            reply = mac.get("spoken") or "Mac Control is unavailable. I did not complete that action."
        return await _finish(
            session,
            device=device,
            target=target,
            request_id=rid,
            text=raw,
            reply=reply,
            topic=topic,
            extra={"mac": mac, "action_target_device_id": str(target.id)},
        )

    from .mobile_actions.engine import (
        apply_confirmation_utterance,
        create_phone_action,
        infer_from_text,
    )

    handled = apply_confirmation_utterance(
        device_id=str(device.id),
        origin=origin or "http://127.0.0.1:8000",
        text=raw,
    )
    if handled is not None:
        reply = str(handled.get("spoken") or "I heard that.")
        extra = {
            "phone_action": handled,
            "action_target_device_id": str(device.id),
        }
        return await _finish(
            session,
            device=device,
            target=target,
            request_id=rid,
            text=raw,
            reply=reply,
            topic=topic,
            extra=extra,
        )

    inferred = infer_from_text(raw)
    if inferred:
        role = (device.role or "companion").strip().lower()
        label = (
            "Primary iPhone"
            if role == "primary_companion"
            else "Secondary iPhone"
            if role == "secondary_companion"
            else device.name or "This iPhone"
        )
        action = create_phone_action(
            device_id=str(device.id),
            role=role,
            instance_id=instance_id,
            session_id=None,
            origin=origin or "http://127.0.0.1:8000",
            arguments=inferred,
            transcript=raw,
            device_label=label,
        )
        reply = str(action.get("spoken") or "I prepared that for this iPhone.")
        extra = {
            "phone_action": action,
            "action_target_device_id": str(device.id),
        }
        return await _finish(
            session,
            device=device,
            target=target,
            request_id=rid,
            text=raw,
            reply=reply,
            topic=topic,
            extra=extra,
        )

    if wants_camera(raw):
        from . import camera as cam

        request = cam.new_request(
            origin_device_id=str(origin_id),
            target_device_id=str(target.id),
        )
        reply = "Look at this — capture on the origin device."
        if str(target.id) != str(origin_id):
            reply = "Use the targeted device camera, not this phone's by default."
        return await _finish(
            session,
            device=device,
            target=target,
            request_id=rid,
            text=raw,
            reply=reply,
            topic=topic,
            extra={
                "needs_camera": True,
                "camera_request_id": request,
                "camera_target_device_id": str(target.id),
            },
        )

    if topic:
        reply = f"Sandbox noted: {topic}."
    else:
        reply = "Sandbox pipeline heard you. Production memory is disabled."
    return await _finish(
        session,
        device=device,
        target=target,
        request_id=rid,
        text=raw,
        reply=reply,
        topic=topic,
    )


def _topic(text: str) -> str | None:
    match = _DISCUSSING.search(text or "")
    if match:
        return match.group(1).strip()[:240]
    match = _PROJECT.search(text or "")
    if match:
        return f"Project {match.group(1).strip()}"[:240]
    return None


async def _finish(
    session: AsyncSession,
    *,
    device: Device,
    target: Device,
    request_id: str,
    text: str,
    reply: str,
    topic: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    await record_turn(session, device_id=device.id, role="user", text=text, topic=topic)
    await record_turn(session, device_id=device.id, role="assistant", text=reply, topic=topic)
    payload = {
        "request_id": request_id,
        "reply": reply,
        "origin_device_id": str(device.id),
        "response_device_id": str(device.id),
        "action_target_device_id": str(target.id),
        "memory_scope": "sandbox",
        "namespace": SANDBOX_NAMESPACE,
        "environment": "SANDBOX",
    }
    if extra:
        payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# G2 ONE-EVIE: canonical owner-turn execution for TRUSTED endpoints.
# Shared by POST /device-gateway/text and the owner broker tool
# (evie_state_query) used by trusted WebRTC realtime sessions.
# Every turn: OwnerTurn -> TurnGate -> Evie Core -> durable trace events.
# ---------------------------------------------------------------------------


_ROUTED_PING = re.compile(r"\bping\b.*\bmac\b|\bmac\b.*\bping\b|\becho\b.*\bmac\b|\bmac\b.*\becho\b", re.I)
_ROUTED_NOTIFY = re.compile(r"\bnotify\b.*\bmac\b|\bmac\b.*\bnotify\b|\bnotification\b.*\bmac\b|\bnotify\b.*\bhome\b", re.I)


def _detect_routed_capability(text: str) -> tuple[str, dict] | None:
    raw = (text or "").strip()
    raw.lower()
    # Notify capability has priority over ping (more specific)
    if _ROUTED_NOTIFY.search(raw):
        # Extract message after notify/colon
        m = re.search(r"notify[^:]*[:\-]\s*(.+)$", raw, re.I)
        msg = (m.group(1) if m else raw)[:280]
        # If raw is just "notify my mac" without message, keep generic
        title = "Evie"
        body = msg if len(msg) > 5 else "G2 cross-device notification"
        return "mac.notify", {"title": title, "body": body, "text": raw}
    if _ROUTED_PING.search(raw):
        m = re.search(r"ping[^:]*[:\-]\s*(.+)$", raw, re.I)
        msg = (m.group(1) if m else raw)[:200]
        return "device.echo", {"text": raw, "message": msg or raw, "payload": msg or "ping"}
    return None


async def run_trusted_device_turn(
    session: AsyncSession,
    *,
    device: Device,
    text: str,
    idempotency_key: str | None = None,
    focus_title: str | None = None,
) -> dict[str, Any]:
    """focus_title: SAME-SESSION bounded entity focus (P0.1 PART 7).

    When the provider reports the entity currently under discussion and the
    owner's words are a bare field follow-up ("what is the priority level?"),
    the query is deterministically rewritten to name that entity. No
    cross-device context architecture — this is one session's focus.

    G2: also checks deterministic cross-device capability routing (B1) and
    bounded context pronoun resolution (C2) before TurnGate.
    """

    from app.ev.owner_turn import create_owner_turn
    from app.ev.turn_gate import handle_owner_turn
    from app.everywhere.owner import owner_scope
    from app.everywhere.sync import emit_everywhere_event

    # Server-derived scope; a paired-sandbox device never reaches here via
    # the broker, but the guard keeps the law explicit (fail closed).
    ctx_scope = owner_scope(f"device:{device.name}", device=device)
    if ctx_scope == "master" and (
        str(getattr(device, "memory_scope", "") or "").lower() == "sandbox"
        or device.revoked_at is not None
    ):
        return {
            "reply": "This device's access changed; reconnect to continue.",
            "ok": False,
            "error_code": "AUTH_REFRESH_REQUIRED",
            "route": "UNSUPPORTED",
            "operation": "UNKNOWN",
            "turn_id": None,
        }

    effective_text = text or ""
    if (
        focus_title
        and focus_title not in effective_text
        and __import__("re").search(
            r"\b(priority|status|due|state)\b", effective_text, __import__("re").IGNORECASE
        )
    ):
        # Bare field follow-up about the focused entity: rewrite into a
        # canonical form the deterministic router resolves (PROJECT_GET).
        lowered = effective_text.lower()
        if "priority" in lowered:
            effective_text = f"What is the priority of {focus_title}?"
        elif "status" in lowered or "state" in lowered:
            effective_text = f"What is the status of {focus_title}?"
        elif "due" in lowered:
            effective_text = f"When is {focus_title} due?"

    # G2 B — deterministic cross-device capability routing (model does NOT decide executor)
    routed = _detect_routed_capability(effective_text)
    if routed is not None:
        cap, args = routed
        try:
            from app.everywhere.device_actions import create_routed_action

            # Stable idempotency for voice turn: reuse provider item / idempotency key if given
            broker_id = (idempotency_key or "")[:64] or uuid4().hex[:16]
            # action_id must be stable per turn to avoid duplicate side effects on retry
            action_id = f"voice-{device.id}-{broker_id}"[:80]
            broker = await create_routed_action(
                session,
                requesting_device=device,
                capability=cap,
                arguments=args,
                action_id=action_id,
                owner_scope=ctx_scope,
            )
            await session.commit()
        except Exception:  # noqa: BLE001 - broker failure is truthful
            await session.rollback()
            return {
                "reply": "I couldn't route that to the other device. Try again.",
                "ok": False,
                "error_code": "CAPABILITY_UNAVAILABLE",
                "route": "DEVICE_ACTION",
                "operation": cap,
                "turn_id": None,
            }
        # Surface broker truth: queued vs routed vs failed (never fake success)
        if broker.get("ok") is True:
            status = broker.get("status") or "ROUTED"
            if status in {"QUEUED", "ROUTED"}:
                if broker.get("queued") or status == "QUEUED":
                    reply = f"I've queued that for your Mac ({cap}). It will run when the Mac is online."
                else:
                    reply = f"I've sent that to your Mac ({cap})."
                # Emit durable trace for observability
                try:
                    await emit_everywhere_event(
                        session,
                        event_type="device.action.routed",
                        actor_label=f"device:{device.name}",
                        content={"capability": cap, "action_id": broker.get("action_id"), "target": broker.get("target_device_id"), "status": status},
                        device_id=str(device.id),
                    )
                    await session.commit()
                except Exception:
                    pass
                return {
                    "reply": reply,
                    "ok": True,
                    "route": "DEVICE_ACTION",
                    "operation": cap,
                    "broker": broker,
                    "turn_id": None,
                }
        # Broker returned soft error (offline, capability unavailable)
        err = broker.get("error_code") or "CAPABILITY_UNAVAILABLE"
        if err == "TARGET_DEVICE_OFFLINE":
            return {
                "reply": "Your Mac is offline right now, so I queued that. It will run when the Mac comes back online.",
                "ok": False,
                "error_code": "TARGET_DEVICE_OFFLINE",
                "route": "DEVICE_ACTION",
                "operation": cap,
                "broker": broker,
                "turn_id": None,
            }
        return {
            "reply": broker.get("message") or "I couldn't complete that capability on the other device.",
            "ok": False,
            "error_code": err,
            "route": "DEVICE_ACTION",
            "operation": cap,
            "broker": broker,
            "turn_id": None,
        }

    # G2 C — bounded cross-device context pronoun resolution (pronoun -> focused entity)
    if focus_title is None and __import__("re").search(r"\b(priority|status|due|state)\b", effective_text, __import__("re").IGNORECASE):
        # Only attempt cross-device resolve when same-session focus didn't already rewrite
        # and text looks like a pronoun follow-up without explicit title
        low = effective_text.lower()
        has_pronoun = bool(__import__("re").search(r"\bits\b|\bit\b|\bthat\b|\bthis\b", low))
        # Quick check: if effective_text already contains a known project word, don't rewrite
        # (prevents stealing explicit titles)
        try:
            from app.everywhere.handoff_context import resolve_pronoun as _resolve

            if has_pronoun or "its priority" in low:
                res = await _resolve(session, text=effective_text, requesting_device=device)
                if res.get("ok") and res.get("resolved"):
                    title = res.get("focused_title") or ""
                    # Rewrite into canonical PROJECT_GET form and reread Core (C7)
                    if "priority" in low and title and title.lower() not in low:
                        effective_text = f"What is the priority of {title}?"
                    elif ("status" in low or "state" in low) and title and title.lower() not in low:
                        effective_text = f"What is the status of {title}?"
                    elif "due" in low and title and title.lower() not in low:
                        effective_text = f"When is {title} due?"
                elif res.get("clarify"):
                    return {
                        "reply": res.get("message") or "Which one did you mean?",
                        "ok": False,
                        "error_code": "AMBIGUOUS_CONTEXT",
                        "route": "UNSUPPORTED",
                        "operation": "UNKNOWN",
                        "needs_clarification": True,
                        "turn_id": None,
                    }
        except Exception:
            pass
    turn = create_owner_turn(
        live_session_id=f"device-text:{device.id}",
        provider_item_id=idempotency_key,
        owner_id="master",
        device_id=str(device.id),
        transcript=effective_text,
        transcript_source="device_text",
        turn_id=(
            f"text-{device.id}-{idempotency_key}" if idempotency_key else None
        ),
    )
    # PART 7/18: rollback guarantee + sanitized canonical failure. Raw
    # database text must never reach the model or the durable trace.
    try:
        result = await handle_owner_turn(session, turn)
        await session.commit()
    except Exception:  # noqa: BLE001 - canonical conversion point
        from contextlib import suppress

        with suppress(Exception):
            await session.rollback()
        result = None
    if result is None:
        return {
            "reply": (
                "I couldn't complete that because an internal state "
                "operation failed. Your request was safely cancelled."
            ),
            "ok": False,
            "error_code": "DATABASE_TEMPORARY_FAILURE",
            "retryable": True,
            "route": "UNSUPPORTED",
            "operation": "UNKNOWN",
            "needs_clarification": False,
            "turn_id": turn.turn_id,
        }
    if result.route == "CONVERSATION":
        # P0.1 PART 6 LAW: a STATE broker must never answer a conversational
        # turn with "Done." Signal the provider to answer from its own
        # conversation; Core asserted there is no canonical state here.
        # F1: turn-scoped recalled history rides along (labeled, expiring).
        shadow = result.shadow_context if isinstance(result.shadow_context, dict) else None
        return {
            "ok": True,
            "conversational": True,
            "reply": None,
            "error_code": None,
            "route": "CONVERSATION",
            "operation": "UNKNOWN",
            "turn_id": turn.turn_id,
            "recalled_history": str((shadow or {}).get("block") or "").strip() or None,
        }
    reply = result.owner_message or (
        "Done." if result.ok else "That didn't complete."
    )
    # PART 18 ERROR SANITIZATION: structured codes pass through; anything
    # carrying a raw exception (route_failed: ...) becomes a safe canonical
    # failure with no internals.
    error_code = result.error if not result.ok else None
    if error_code and any(
        ch in str(error_code) for ch in ("(", ":", "`")
    ):
        error_code = "OWNER_TURN_FAILED"
    await emit_everywhere_event(
        session,
        event_type="message.user",
        actor_label=f"device:{device.name}",
        content={
            "text": (text or "")[:2000],
            "turn_id": turn.turn_id,
            "route": result.route,
            "operation": result.operation,
        },
        device_id=str(device.id),
    )
    await emit_everywhere_event(
        session,
        event_type="message.assistant",
        actor_label="evie",
        content={
            "text": reply[:2000],
            "turn_id": turn.turn_id,
            "ok": bool(result.ok),
            "error_code": error_code,
        },
        device_id=str(device.id),
    )
    await session.commit()
    # G2 C — update bounded context on successful entity focus (auto handoff)
    try:
        if result.ok and result.operation in {"PROJECT_GET", "PROJECT_CREATE", "PROJECT_UPDATE", "PROJECT_LIST"}:
            data = result.canonical_data
            # PROJECT_LIST returns list; take first if filtered, else top_focus
            title = None
            pid = None
            if isinstance(data, dict) and data.get("title"):
                title = data.get("title")
                pid = data.get("id")
            elif isinstance(data, list) and data:
                # If list filtered to one, that's focus
                if len(data) == 1:
                    title = data[0].get("title")
                    pid = data[0].get("id")
            if title and pid:
                from app.everywhere.handoff_context import set_context

                await set_context(
                    session,
                    source_device=device,
                    focused_type="project",
                    focused_id=str(pid),
                    focused_title=str(title),
                    focused_project_id=str(pid),
                    focused_project_title=str(title),
                )
                await session.commit()
    except Exception:
        pass
    return {
        "reply": reply,
        "ok": bool(result.ok),
        "error_code": error_code if result.ok is False else None,
        "route": result.route,
        "operation": result.operation,
        "needs_clarification": bool(result.needs_clarification),
        "turn_id": turn.turn_id,
        "duplicate": bool(getattr(result, "duplicate", False)),
    }
