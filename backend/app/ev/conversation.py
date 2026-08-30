"""Single continuous conversation: one lifelong thread, ephemeral state on top."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConversationState, ConversationThread, Event
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.utils.text import utcnow

DEFAULT_TITLE = "EV — continuous conversation"


async def get_default_thread(session: AsyncSession) -> ConversationThread:
    existing = await _find_default_thread(session)
    if existing is not None:
        return existing
    candidate = ConversationThread(title=DEFAULT_TITLE, is_default=True)
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
    except IntegrityError:
        # Another request created the default thread first; the partial unique
        # index guarantees exactly one, so adopt the winner.
        existing = await _find_default_thread(session)
        if existing is None:
            raise
        return existing
    return candidate


async def _find_default_thread(session: AsyncSession) -> ConversationThread | None:
    result = await session.execute(
        select(ConversationThread)
        .where(ConversationThread.is_default.is_(True))
        .order_by(ConversationThread.created_at.asc())
        .limit(1)
    )
    return result.scalars().first()


async def resolve_thread(
    session: AsyncSession,
    conversation_id: UUID | None,
) -> ConversationThread:
    """Explicit id wins; otherwise the one default lifelong window."""
    if conversation_id is not None:
        thread = await session.get(ConversationThread, conversation_id)
        if thread is None:
            raise KeyError(f"Conversation {conversation_id} not found")
        return thread
    return await get_default_thread(session)


async def get_or_create_state(session: AsyncSession, thread_id: UUID) -> ConversationState:
    state = await session.get(ConversationState, thread_id)
    if state is not None:
        return state
    state = ConversationState(thread_id=thread_id)
    session.add(state)
    await session.flush()
    return state


async def update_state(
    session: AsyncSession,
    thread_id: UUID,
    *,
    focus: str | None = None,
    topics: list[str] | None = None,
    pending_questions: list[str] | None = None,
    working_context: dict | None = None,
) -> ConversationState:
    state = await get_or_create_state(session, thread_id)
    if focus is not None:
        state.focus = focus
    if topics is not None:
        merged = list(dict.fromkeys([*topics, *(state.recent_topics or [])]))[:10]
        state.recent_topics = merged
    if pending_questions is not None:
        merged = list(dict.fromkeys([*pending_questions, *(state.pending_questions or [])]))[:5]
        state.pending_questions = merged
    if working_context is not None:
        state.working_context = {**(state.working_context or {}), **working_context}
    state.updated_at = utcnow()
    return state


async def history(
    session: AsyncSession,
    thread_id: UUID,
    *,
    limit: int = 50,
    access: str = "user",
) -> list[Event]:
    """The most recent ``limit`` message events in a thread, oldest first.

    ``access="model"`` enforces the payload boundary here too: conversation
    history is assembled into provider context, so ``never_send_to_model`` and
    (without an explicit opt-in) ``sensitive`` events are excluded. Verified
    voice turns may use ``voice_model`` to retain sensitive owner context while
    still excluding explicit never-send events.
    """
    stmt = (
        select(Event)
        .where(
            Event.conversation_id == thread_id,
            Event.tombstoned_at.is_(None),
            Event.event_type.in_(["message.user", "message.assistant"]),
        )
        .order_by(Event.occurred_at.desc(), Event.id.desc())
        .limit(min(limit, 200))
    )
    if access in {"model", "voice_model"}:
        excluded = (
            ("never_send_to_model",)
            if access == "voice_model"
            else ("never_send_to_model", "sensitive")
        )
        stmt = stmt.where(Event.privacy_level.notin_(excluded))
    result = await session.execute(stmt)
    return list(reversed(result.scalars().all()))


async def list_threads(session: AsyncSession) -> list[ConversationThread]:
    result = await session.execute(
        select(ConversationThread).order_by(ConversationThread.updated_at.desc())
    )
    return list(result.scalars().all())


async def reset_state(
    session: AsyncSession,
    thread_id: UUID,
    *,
    reason: str,
    actor: str = "api",
) -> ConversationState:
    """Start fresh without creating a new chat: same thread, cleared working state."""
    await EventService(session, actor=actor).create(
        EventCreate(
            source="conversation",
            event_type="conversation.reset",
            text=f"Reset conversation state: {reason}",
            conversation_id=thread_id,
        )
    )
    state = await get_or_create_state(session, thread_id)
    state.focus = None
    state.recent_topics = []
    state.pending_questions = []
    state.working_context = {}
    state.updated_at = utcnow()
    return state
