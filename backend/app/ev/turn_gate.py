"""Authoritative Turn Gate (G1.4+) — backend owns response creation.

Every final owner turn passes through one gate. No Realtime stateful bypass.
Exactly one assistant response per turn, idempotent.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.owner_turn import OwnerTurn, is_consumed, mark_consumed
from app.ev.turn_controller import TurnController
from app.ev.turn_intent import TurnResult

logger = logging.getLogger("ev.turn_gate")

# In-memory gate state for exactly-once (bounded)
_GATE_DECISIONS: dict[str, TurnResult] = {}  # turn_id -> TurnResult

# PART 22/23 — transaction observability. Safe counters only; no SQL, no
# owner content. Health surfaces these so a poisoned executor is visible.
_DB_FAILURE_STATS: dict[str, Any] = {
    "owner_turn_db_failures": 0,
    "owner_turn_rollbacks": 0,
    "owner_turn_commit_failures": 0,
    "last_owner_turn_db_failure_at": None,
    "last_owner_turn_db_failure_code": None,
}


def db_failure_stats() -> dict[str, Any]:
    return dict(_DB_FAILURE_STATS)


def _note_db_failure(code: str) -> None:
    from app.utils.text import utcnow

    _DB_FAILURE_STATS["owner_turn_db_failures"] = (
        int(_DB_FAILURE_STATS["owner_turn_db_failures"]) + 1
    )
    _DB_FAILURE_STATS["last_owner_turn_db_failure_at"] = (
        utcnow().isoformat()
    )
    _DB_FAILURE_STATS["last_owner_turn_db_failure_code"] = str(code)[:64]


_RAW_DB_MARKERS = (
    "route_failed:",
    "psycopg",
    "sqlalchemy",
    "InFailedSqlTransaction",
    "PendingRollbackError",
    "StatementError",
    "DBAPIError",
    "current transaction is aborted",
)


def _is_raw_db_failure(result: TurnResult) -> bool:
    """True when a TurnResult carries raw database internals as its error."""
    if result.ok:
        return False
    blob = str(result.error or "")
    return any(marker in blob for marker in _RAW_DB_MARKERS)


def _canonical_db_failure(error: Exception) -> TurnResult:
    """PART 17/18: the owner never sees raw database internals."""
    _note_db_failure(type(error).__name__)
    return TurnResult(
        ok=False,
        route="UNSUPPORTED",
        operation="UNKNOWN",
        error="OWNER_TURN_FAILED",
        owner_message=(
            "I couldn't complete that because an internal state operation "
            "failed. Your request was safely cancelled."
        ),
    )


async def handle_owner_turn(
    session: AsyncSession,
    owner_turn: OwnerTurn,
) -> TurnResult:
    """Gate entry: final canonical OwnerTurn → TurnController → TurnResult.

    Ensures exactly-once: if turn_id already consumed, returns cached decision
    without re-executing and without creating a duplicate response.
    """
    if is_consumed(owner_turn.turn_id):
        cached = _GATE_DECISIONS.get(owner_turn.turn_id)
        if cached:
            logger.warning(
                "turn_gate duplicate turn_id=%s already consumed, returning cached",
                owner_turn.turn_id,
            )
            return cached
        # Already consumed but no cached result (should not happen) — return degraded
        return TurnResult(ok=False, route="UNSUPPORTED", operation="UNKNOWN", error="duplicate_turn")

    # Mark consumed before execution to prevent race (optimistic)
    mark_consumed(owner_turn.turn_id)

    controller = TurnController(
        session,
        actor=owner_turn.owner_id,
        device_id=owner_turn.device_id,
        session_id=owner_turn.live_session_id,
    )
    # Bounded context already handled inside controller
    # TRANSACTION OWNERSHIP LAW (PART 4/7/8): one owner turn owns one clear
    # transaction boundary. Any exception is rolled back IMMEDIATELY and
    # converted to a canonical failure — the session must be healthy for the
    # next owner turn, and raw database text must never reach the model.
    # (F1 shadow-memory attachment runs after this boundary below; it may only
    # DOWNGRADE a read-only historical question from STATE_QUERY to
    # CONVERSATION. It never mutates canonical state.)
    try:
        result = await controller.handle_turn(owner_turn.transcript, turn_id=owner_turn.turn_id)
    except Exception as exc:  # noqa: BLE001 - canonical conversion point
        # (asyncio.CancelledError derives from BaseException and is NOT
        # swallowed here — cancellation always propagates.)
        with contextlib.suppress(Exception):
            await session.rollback()
            _DB_FAILURE_STATS["owner_turn_rollbacks"] = (
                int(_DB_FAILURE_STATS["owner_turn_rollbacks"]) + 1
            )
        result = _canonical_db_failure(exc)

    # PART 4/8/18: handle_turn converts internal exceptions into
    # route_failed results whose text may contain RAW DATABASE INTERNALS,
    # and the underlying transaction may be left ABORTED. Detect either
    # condition, roll back immediately, and replace with the canonical
    # owner-safe failure. The session stays healthy for the next turn.
    if not result.ok and _is_raw_db_failure(result):
        with contextlib.suppress(Exception):
            await session.rollback()
            _DB_FAILURE_STATS["owner_turn_rollbacks"] = (
                int(_DB_FAILURE_STATS["owner_turn_rollbacks"]) + 1
            )
        result = _canonical_db_failure(
            RuntimeError(str(result.error).split("route_failed:")[-1].strip()[:120])
        )

    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await session.rollback()
        _note_db_failure(type(exc).__name__)
        _DB_FAILURE_STATS["owner_turn_commit_failures"] = (
            int(_DB_FAILURE_STATS["owner_turn_commit_failures"]) + 1
        )

    # F1 shadow memory: attach turn-scoped recalled history when the memory
    # gate is ON and the turn qualitatively deserves history. Runs AFTER the
    # transaction boundary; read-only; never touches mutation routes; the
    # envelope is exactly-once per turn and never persisted. Attach BEFORE
    # the idempotency cache so a retried turn returns the identical payload.
    with contextlib.suppress(Exception):
        result = await _maybe_attach_shadow_context(session, owner_turn, result)

    # Cache decision for idempotency
    _GATE_DECISIONS[owner_turn.turn_id] = result
    # Bounded cache
    if len(_GATE_DECISIONS) > 500:
        oldest = next(iter(_GATE_DECISIONS))
        del _GATE_DECISIONS[oldest]

    logger.warning(
        "turn_gate decision turn_id=%s provider_item=%s route=%s op=%s ok=%s latency=%.1fms",
        owner_turn.turn_id,
        owner_turn.provider_item_id,
        result.route,
        result.operation,
        result.ok,
        result.latency_ms or 0,
    )
    return result


async def _owner_memory_scope(session: AsyncSession, owner_turn: OwnerTurn) -> str:
    """Server-derived memory scope for the turn's device (fail closed)."""

    if not owner_turn.device_id:
        return "owner"
    try:
        from uuid import UUID as _UUID

        from app.models import Device

        device_id = (
            owner_turn.device_id
            if isinstance(owner_turn.device_id, _UUID)
            else _UUID(str(owner_turn.device_id))
        )
        row = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalars().first()
        if row is None or row.revoked_at is not None:
            return "untrusted"
        return str(getattr(row, "memory_scope", "") or "owner").strip().lower() or "owner"
    except Exception:  # noqa: BLE001 - scope resolution must fail closed
        return "untrusted"


