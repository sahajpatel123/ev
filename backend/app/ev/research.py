"""Research assistant: memory-grounded sessions, notes, sources, conclusions."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings import get_embedder
from app.models import Event, Memory, MemoryEvent, ResearchNote, ResearchSession
from app.schemas import EventCreate, ResearchConclude, ResearchNoteCreate, ResearchSessionCreate
from app.services.event_service import EventService
from app.utils.text import fingerprint, normalize_text


class ResearchService:
    def __init__(self, session: AsyncSession, actor: str = "api") -> None:
        self.session = session
        self.actor = actor

    async def create_session(self, data: ResearchSessionCreate) -> ResearchSession:
        key = normalize_text(data.question)
        open_rows = (
            await self.session.execute(
                select(ResearchSession).where(ResearchSession.status == "open")
            )
        ).scalars().all()
        for row in open_rows:
            if normalize_text(row.question) == key:
                return row
        session = ResearchSession(question=data.question, status="open")
        self.session.add(session)
        await self.session.flush()
        await EventService(self.session, actor=self.actor).create(
            EventCreate(
                source="research",
                event_type="research.session",
                text=f"Research: {data.question}",
                metadata={"research_session_id": str(session.id)},
            )
        )
        return session

    async def add_note(
        self,
        session_id: UUID,
        data: ResearchNoteCreate,
    ) -> ResearchNote:
        research = await self.session.get(ResearchSession, session_id)
        if research is None:
            raise KeyError(f"Research session {session_id} not found")
        if research.status != "open":
            raise ValueError("Research session is already concluded")
        event = await EventService(self.session, actor=self.actor).create(
            EventCreate(
                source="research",
                event_type="research.note",
                text=data.note,
                metadata={
                    "research_session_id": str(session_id),
                    "source_url": data.source_url,
                    "source_title": data.source_title,
                },
            )
        )
        note = ResearchNote(
            session_id=session_id,
            event_id=event.id,
            note=data.note,
            source_url=data.source_url,
            source_title=data.source_title,
        )
        self.session.add(note)
        await self.session.flush()
        return note

    async def conclude(
        self,
        session_id: UUID,
        data: ResearchConclude,
    ) -> tuple[ResearchSession, Memory]:
        research = await self.session.get(ResearchSession, session_id)
        if research is None:
            raise KeyError(f"Research session {session_id} not found")
        if research.status != "open":
            raise ValueError("Research session is already concluded")

        event = await EventService(self.session, actor=self.actor).create(
            EventCreate(
                source="research",
                event_type="research.conclusion",
                text=f"Research concluded: {data.conclusion}",
                metadata={"research_session_id": str(session_id)},
            )
        )
        research.status = "concluded"
        research.conclusion = data.conclusion

        memory = await _build_conclusion_memory(self.session, research, event)
        return research, memory

    async def _session_event_ids(self, session_id: UUID) -> set[UUID]:
        note_rows = (
            await self.session.execute(
                select(ResearchNote).where(ResearchNote.session_id == session_id)
            )
        ).scalars().all()
        event_ids = {note.event_id for note in note_rows}
        all_research_events = (
            await self.session.execute(select(Event).where(Event.source == "research"))
        ).scalars().all()
        event_rows = [
            event
            for event in all_research_events
            if (event.metadata_ or {}).get("research_session_id") == str(session_id)
        ]
        for event in event_rows:
            event_ids.add(event.id)
        return event_ids

    async def detail(self, session_id: UUID) -> ResearchSession | None:
        return await self.session.get(ResearchSession, session_id)


async def _build_conclusion_memory(
    session: AsyncSession,
    research: ResearchSession,
    event: Event,
) -> Memory:
    """Create the derived summary memory for a research.conclusion event."""
    conclusion = research.conclusion or ""
    memory = Memory(
        memory_type="summary",
        text=f"Research conclusion: {conclusion}",
        payload={
            "kind": "research_conclusion",
            "question": research.question,
            "conclusion": conclusion,
            "research_session_id": str(research.id),
        },
        importance=0.7,
        confidence=0.85,
        source_type="derived",
        privacy_level="normal",
        event_time=event.occurred_at,
        valid_from=event.occurred_at,
        version_group=uuid4(),
        version=1,
        fingerprint=fingerprint(
            {
                "memory_type": "summary",
                "research_session_id": str(research.id),
                "conclusion": normalize_text(conclusion),
            }
        ),
    )
    try:
        memory.embedding = (await get_embedder().embed([memory.text]))[0]
    except Exception:
        memory.embedding = None
    session.add(memory)
    await session.flush()

    # Provenance: the conclusion traces to every event in the session.
    service = ResearchService(session)
    session_events = await service._session_event_ids(research.id)
    session_events.add(event.id)
    for event_id in session_events:
        session.add(MemoryEvent(memory_id=memory.id, event_id=event_id))
    return memory


async def recreate_conclusion_memory(session: AsyncSession, event: Event) -> Memory | None:
    """Replay a research.conclusion event into its derived summary memory."""
    session_id = (event.metadata_ or {}).get("research_session_id")
    if not session_id:
        return None
    research = await session.get(ResearchSession, UUID(session_id))
    if research is None or not research.conclusion:
        return None
    return await _build_conclusion_memory(session, research, event)


async def list_sessions(
    session: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[ResearchSession]:
    stmt = select(ResearchSession).order_by(ResearchSession.created_at.desc()).limit(min(limit, 200))
    if status:
        stmt = stmt.where(ResearchSession.status == status)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_notes(session: AsyncSession, session_id: UUID) -> list[ResearchNote]:
    result = await session.execute(
        select(ResearchNote)
        .where(ResearchNote.session_id == session_id)
        .order_by(ResearchNote.created_at.asc())
    )
    return list(result.scalars().all())
