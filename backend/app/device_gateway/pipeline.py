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

    from .mobile_actions.service import create_phone_action, infer_from_text

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