async def _maybe_attach_shadow_context(
    session: AsyncSession,
    owner_turn: OwnerTurn,
    result: TurnResult,
) -> TurnResult:
    """Attach [EVIE_RECALLED_HISTORY] to conversation turns when gate=ON.

    Rules (F0+F1 directive):
      - OFF: zero work. SHADOW: route_turn measures only; nothing attaches.
      - ON + CONVERSATION: attach recalled history for this turn.
      - ON + STATE_QUERY + historical-truth question ("what WAS the priority
        originally?"): downgrade to CONVERSATION and answer from history —
        never present retrieved memory as current canonical state.
      - ON + STATE_QUERY (current-state): NO history; canonical answer stands.
    """

    from app.memory.foundation import LEVEL_TOKEN_BUDGETS
    from app.memory.intent import classify_retrieval
    from app.memory.shadow import mark_injected, memory_gate_mode, route_turn

    mode = memory_gate_mode()
    if mode == "off":
        return result
    if result.route not in {"CONVERSATION", "STATE_QUERY"}:
        return result

    classification = classify_retrieval(owner_turn.transcript)
    if classification.is_current_state_guard and not classification.historical_truth:
        return result

    memory_scope = await _owner_memory_scope(session, owner_turn)
    envelope = await route_turn(
        session,
        query=owner_turn.transcript,
        turn_id=owner_turn.turn_id,
        live_session_id=owner_turn.live_session_id,
        memory_scope=memory_scope,
        classification=classification,
    )
    if envelope is None or envelope.injected or envelope.expired:
        return result
    if mode == "shadow":
        return result

    downgrade = (
        result.route == "STATE_QUERY"
        and result.ok
        and classification.historical_truth
        and bool(envelope.items)
    )
    if result.route == "STATE_QUERY" and not downgrade:
        return result
    if not downgrade and result.route != "CONVERSATION":
        return result

    if not mark_injected(envelope):
        return result
    block = envelope.render(
        budget_tokens=LEVEL_TOKEN_BUDGETS.get(envelope.level, 300)
    )
    if not block:
        return result
    shadow_payload = {
        "block": block,
        "intent": envelope.retrieval_intent.value,
        "level": envelope.level,
        "tokens": envelope.token_count,
        "fingerprint": envelope.query_fingerprint,
        "turn_scoped": True,
    }
    if downgrade:
        return result.model_copy(
            update={
                "route": "CONVERSATION",
                "operation": "UNKNOWN",
                "canonical_data": None,
                "owner_message": None,
                "shadow_context": shadow_payload,
            }
        )
    return result.model_copy(update={"shadow_context": shadow_payload})


