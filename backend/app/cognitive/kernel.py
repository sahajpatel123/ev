"""Cognitive Kernel — OwnerTurn → reflex or Muse. No second general mind."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.cognitive import telemetry
from app.cognitive.context import compile_context
from app.cognitive.executor import dump_tool_json, execute_semantic
from app.cognitive.reflex import match_reflex
from app.cognitive.session_store import (
    bind_live,
    bump_steering,
    current,
    has_active_work,
    save,
    status_line,
)
from app.cognitive.speed import (
    compact_turn,
    max_tool_turns,
    reasoning_effort,
    should_prefetch_memory,
    tool_specs_for_turn,
)
from app.contracts import ChatMessage
from app.device_gateway.cognitive_text import PhoneTextContext
from app.gateway.muse import MuseProviderUnavailable, muse_spark_key_loaded, muse_spark_model
from app.gateway.reliability import CircuitOpenError

_CODE_WORKING = (
    "I'm writing that now. I'll tell you when it's saved and I've run it."
)


async def _dispatch_kernel_code(
    session: AsyncSession,
    text: str,
    *,
    cognition,
    actor: str,
    live_session_id: str | None,
    modality: str,
    steering_seen: int,
    started: float,
) -> KernelResult | None:
    """Run the coding jail instead of letting Muse narrate a write."""

    from app.ev.code_studio import looks_like_long_code_goal, maybe_handle_code_ops
    from app.ev.luna_code import owner_asked_to_code, run_code_job_and_notify

    if cognition.prepare_only or not owner_asked_to_code(text):
        return None
    if looks_like_long_code_goal(text):
        ack = maybe_handle_code_ops(text, session_key=str(live_session_id or "owner"))
        if not ack:
            return None
        telemetry.inc("background_executions")
        telemetry.note(
            last_turn_kind="code",
            last_transcript_to_muse_ms=telemetry.timed_ms(started),
        )
        return KernelResult(
            spoken=ack[:2000],
            kind="code",
            persist=True,
            tool_calls=1,
            steering_version=cognition.steering_version,
            goal_id=cognition.focused_goal_id,
            latency_ms=telemetry.timed_ms(started),
        )
    voice = (modality or "").lower() == "voice" or bool(live_session_id)
    if voice:
        asyncio.create_task(
            run_code_job_and_notify(
                text[:8000],
                actor=actor,
                session_key=str(live_session_id or "owner"),
            ),
            name="ev-kernel-code-job",
        )
        telemetry.inc("background_executions")
        telemetry.note(
            last_turn_kind="code",
            last_transcript_to_muse_ms=telemetry.timed_ms(started),
        )
        return KernelResult(
            spoken=_CODE_WORKING,
            kind="code",
            persist=True,
            tool_calls=1,
            steering_version=cognition.steering_version,
            goal_id=cognition.focused_goal_id,
            latency_ms=telemetry.timed_ms(started),
        )
    evidence = await execute_semantic(
        session,
        "code.act",
        {"effect": text[:4000]},
        cognition=cognition,
        actor=actor,
        live_session_id=live_session_id,
        steering_seen=steering_seen,
    )
    spoken = str((evidence or {}).get("spoken") or "").strip() or (
        "I couldn't finish that coding job."
    )
    telemetry.inc("muse_tool_calls")
    telemetry.note(
        last_turn_kind="code",
        last_transcript_to_muse_ms=telemetry.timed_ms(started),
    )
    return KernelResult(
        spoken=spoken[:2000],
        kind="code",
        persist=True,
        tool_calls=1,
        steering_version=cognition.steering_version,
        goal_id=cognition.focused_goal_id,
        latency_ms=telemetry.timed_ms(started),
    )


@dataclass
class KernelResult:
    spoken: str
    kind: str = "muse"
    unavailable: bool = False
    persist: bool = False
    steering_version: int = 0
    goal_id: str | None = None
    latency_ms: float = 0.0
    tool_calls: int = 0
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        from app.gateway.muse import muse_spark_inference_route

        return {
            "spoken": self.spoken,
            "kind": self.kind,
            "unavailable": self.unavailable,
            "persist": self.persist,
            "steering_version": self.steering_version,
            "goal_id": self.goal_id,
            "latency_ms": self.latency_ms,
            "tool_calls": self.tool_calls,
            "muse_provider": muse_spark_inference_route(),
        }


_UNAVAILABLE = (
    "I can't think that through right now. Give me a moment and ask again — "
    "I won't guess."
)


def _spoken_send_receipt(body: dict[str, Any] | None) -> str:
    """Owner-facing send result. Never a bare Okay — that hid real failures."""

    payload = body if isinstance(body, dict) else {}
    spoken = str(payload.get("spoken") or "").strip()
    if spoken and spoken.lower() not in {"ok", "okay", "okay.", "ok."}:
        return spoken[:2000]
    who = str(payload.get("to") or payload.get("name") or "them").strip() or "them"
    if payload.get("sent"):
        return f"Sent to {who}."
    channel = str(payload.get("channel") or "").strip().lower()
    if payload.get("opened") and channel == "whatsapp":
        return (
            f"WhatsApp to {who} is open with your message ready — tap send to finish it."
        )
    if payload.get("opened"):
        return f"The message to {who} is ready."
    err = str(
        payload.get("next_step")
        or payload.get("error")
        or payload.get("reason")
        or ""
    ).strip()
    if err:
        if err.lower() in {"ok", "okay", "okay."}:
            err = ""
        else:
            if err[:2].lower() == "i ":
                return err[:2000]
            return f"I couldn't send that. {err}"[:2000]
    if payload.get("ok") is False:
        return f"I couldn't send that to {who}."
    return (
        f"I tried to send that to {who}, but I didn't get a receipt from the Mac."
    )


def _spoken_computer_receipt(body: dict[str, Any] | None) -> str:
    """Owner-facing Mac open/navigate result. Never a bare Okay."""

    payload = body if isinstance(body, dict) else {}
    spoken = str(payload.get("spoken") or "").strip()
    if spoken and spoken.lower() not in {"ok", "okay", "okay.", "ok."}:
        return spoken[:2000]
    url = str(payload.get("url") or payload.get("query") or "").strip()
    action = str(payload.get("action") or "").strip().lower()
    if payload.get("ok") is False:
        err = str(payload.get("error") or payload.get("reason") or "").strip()
        if err:
            return f"I couldn't do that on the Mac. {err}"[:2000]
        return "I couldn't do that on the Mac."
    if action == "navigate" and url:
        return f"Opening {url}."
    if payload.get("ok"):
        return "Done."
    return "I tried that on the Mac, but I didn't get a receipt."


async def handle_turn(
    *,
    transcript: str,
    live_session_id: str | None = None,
    device_id: str | None = None,
    modality: str = "voice",
    session: AsyncSession | None = None,
    actor: str = "master",
    phone_text_context: PhoneTextContext | None = None,
) -> KernelResult:
    started = time.perf_counter()
    text = (transcript or "").strip()
    if phone_text_context is not None and session is not None:
        from app.device_gateway.cognitive_text import maybe_cancel_phone_text

        cancelled = await maybe_cancel_phone_text(session, context=phone_text_context, transcript=text)
        if cancelled is not None:
            return KernelResult(
                spoken=str(cancelled.get("spoken") or cancelled.get("reply") or "Cancellation could not be confirmed."),
                kind="phone_cancel", tool_calls=1, evidence=[cancelled],
                latency_ms=telemetry.timed_ms(started),
            )
    cognition = bind_live(live_session_id)
    reflex = match_reflex(
        text,
        has_active_goal=has_active_work(cognition),
        status_line=status_line(cognition),
    )
    if reflex is not None:
        telemetry.inc("deterministic_reflex_turns")
        if reflex.cancel_work:
            from app.cognitive.intent import clear_pending_send

            clear_pending_send(cognition)
            if session is not None:
                from app.cognitive.executor import _cancel_goal

                await _cancel_goal(session, cognition)
            else:
                cognition.focused_goal_id = None
                cognition.semantic_objective = ""
                bump_steering(cognition)
        elif reflex.park:
            cognition.parked = True
            save(cognition)
        elif reflex.resume:
            cognition.parked = False
            save(cognition)
        telemetry.note(last_turn_kind="reflex")
        return KernelResult(
            spoken=reflex.spoken,
            kind=f"reflex:{reflex.kind}",
            steering_version=cognition.steering_version,
            goal_id=cognition.focused_goal_id,
            latency_ms=telemetry.timed_ms(started),
        )

    from app.cognitive.intent import (
        begin_owner_turn,
        clear_pending_send,
        pending_send,
        set_pending_send,
        take_dropped_goal_id,
    )
    begin_owner_turn(cognition, text)
    dropped_goal = take_dropped_goal_id(cognition)

    # A parked WhatsApp Web send is approved or cancelled deterministically
    # before any model call: one "yes" resumes the exact prepared message.
    from app.ev.messaging.approval import handle_send_approval

    if session is None:
        from app.db import SessionLocal

        async with SessionLocal() as approval_db:
            approval = await handle_send_approval(
                approval_db,
                text,
                actor=actor,
                device_id=device_id,
                live_session_id=live_session_id,
            )
            if approval is not None:
                await approval_db.commit()
    else:
        approval = await handle_send_approval(
            session,
            text,
            actor=actor,
            device_id=device_id,
            live_session_id=live_session_id,
        )
    if approval is not None:
        return KernelResult(
            spoken=str(approval.get("spoken") or ""),
            kind="send_approval",
            persist=False,
            steering_version=cognition.steering_version,
            latency_ms=telemetry.timed_ms(started),
        )

    # Phone requests must never enter the deterministic Mac send shortcut.
    # Classify using the persisted device, not a model-supplied tool argument.
    from app.device_gateway.cognitive_phone import is_phone_turn

    if device_id:
        from app.db import SessionLocal

        async def _phone(db: AsyncSession) -> KernelResult | None:
            if not await is_phone_turn(db, device_id):
                return None
            if not muse_spark_key_loaded():
                return KernelResult(spoken=_UNAVAILABLE, kind="unavailable", unavailable=True)
            action_receipts: list[dict[str, Any]] = []
            result = await _muse_turn(
                db, text=text, live_session_id=live_session_id, device_id=device_id,
                modality=modality, actor=actor, cognition=cognition, started=started,
                action_receipts=action_receipts,
                phone_text_context=phone_text_context,
            )
            # Private in-process result channel, including when the model fails
            # after preparing an action. Never add these cards to model context.
            result.evidence.extend(action_receipts)
            if session is None:
                await db.commit()
            else:
                # A receipt caller owns its transaction; do not commit its
                # partially populated row before the final reply is stamped.
                await db.flush()
            return result

        if session is None:
            async with SessionLocal() as db:
                phone_result = await _phone(db)
        else:
            phone_result = await _phone(session)
        if phone_result is not None:
            return phone_result

    from app.ev.send_intent import (
        incomplete_send,
        looks_like_message_body,
        parse_send_intent,
        prompt_for_send_body,
    )

    send = parse_send_intent(text)
    if send is None:
        asked = incomplete_send(text)
        if asked is not None:
            set_pending_send(
                cognition,
                to=str(asked["to"]),
                channel=asked.get("channel"),
            )
            spoken = prompt_for_send_body(
                to=str(asked["to"]),
                channel=asked.get("channel"),
            )
            telemetry.note(last_turn_kind="send_prompt")
            return KernelResult(
                spoken=spoken,
                kind="send_prompt",
                persist=False,
                steering_version=cognition.steering_version,
                latency_ms=telemetry.timed_ms(started),
            )
        waiting = pending_send(cognition)
        domain = str((cognition.constraints or {}).get("turn_domain") or "")
        if (
            waiting
            and domain not in {"look", "file"}
            and looks_like_message_body(text)
        ):
            send = {
                "to": waiting["to"],
                "text": text.strip()[:500],
            }
            if waiting.get("channel"):
                send["channel"] = waiting["channel"]
        elif waiting:
            clear_pending_send(cognition)
    if send is not None:
        clear_pending_send(cognition)
        from app.db import SessionLocal

        async def _send(db: AsyncSession) -> KernelResult:
            if dropped_goal:
                from app.cognitive.executor import _cancel_goal

                cognition.focused_goal_id = dropped_goal
                await _cancel_goal(db, cognition)
            from app.cognitive.executor import execute_semantic

            body = await execute_semantic(
                db,
                "life.send",
                send,
                cognition=cognition,
                actor=actor,
                live_session_id=live_session_id,
                steering_seen=int(cognition.steering_version),
            )
            spoken = _spoken_send_receipt(body)
            telemetry.note(last_turn_kind="send")
            return KernelResult(
                spoken=spoken[:2000],
                kind="send",
                persist=False,
                steering_version=cognition.steering_version,
                latency_ms=telemetry.timed_ms(started),
                tool_calls=1,
            )

        if session is None:
            async with SessionLocal() as db:
                result = await _send(db)
                await db.commit()
                return result
        result = await _send(session)
        await session.commit()
        return result

    from app.ev.computer_strategy import parse_open_intent

    opened = parse_open_intent(text)
    if opened is not None:
        from app.db import SessionLocal

        async def _open(db: AsyncSession) -> KernelResult:
            if dropped_goal:
                from app.cognitive.executor import _cancel_goal

                cognition.focused_goal_id = dropped_goal
                await _cancel_goal(db, cognition)
            body = await execute_semantic(
                db,
                "computer.perform_effect",
                {"effect": text},
                cognition=cognition,
                actor=actor,
                live_session_id=live_session_id,
                steering_seen=int(cognition.steering_version),
            )
            spoken = _spoken_computer_receipt(body)
            telemetry.note(last_turn_kind="computer")
            return KernelResult(
                spoken=spoken[:2000],
                kind="computer",
                persist=False,
                steering_version=cognition.steering_version,
                latency_ms=telemetry.timed_ms(started),
                tool_calls=1,
            )

        if session is None:
            async with SessionLocal() as db:
                result = await _open(db)
                await db.commit()
                return result
        result = await _open(session)
        await session.commit()
        return result

    if not muse_spark_key_loaded():
        telemetry.inc("unavailable")
        telemetry.inc("provider_failures")
        return KernelResult(
            spoken=_UNAVAILABLE,
            kind="unavailable",
            unavailable=True,
            latency_ms=telemetry.timed_ms(started),
        )

    from app.db import SessionLocal

    async def _run(db: AsyncSession) -> KernelResult:
        if dropped_goal:
            from app.cognitive.executor import _cancel_goal

            cognition.focused_goal_id = dropped_goal
            await _cancel_goal(db, cognition)
        result = await _muse_turn(
            db,
            text=text,
            live_session_id=live_session_id,
            device_id=device_id,
            modality=modality,
            actor=actor,
            cognition=cognition,
            started=started,
        )
        await db.commit()
        return result

    if session is None:
        async with SessionLocal() as db:
            return await _run(db)
    return await _run(session)


async def _muse_turn(
    session: AsyncSession,
    *,
    text: str,
    live_session_id: str | None,
    device_id: str | None,
    modality: str,
    actor: str,
    cognition,
    started: float,
    action_receipts: list[dict[str, Any]] | None = None,
    phone_text_context: PhoneTextContext | None = None,
) -> KernelResult:
    from app.config import settings
    from app.gateway.muse_spark import muse_spark_provider

    domain = str((cognition.constraints or {}).get("turn_domain") or "open")
    has_work = has_active_work(cognition)
    compact = compact_turn(text=text, domain=domain, has_work=has_work)
    effort = reasoning_effort(domain=domain, compact=compact, has_work=has_work)
    from app.cognitive.capabilities import PHONE_LOCAL_TOOL_NAMES
    from app.device_gateway.cognitive_phone import (
        capture_phone_binding,
        execute_phone_tool,
        is_phone_turn,
        phone_turn_specs,
    )

    phone_turn = await is_phone_turn(session, device_id) if device_id else False
    phone_binding = await capture_phone_binding(
        session, device_id=str(device_id), live_session_id=live_session_id,
    ) if phone_turn else None
    if not phone_turn:
        routed = await _dispatch_kernel_code(
            session,
            text,
            cognition=cognition,
            actor=actor,
            live_session_id=live_session_id,
            modality=modality,
            steering_seen=int(cognition.steering_version),
            started=started,
        )
        if routed is not None:
            return routed
    # A phone turn sees the same semantic bus as every other surface, plus its
    # own local actuators. Restricting it to three tools made every Core or
    # Home Station capability unreachable from the device the owner actually
    # carries.
    specs = phone_turn_specs(compact=compact) if phone_turn else tool_specs_for_turn(compact=compact)
    if compact:
        telemetry.inc("compact_turns")
    telemetry.note(last_reasoning_effort=effort, last_muse_tools_offered=len(specs))

    memories: list[dict[str, Any]] = []
    if should_prefetch_memory(compact=compact):
        try:
            from app.memory.select import explicit_recall_payload

            pack = await explicit_recall_payload(session, text[:400], k=4)
            if isinstance(pack, dict):
                hits = pack.get("memories") or pack.get("items") or pack.get("hits") or []
                if isinstance(hits, list):
                    memories = [row for row in hits if isinstance(row, dict)][:6]
                elif pack.get("spoken"):
                    memories = [{"text": str(pack.get("spoken"))}]
        except Exception:
            memories = []

    from app.ev.computer_runtime import computer_prompt_state

    computer_state, computer_ready = computer_prompt_state(
        live_session_id=live_session_id, device_id=device_id
    )
    system = compile_context(
        transcript=text,
        modality=modality,
        device_id=device_id,
        cognition=cognition,
        memories=memories,
        compact=compact,
        capability_names=[spec.name for spec in specs],
        computer_state=computer_state,
        computer_ready=computer_ready,
    )
    messages = [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=text[:4000]),
    ]
    timeout = float(
        getattr(settings, "cognitive_conversation_timeout_seconds", 25.0)
        if compact
        else getattr(settings, "cognitive_work_timeout_seconds", 90.0)
    )
    max_steps = max_tool_turns(compact=compact)
    tool_count = 0
    steering_seen = int(cognition.steering_version)
    last_spoken = ""
    deadline = started + timeout

    def _in_flight() -> KernelResult:
        telemetry.inc("muse_turns")
        telemetry.note(
            last_turn_kind="muse",
            last_transcript_to_muse_ms=telemetry.timed_ms(started),
        )
        return KernelResult(
            spoken=(
                last_spoken
                or "I still have work in flight. Ask me where we are and I'll use the real task state."
            )[:2000],
            kind="muse",
            persist=bool(cognition.focused_goal_id),
            steering_version=cognition.steering_version,
            goal_id=cognition.focused_goal_id,
            latency_ms=telemetry.timed_ms(started),
            tool_calls=tool_count,
        )

    try:
        provider = muse_spark_provider()
        for _step in range(max(1, min(max_steps, 16))):
            remaining = deadline - time.perf_counter()
            if remaining <= 1.5:
                if tool_count:
                    return _in_flight()
                raise TimeoutError()
            result = await asyncio.wait_for(
                provider.chat_with_tools(
                    messages,
                    specs,
                    model=muse_spark_model(),
                    reasoning_effort=effort,
                ),
                timeout=remaining,
            )
            telemetry.inc("muse_turns")
            calls = list(result.tool_calls or [])
            if not calls:
                spoken = (result.text or "").strip() or "Okay."
                if (
                    tool_count == 0
                    and not phone_turn
                    and not cognition.prepare_only
                ):
                    routed = await _dispatch_kernel_code(
                        session,
                        text,
                        cognition=cognition,
                        actor=actor,
                        live_session_id=live_session_id,
                        modality=modality,
                        steering_seen=steering_seen,
                        started=started,
                    )
                    if routed is not None:
                        return routed
                    from app.ev.luna_code import (
                        _spoken_claims_code_write,
                        owner_asked_to_code,
                    )

                    if owner_asked_to_code(text) and _spoken_claims_code_write(spoken):
                        spoken = "I couldn't finish that coding job."
                telemetry.note(
                    last_turn_kind="muse",
                    last_transcript_to_muse_ms=telemetry.timed_ms(started),
                )
                return KernelResult(
                    spoken=spoken[:2000],
                    kind="muse",
                    persist=bool(cognition.focused_goal_id),
                    steering_version=cognition.steering_version,
                    goal_id=cognition.focused_goal_id,
                    latency_ms=telemetry.timed_ms(started),
                    tool_calls=tool_count,
                )
            telemetry.inc("muse_tool_turns")
            last_spoken = (result.text or "").strip()
            if not phone_turn and compact and any(str(call.name or "") == "capability.discover" for call in calls):
                specs = tool_specs_for_turn(compact=False, expand=True)
                compact = False
            assistant = ChatMessage(
                role="assistant",
                content=result.text or "",
                tool_calls=calls,
            )
            messages.append(assistant)
            for call in calls:
                remaining = deadline - time.perf_counter()
                if remaining <= 1.5:
                    return _in_flight()
                tool_count += 1
                telemetry.inc("muse_tool_calls")
                if phone_turn and str(call.name or "") in PHONE_LOCAL_TOOL_NAMES:
                    if int(current().steering_version) != steering_seen:
                        evidence = {"ok": False, "error": "STEERING_CHANGED", "executed": False}
                    else:
                        if phone_text_context is not None:
                            from app.device_gateway.cognitive_text import execute_phone_text_tool

                            operation = execute_phone_text_tool(
                                session, call.name, dict(call.arguments or {}),
                                context=phone_text_context, transcript=text,
                                prepare_only=bool(cognition.prepare_only),
                            )
                        else:
                            operation = execute_phone_tool(
                                session, call.name, dict(call.arguments or {}),
                                device_id=str(device_id), live_session_id=live_session_id,
                                transcript=text, prepare_only=bool(cognition.prepare_only),
                                expected_binding=phone_binding,
                            )
                        phone_evidence = await asyncio.wait_for(operation, timeout=remaining)
                        if action_receipts is not None and (
                            call.name == "phone_action" or phone_evidence.get("ok") is False
                        ):
                            action_receipts.append(phone_evidence)
                        # Action cards travel through the authenticated phone HUD,
                        # never through the language model with signed launch URLs.
                        evidence = {key: value for key, value in phone_evidence.items() if key in {
                            "ok", "error", "failure", "spoken", "reply", "executed", "verified",
                            "operation", "method", "confirmation_required", "capabilities",
                        }}
                else:
                    from app.ev.luna_code import (
                        reset_kernel_turn_deadline,
                        set_kernel_turn_deadline,
                    )

                    # A semantic tool on a phone turn reaches Core and Home
                    # Station, so it carries the same authority binding as a
                    # device-local action: the turn that opened the work is the
                    # turn that may finish it.
                    changed: str | None = None
                    if phone_turn:
                        from app.device_gateway.cognitive_phone import (
                            phone_turn_authority_changed,
                        )

                        changed = await phone_turn_authority_changed(
                            session,
                            device_id=str(device_id) if device_id else None,
                            live_session_id=live_session_id,
                            expected_binding=phone_binding,
                            text_context=phone_text_context,
                        )
                    if changed is not None:
                        evidence = {
                            "ok": False,
                            "error": changed,
                            "diagnosis": changed,
                            "executed": False,
                            "verified": False,
                            "spoken": (
                                "The phone connection changed while I was thinking. "
                                "Please ask again."
                            ),
                        }
                    else:
                        # A nested code job may take minutes; cap it to what is
                        # left of this turn so it returns a partial result instead
                        # of being cancelled after writing files.
                        deadline_token = set_kernel_turn_deadline(deadline)
                        try:
                            evidence = await asyncio.wait_for(
                                execute_semantic(
                                    session,
                                    call.name,
                                    dict(call.arguments or {}),
                                    cognition=cognition,
                                    actor=actor,
                                    live_session_id=live_session_id,
                                    steering_seen=steering_seen,
                                    device_id=str(device_id) if device_id else None,
                                ),
                                timeout=remaining,
                            )
                        finally:
                            reset_kernel_turn_deadline(deadline_token)
                    # A phone turn's failure has to reach the phone receipt too,
                    # otherwise the device sees only prose and cannot tell a real
                    # failure from a refusal.
                    if (
                        phone_turn
                        and action_receipts is not None
                        and isinstance(evidence, dict)
                        and evidence.get("ok") is False
                    ):
                        action_receipts.append(evidence)
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=dump_tool_json(evidence if isinstance(evidence, dict) else {"result": evidence}),
                        name=call.name,
                        tool_call_id=call.id,
                    )
                )
            cognition = current()
            steering_seen = int(cognition.steering_version)
        return _in_flight()
    except TimeoutError:
        if not phone_turn:
            routed = await _dispatch_kernel_code(
                session,
                text,
                cognition=cognition,
                actor=actor,
                live_session_id=live_session_id,
                modality=modality,
                steering_seen=int(cognition.steering_version),
                started=started,
            )
            if routed is not None:
                telemetry.note(last_error="turn_budget")
                return routed
        if tool_count:
            telemetry.note(last_error="turn_budget")
            return _in_flight()
        telemetry.inc("provider_timeouts")
        telemetry.inc("unavailable")
        telemetry.note(last_error="timeout")
        return KernelResult(
            spoken=_UNAVAILABLE,
            kind="unavailable",
            unavailable=True,
            latency_ms=telemetry.timed_ms(started),
            tool_calls=tool_count,
        )
    except CircuitOpenError:
        telemetry.inc("circuit_opens")
        telemetry.inc("unavailable")
        telemetry.note(last_error="circuit_open")
        return KernelResult(
            spoken=_UNAVAILABLE,
            kind="unavailable",
            unavailable=True,
            latency_ms=telemetry.timed_ms(started),
            tool_calls=tool_count,
        )
    except MuseProviderUnavailable as exc:
        telemetry.inc("provider_failures")
        telemetry.inc("unavailable")
        telemetry.note(last_error=str(exc)[:160] or "muse_unavailable")
        return KernelResult(
            spoken=_UNAVAILABLE,
            kind="unavailable",
            unavailable=True,
            latency_ms=telemetry.timed_ms(started),
            tool_calls=tool_count,
        )
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            telemetry.inc("provider_429")
        telemetry.inc("provider_failures")
        telemetry.inc("unavailable")
        telemetry.note(last_error=type(exc).__name__)
        return KernelResult(
            spoken=_UNAVAILABLE,
            kind="unavailable",
            unavailable=True,
            latency_ms=telemetry.timed_ms(started),
            tool_calls=tool_count,
        )


async def handle_turn_maybe_remote(**kwargs: Any) -> KernelResult:
    """Run the mind on this process.

    Live Talk used to POST every utterance to :8000. That kernel keeps a
    CognitiveSession in RAM, so closing Evie still replayed yesterday's
    leftover file GoalContract. Talk already has Mac hands and this tree's
    turn-start release. The current utterance is the live job here.
    """

    return await handle_turn(**kwargs)
