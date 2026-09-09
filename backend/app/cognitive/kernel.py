"""Cognitive Kernel — OwnerTurn → reflex or Muse. No second general mind."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.cognitive import telemetry
from app.cognitive.capabilities import tool_specs
from app.cognitive.context import compile_context
from app.cognitive.executor import dump_tool_json, execute_semantic
from app.cognitive.mode import kernel_url
from app.cognitive.reflex import match_reflex
from app.cognitive.session_store import (
    bind_live,
    bump_steering,
    current,
    has_active_work,
    save,
    status_line,
)
from app.contracts import ChatMessage
from app.gateway.muse import MuseProviderUnavailable, muse_spark_key_loaded, muse_spark_model
from app.gateway.reliability import CircuitOpenError


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


async def handle_turn(
    *,
    transcript: str,
    live_session_id: str | None = None,
    device_id: str | None = None,
    modality: str = "voice",
    session: AsyncSession | None = None,
    actor: str = "master",
) -> KernelResult:
    started = time.perf_counter()
    text = (transcript or "").strip()
    cognition = bind_live(live_session_id)
    reflex = match_reflex(
        text,
        has_active_goal=has_active_work(cognition),
        status_line=status_line(cognition),
    )
    if reflex is not None:
        telemetry.inc("deterministic_reflex_turns")
        if reflex.cancel_work:
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
) -> KernelResult:
    from app.config import settings
    from app.gateway.muse_spark import muse_spark_provider

    memories: list[dict[str, Any]] = []
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

    from app.cognitive.artifact import begin_owner_turn

    begin_owner_turn(cognition, text)
    system = compile_context(
        transcript=text,
        modality=modality,
        device_id=device_id,
        cognition=cognition,
        memories=memories,
    )
    messages = [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=text[:4000]),
    ]
    specs = tool_specs()
    short = len(text.split()) < 10 and not has_active_work(cognition)
    timeout = float(
        getattr(settings, "cognitive_conversation_timeout_seconds", 25.0)
        if short
        else getattr(settings, "cognitive_work_timeout_seconds", 90.0)
    )
    max_steps = int(getattr(settings, "cognitive_max_tool_turns", 12) or 12)
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
                provider.chat_with_tools(messages, specs, model=muse_spark_model()),
                timeout=remaining,
            )
            telemetry.inc("muse_turns")
            calls = list(result.tool_calls or [])
            if not calls:
                spoken = (result.text or "").strip() or "Okay."
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
                evidence = await asyncio.wait_for(
                    execute_semantic(
                        session,
                        call.name,
                        dict(call.arguments or {}),
                        cognition=cognition,
                        actor=actor,
                        live_session_id=live_session_id,
                        steering_seen=steering_seen,
                    ),
                    timeout=remaining,
                )
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
    """Voice edge posts to :8000; kernel runs locally.

    If the kernel process is unreachable, Spark still decides on this Talk
    process so first-try look/keep are not stranded on a dead HTTP hop.
    """

    from app.cognitive.mode import is_voice_edge

    if is_voice_edge():
        from app.cognitive.edge import post_turn

        try:
            return await post_turn(kernel_url(), **kwargs)
        except Exception as exc:
            telemetry.note(last_error=f"kernel_post:{type(exc).__name__}")
            return await handle_turn(**kwargs)
    return await handle_turn(**kwargs)
