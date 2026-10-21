"""One-brain facade: every conversational entrypoint funnels through here.

Two modes, one funnel:

- text mode (the live brain): the entrypoint's utterance reaches the MiMo
  kernel (D7 default) with fabric tools registered. When EV_FABRIC=0 the
  facade is a transparent passthrough to ``handle_turn`` — legacy behavior
  byte-identical (golden-tested, S24).
- ops mode (explicit lane plans): delegates to the sync ``run_turn``
  ops dispatcher in a worker thread so the event loop never blocks.

Every fabric-on turn is recorded in :mod:`app.fabric.trace` (turn id,
entrypoint, lane spans) so route-trace tests and status endpoints can
prove the fabric was in the path. Panic (D10) denies text-mode turns
before the kernel runs; ops-mode effect lanes check panic themselves.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.cognitive.kernel import KernelResult, handle_turn

from . import trace

_PANIC_SPOKEN = (
    "Evie is paused by the owner panic switch. Effects are denied until "
    "the owner re-enables them."
)


def fabric_enabled() -> bool:
    from app.config import settings

    return bool(getattr(settings, "fabric", True))


async def arun_kernel_turn(
    *,
    transcript: str,
    session=None,
    actor: str = "master",
    device_id: str | None = None,
    modality: str = "typed",
    live_session_id: str | None = None,
    entrypoint: str,
    phone_text_context=None,
) -> KernelResult:
    """Kernel-backed turn with fabric spans + panic gate. Typed core of arun_turn."""
    # Typed chat already wrote the user event. Recording it again would
    # commit the chat session in the middle of the pipeline.
    persist_memory = entrypoint != "v1_chat_pipeline"
    if not fabric_enabled():
        return await handle_turn(
            transcript=transcript,
            live_session_id=live_session_id,
            device_id=device_id,
            modality=modality,
            session=session,
            actor=actor,
            phone_text_context=phone_text_context,
            persist_memory=persist_memory,
        )
    from .panic import panicked

    if panicked():
        return KernelResult(spoken=_PANIC_SPOKEN, kind="denied", unavailable=True)
    with trace.turn(entrypoint, transcript):
        return await handle_turn(
            transcript=transcript,
            live_session_id=live_session_id,
            device_id=device_id,
            modality=modality,
            session=session,
            actor=actor,
            phone_text_context=phone_text_context,
            persist_memory=persist_memory,
        )


async def arun_turn(
    prompt: str,
    *,
    entrypoint: str,
    session=None,
    actor: str = "master",
    device_id: str | None = None,
    modality: str = "typed",
    live_session_id: str | None = None,
    phone_text_context=None,
    ops: list[dict] | None = None,
    root=None,
    provenance: str = "owner_turn",
    turn_id: str | None = None,
    cancel=None,
    **kw: Any,
) -> dict[str, Any]:
    """Single facade. ``ops`` selects explicit-lane mode; otherwise the kernel."""
    from .run_turn import run_turn

    if ops is not None:
        if not fabric_enabled():
            return {
                "ok": False, "verified": True, "evidence": {},
                "error": "fabric disabled (EV_FABRIC=0)", "undo": None,
            }
        from .panic import panicked

        if panicked():
            return {
                "ok": False, "verified": True, "evidence": {},
                "error": "denied: owner panic switch is engaged", "undo": None,
            }
        import uuid as _uuid

        from .cancel import new_token

        tid = turn_id or f"ft-{_uuid.uuid4().hex[:12]}"
        token = cancel or new_token(tid)
        with trace.turn(entrypoint, prompt, turn_id=tid):
            receipt = await asyncio.to_thread(
                run_turn, prompt, root=root, ops=ops, provenance=provenance,
                cancel=token, **kw
            )
        receipt["fabric_turn_id"] = tid
        from .audit import record as audit_record

        audit_record("turn", entrypoint, prompt[:200],
                     "EXECUTED" if receipt.get("ok") else "FAILED",
                     bool(receipt.get("ok")), turn_id=tid)
        return receipt
    result = await arun_kernel_turn(
        transcript=prompt,
        session=session,
        actor=actor,
        device_id=device_id,
        modality=modality,
        live_session_id=live_session_id,
        entrypoint=entrypoint,
        phone_text_context=phone_text_context,
    )
    recent = trace.recent_turn(entrypoint)
    turn_id = recent[0] if recent else None
    ok = not result.unavailable and result.kind not in {"failed", "denied", "capped"}
    return {
        "ok": ok,
        "verified": True,
        "evidence": {"kind": result.kind, "tool_calls": result.tool_calls,
                     "spans": len(recent[1]) if recent else 0},
        "error": None if ok else result.spoken[:300],
        "undo": None,
        "spoken": result.spoken,
        "kind": result.kind,
        "fabric_turn_id": turn_id,
    }