def create_realtime_response_payload(owner_turn: OwnerTurn, turn_result: TurnResult) -> dict[str, Any]:
    """Create exactly ONE GA-valid response.create payload for the TurnGate.

    Single builder for all four shapes (conversation / clarification /
    failure / state-result). The session owns audio output configuration;
    response.create carries only authorization + instructions. The
    `response.modalities` field is NOT part of the current Realtime
    client-event schema (owner-proven: Unknown parameter 'response.modalities')
    and must never be sent here.
    """

    def _envelope(instructions: str) -> dict[str, Any]:
        return {
            "type": "response.create",
            "response": {"instructions": instructions},
        }

    # Conversation: let Realtime answer the committed owner item naturally.
    # F1: turn-scoped recalled history rides along, clearly labeled as
    # historical background — never as canonical state or owner instruction.
    if turn_result.route == "CONVERSATION":
        instructions = "Respond naturally to the user's conversational turn."
        shadow = turn_result.shadow_context if isinstance(turn_result.shadow_context, dict) else None
        block = str((shadow or {}).get("block") or "").strip()
        if block:
            instructions = (
                f"{instructions}\n{block}\n"
                "The bracketed history above is background only: use it if the "
                "user's turn refers to the past, otherwise ignore it. Current "
                "canonical state always outranks it."
            )
        return _envelope(instructions)

    # Clarification: ask exactly one question, no state mutation occurred.
    if turn_result.needs_clarification or turn_result.route == "CLARIFICATION":
        q = turn_result.clarification_question or turn_result.owner_message or "Which one?"
        return _envelope(f"Ask the user for clarification: {q}")

    # Failure path: the OWNER hears the canonical Core reason. Realtime may
    # reword it naturally, but it may NEVER convert a Core execution failure
    # into a claim about Evie's abilities ("I can't cancel commitments") —
    # capability self-assessment by the model is forbidden (capability law).
    if not turn_result.ok:
        canonical = (turn_result.owner_message or "").strip() or (
            f"I couldn't complete that ({turn_result.error})."
        )
        return _envelope(
            "Evie attempted this action in her backend and it did not succeed. "
            f"Canonical result: \"{canonical}\" "
            "Say exactly this meaning; you may reword lightly. "
            "NEVER claim Evie lacks the ability, lacks a tool, or cannot "
            "handle this kind of request. Cite only the given reason."
        )

    # State/action success: voice the authoritative facts only. The action
    # ALREADY happened in Evie's backend — the model must not re-interpret
    # success as inability or hedge about capability.
    facts = ""
    if turn_result.canonical_data:
        import json

        try:
            facts = json.dumps(turn_result.canonical_data, default=str)[:2000]
        except Exception:
            facts = str(turn_result.canonical_data)[:2000]
    msg = turn_result.owner_message or ""
    return _envelope(
        f"Answer using ONLY these canonical facts; do not contradict them and do not call other tools for this: {msg} Facts: {facts} "
        "The action already succeeded in Evie's backend — never claim she cannot perform it."
    )


# Config flag for gate canary
def is_gate_enabled() -> bool:
    from app.config import settings

    return bool(getattr(settings, "turn_gate_enabled", False))
