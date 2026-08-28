"""Operations API: aggregate health, latency, and cost-budget metrics."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_master
from app.db import get_session
from app.ops.metrics import collect_metrics_with_warnings

router = APIRouter(prefix="/v1/ops", tags=["ops"])


@router.get("/metrics")
async def ops_metrics(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> dict:
    """Aggregate model-call latency/error/cost metrics against budgets."""

    return await collect_metrics_with_warnings(session)


@router.post("/memory-router/probe")
async def memory_router_probe(
    body: dict,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> dict:
    """F0+F1 canary instrument (read-only, master-key only).

    Runs the memory router boundary exactly as a real owner turn would and
    returns BOUNDED metadata: ids, counts, latencies, hashes. Never returns
    memory text. Never injects into any live session. When ``run_gate`` is
    true, additionally exercises the TurnGate seam on a transient OwnerTurn
    (canonical reads only; nothing is sent anywhere). Respects
    ``EV_MEMORY_GATE``: off → reports zero work, which doubles as the
    rollback-proof instrument.
    """

    import time
    from uuid import uuid4

    from app.memory.foundation import LEVEL_TOKEN_BUDGETS
    from app.memory.intent import classify_retrieval
    from app.memory.shadow import memory_gate_mode, route_turn

    query = str((body or {}).get("query") or "").strip()
    if not query or len(query) > 400:
        return {"ok": False, "error": "invalid_query"}
    scope = str((body or {}).get("scope") or "owner").strip().lower() or "owner"
    turn_id = str((body or {}).get("turn_id") or "").strip() or f"probe-{uuid4().hex}"
    run_gate = bool((body or {}).get("run_gate")) and scope == "owner"

    mode = memory_gate_mode()
    started = time.perf_counter()
    classification = classify_retrieval(query)

    out: dict = {
        "ok": True,
        "mode": mode,
        "turn_id": turn_id,
        "query_fingerprint": None,
        "intent": classification.intent.value,
        "level": classification.level,
        "current_state_guard": classification.is_current_state_guard,
        "historical_truth": classification.historical_truth,
        "retrieval_triggered": False,
        "scope_denied": False,
        "retrieval_ms": None,
        "candidates": 0,
        "selected": 0,
        "shadow_tokens": 0,
        "item_refs": [],
    }

    if mode == "off" or classification.level <= 0 or classification.is_current_state_guard:
        out["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
        if scope != "owner" and mode != "off":
            out["scope_denied"] = True
        return out

    envelope = await route_turn(
        session,
        query=query,
        turn_id=turn_id,
        live_session_id="ops-probe",
        memory_scope=scope,
        classification=classification,
    )
    if envelope is None:
        out["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return out

    out.update(
        {
            "retrieval_triggered": True,
            "query_fingerprint": envelope.query_fingerprint,
            "level": envelope.level,
            "scope_denied": scope != "owner",
            "retrieval_ms": envelope.diagnosis.get("retrieval_ms"),
            "candidates": envelope.diagnosis.get("candidates", 0),
            "selected": len(envelope.items),
            "shadow_tokens": envelope.token_count,
            "budget": LEVEL_TOKEN_BUDGETS.get(envelope.level),
            "escalated": envelope.diagnosis.get("escalated", False),
            "item_refs": [
                {
                    "ref": item.ref,
                    "kind": item.kind,
                    "type": item.memory_type,
                    "score": item.score,
                }
                for item in envelope.items[:8]
            ],
        }
    )

    if (body or {}).get("prospective"):
        from app.memory.prospective import (
            is_prospective_question,
            prospective_mode,
        )

        pmode = prospective_mode()
        pctx = None
        if pmode in {"shadow", "on"} and is_prospective_question(query):
            from app.memory.prospective import build_prospective_context

            pctx = await build_prospective_context(session)
        out["prospective"] = {
            "mode": pmode,
            "is_prospective": is_prospective_question(query),
            "required": [i.to_dict() for i in (pctx.required if pctx else [])][:6],
            "planned": [i.to_dict() for i in (pctx.planned if pctx else [])][:6],
            "suggested": [i.to_dict() for i in (pctx.suggested if pctx else [])][:6],
            "conflicts": (pctx.conflicts if pctx else [])[:4],
            "sourceless_suggestions": sum(
                1 for i in (pctx.suggested if pctx else []) if not i.source_refs
            ),
        }
        return out

    if run_gate:
        from app.ev.owner_turn import create_owner_turn
        from app.ev.turn_gate import create_realtime_response_payload, handle_owner_turn
        from app.utils.text import utcnow

        gate_started = time.perf_counter()
        turn = create_owner_turn(
            live_session_id="ops-probe",
            provider_item_id=None,
            owner_id="master",
            device_id=None,
            transcript=query,
            transcript_source="text",
            committed_at=utcnow(),
            transcription_completed_at=utcnow(),
            turn_id=turn_id,
        )
        result = await handle_owner_turn(session, turn)
        payload = create_realtime_response_payload(turn, result)
        instructions = str((payload.get("response") or {}).get("instructions") or "")
        block = result.shadow_context.get("block") if isinstance(result.shadow_context, dict) else ""
        out["gate"] = {
            "route": result.route,
            "operation": result.operation,
            "ok": result.ok,
            "shadow_attached": bool(result.shadow_context),
            "block_tokens": (len(block.split()) * 4 // 3) if block else 0,
            "labeled_block_in_payload": "[EVIE_RECALLED_HISTORY]" in instructions,
            "gate_ms": round((time.perf_counter() - gate_started) * 1000, 2),
        }
    out["total_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return out
