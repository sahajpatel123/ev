"""Owner distill path: owner-fact candidates -> owner store (or ledger).

Modes (EV_OWNER_DISTILL_MODE): off | shadow | on.
  off    — owner candidates are dropped; the memory pipeline is untouched.
  shadow — decisions are computed + ledgered via memory_trace; nothing is written.
  on     — surviving candidates are written as versioned owner rows.

SECRET LAW and ASSISTANT-STATEMENT LAW apply on every mode that inspects
candidates: credential-shaped or assistant-speculated content never becomes
an owner row, even in ON mode.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory.candidates import Decision, evaluate_candidate
from app.memory.observe import log_memory


def distill_mode() -> str:
    return (getattr(settings, "owner_distill_mode", "shadow") or "shadow").strip().lower()


async def distill_owner_candidates(
    session: AsyncSession,
    event: Any,
    candidates: list,
) -> dict:
    """Route owner-fact candidates per EV_OWNER_DISTILL_MODE. Returns counts."""
    mode = distill_mode()
    summary: dict[str, Any] = {
        "mode": mode,
        "seen": len(candidates),
        "written": 0,
        "dropped_secret": 0,
        "dropped_assistant": 0,
    }
    if mode == "off" or not candidates:
        return summary

    survivors = []
    for candidate in candidates:
        decision = evaluate_candidate(event, candidate)
        if decision.decision == Decision.REJECT_SECRET:
            summary["dropped_secret"] += 1
            continue
        if decision.decision == Decision.REJECT_ASSISTANT_SPECULATION:
            summary["dropped_assistant"] += 1
            continue
        survivors.append(candidate)

    log_memory(
        "owner.distill_shadow",
        extra={
            "event_id": str(getattr(event, "id", "")),
            "mode": mode,
            "seen": summary["seen"],
            "surviving": len(survivors),
            "dropped_secret": summary["dropped_secret"],
            "dropped_assistant": summary["dropped_assistant"],
        },
    )
    if mode != "on":
        return summary

    from app.ev.owner_model import write_owner_row

    event_id: UUID = event.id
    for candidate in survivors:
        await write_owner_row(
            session,
            candidate.owner_kind,
            candidate.text,
            source_event_id=event_id,
            importance=candidate.importance,
            confidence=candidate.confidence,
            source_type=candidate.source_type,
            privacy_level=candidate.privacy_level,
            event_time=candidate.event_time,
            payload={"topic": (candidate.payload or {}).get("topic")},
        )
        summary["written"] += 1
    return summary
