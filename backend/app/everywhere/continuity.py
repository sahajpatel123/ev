"""G2 Phase 7 — Conversation continuity foundation.

Logical thread continuation, NOT live socket migration. A conversation that
started on the Mac resumes on the phone from: the canonical thread, its
ephemeral state (focus/topics/pending questions), the live pending offer Evie
actually just spoke, the durable rollup, and active Project/Goal references —
bounded, never a giant transcript dump.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.everywhere.owner import owner_scope
from app.life import service as life
from app.models import ConversationRollup, ConversationState, ConversationThread
from app.utils.text import utcnow

MAX_TOPICS = 8
MAX_OPEN_QUESTIONS = 5
MAX_DECISIONS = 5
SUMMARY_CHARS = 1200


def _live_pending_offer() -> dict | None:
    """The offer Evie actually just made, so a resumed "yes" has a referent.

    ``ConversationState.pending_questions`` is the durable ask queue; it does
    not carry the question Evie just spoke. The live offer does, and it is
    process-shared through the cognitive session file. Fail open: an
    unreadable session is no offer, never an error.
    """

    try:
        from app.cognitive import intent, session_store

        return intent.pending_offer(session_store.current())
    except Exception:
        return None


async def resume_context(
    session: AsyncSession,
    *,
    actor: str,
    device_name: str | None = None,
    thread_id: str | None = None,
    device=None,
) -> dict:
    scope = owner_scope(actor, device=device)
    thread: ConversationThread | None = None
    if thread_id:
        try:
            thread = await session.get(ConversationThread, UUID(thread_id))
        except (ValueError, TypeError):
            thread = None
        if thread is None:
            return {"ok": False, "error": "thread_not_found"}
    else:
        row = (
            (
                await session.execute(
                    select(ConversationThread).where(ConversationThread.is_default.is_(True)).limit(1)
                )
            )
            .scalars()
            .first()
        )
        thread = row

    state: ConversationState | None = None
    rollup: ConversationRollup | None = None
    if thread is not None:
        state = (
            await session.get(ConversationState, thread.id)
            if hasattr(ConversationState, "thread_id")
            else None
        )
        rollup = (
            (
                await session.execute(
                    select(ConversationRollup).where(ConversationRollup.thread_id == thread.id).limit(1)
                )
            )
            .scalars()
            .first()
        )

    situation = await life.situation_snapshot(session, actor=scope)
    active_goals = (situation.get("active_goals") or [])[:3]
    top_focus = situation.get("top_focus")

    last_activity = None
    if thread is not None and thread.updated_at is not None:
        last_activity = thread.updated_at.isoformat()

    summary = (rollup.summary or "")[:SUMMARY_CHARS] if rollup else ""
    open_questions = (rollup.open_questions or [])[:MAX_OPEN_QUESTIONS] if rollup else []
    decisions = (rollup.decisions or [])[:MAX_DECISIONS] if rollup else []

    parts: list[str] = []
    if top_focus:
        parts.append(f"Current top project: {top_focus['title']}.")
    for g in active_goals:
        nxt = f" Next action: {g['next_action']}." if g.get("next_action") else ""
        parts.append(f"Active goal: {g['title']}.{nxt}")
    if summary:
        parts.append(f"Recent arc: {summary[:400]}")
    resume_hint = " ".join(parts) if parts else "No active context found."

    payload = {
        "ok": True,
        "thread_id": str(thread.id) if thread is not None else None,
        "last_activity": last_activity,
        "requested_from_device": device_name,
        "focus": state.focus if state else None,
        "recent_topics": list((state.recent_topics or [])[:MAX_TOPICS]) if state else [],
        "pending_questions": list((state.pending_questions or [])[:MAX_OPEN_QUESTIONS]) if state else [],
        "rollup": {
            "summary": summary,
            "open_questions": open_questions,
            "decisions": decisions,
            "covered_turn_count": rollup.covered_turn_count if rollup else 0,
        },
        "situation_refs": {
            "top_project": top_focus,
            "active_goals": active_goals,
        },
        "resume_hint": resume_hint,
        "generated_at": utcnow().isoformat(),
    }
    # Additive key: the question Evie just asked, which pending_questions does
    # not carry. Omitted when there is no live offer so the payload is
    # otherwise byte-identical.
    offer = _live_pending_offer()
    if offer:
        payload["pending_offer"] = offer
    return payload
