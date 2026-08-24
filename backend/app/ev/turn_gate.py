"""Authoritative Turn Gate (G1.4+) — backend owns response creation.

Every final owner turn passes through one gate. No Realtime stateful bypass.
Exactly one assistant response per turn, idempotent.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.owner_turn import OwnerTurn, is_consumed, mark_consumed
from app.ev.turn_controller import TurnController
from app.ev.turn_intent import TurnResult

logger = logging.getLogger("ev.turn_gate")

# In-memory gate state for exactly-once (bounded)
_GATE_DECISIONS: dict[str, TurnResult] = {}  # turn_id -> TurnResult


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
    result = await controller.handle_turn(owner_turn.transcript, turn_id=owner_turn.turn_id)

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
    if turn_result.route == "CONVERSATION":
        return _envelope("Respond naturally to the user's conversational turn.")

    # Clarification: ask exactly one question, no state mutation occurred.
    if turn_result.needs_clarification or turn_result.route == "CLARIFICATION":
        q = turn_result.clarification_question or turn_result.owner_message or "Which one?"
        return _envelope(f"Ask the user for clarification: {q}")

    # Failure path: honest report, never a false success claim.
    if not turn_result.ok:
        msg = turn_result.error or "I couldn't do that."
        return _envelope(
            f"The previous request failed: {msg}. Tell the user what happened and what they can try next. Do not claim success."
        )

    # State/action success: voice the authoritative facts only.
    facts = ""
    if turn_result.canonical_data:
        import json

        try:
            facts = json.dumps(turn_result.canonical_data, default=str)[:2000]
        except Exception:
            facts = str(turn_result.canonical_data)[:2000]
    msg = turn_result.owner_message or ""
    return _envelope(
        f"Answer using ONLY these canonical facts; do not contradict them and do not call other tools for this: {msg} Facts: {facts}"
    )


# Config flag for gate canary
def is_gate_enabled() -> bool:
    from app.config import settings

    return bool(getattr(settings, "turn_gate_enabled", False))
