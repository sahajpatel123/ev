"""Research assistant: memory-grounded sessions, notes, sources, conclusions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings import get_embedder
from app.models import Event, Memory, MemoryEvent, ResearchNote, ResearchSession
from app.schemas import (
    EventCreate,
    ResearchConclude,
    ResearchJobCreate,
    ResearchNoteCreate,
    ResearchSessionCreate,
)
from app.search.providers import SearchProvider, SearchResult, get_search_provider
from app.services.access_log import log_access
from app.services.event_service import EventService
from app.utils.text import fingerprint, normalize_text

JOB_STATUSES = {"queued", "running", "paused", "failed", "cancelled", "completed"}
ALLOWED_JOB_TOOLS = {"web_search"}


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
        session = ResearchSession(
            question=data.question,
            goal=data.question,
            owner=self.actor,
            mode="session",
            status="open",
        )
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

    async def create_job(self, data: ResearchJobCreate) -> ResearchSession:
        """Create an idempotent, bounded research job in the existing session table."""
        tools = sorted({str(item).strip() for item in data.allowed_tools if str(item).strip()})
        if not tools:
            tools = ["web_search"]
        unknown = sorted(set(tools) - ALLOWED_JOB_TOOLS)
        if unknown:
            raise ValueError(f"unsupported research tools: {', '.join(unknown)}")
        if data.deadline_at is not None:
            deadline = data.deadline_at
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            if deadline <= datetime.now(UTC):
                raise ValueError("deadline_at must be in the future")
        existing = (
            await self.session.execute(
                select(ResearchSession).where(
                    ResearchSession.mode == "job",
                    ResearchSession.owner == self.actor,
                    ResearchSession.status.in_(["queued", "running", "paused"]),
                )
            )
        ).scalars().all()
        key = normalize_text(data.goal)
        for row in existing:
            if normalize_text(row.goal or row.question) == key:
                return row
        checkpoints = [
            {"name": name, "status": "pending"}
            for name in data.checkpoints
            if name.strip()
        ]
        if not checkpoints:
            checkpoints = [{"name": "collect_sources", "status": "pending"}]
        job = ResearchSession(
            question=data.goal,
            goal=data.goal,
            owner=self.actor,
            mode="job",
            status="queued",
            allowed_tools=tools,
            deadline_at=data.deadline_at,
            budget={
                "max_results": data.max_results,
                "timeout_seconds": data.timeout_seconds,
            },
            checkpoints=checkpoints,
            progress={
                "phase": "queued",
                "completed": 0,
                "total": len(checkpoints),
                "percent": 0,
            },
            final_artifacts=[],
            citations=[],
            evidence={},
        )
        self.session.add(job)
        await self.session.flush()
        await EventService(self.session, actor=self.actor).create(
            EventCreate(
                source="research",
                event_type="research.job.created",
                text=f"Research job: {data.goal}",
                metadata={
                    "research_job_id": str(job.id),
                    "owner": self.actor,
                    "allowed_tools": tools,
                    "budget": job.budget,
                },
            )
        )
        await log_access(
            self.session,
            actor=self.actor,
            action="research.job.create",
            endpoint="POST /v1/research/jobs",
            resource_type="research_job",
            resource_ids=[job.id],
            details={"allowed_tools": tools, "budget": job.budget},
        )
        return job

    async def _job(self, job_id: UUID) -> ResearchSession:
        job = await self.session.get(ResearchSession, job_id)
        if job is None or job.mode != "job":
            raise KeyError(f"Research job {job_id} not found")
        if job.owner != self.actor and self.actor not in {"master", "scheduler", "worker"}:
            raise PermissionError("research job belongs to another owner")
        return job

    @staticmethod
    def _job_payload(job: ResearchSession, *, replay: bool = False) -> dict:
        payload = {
            "id": str(job.id),
            "owner": job.owner,
            "goal": job.goal or job.question,
            "status": job.status,
            "allowed_tools": list(job.allowed_tools or []),
            "deadline_at": job.deadline_at.isoformat() if job.deadline_at else None,
            "budget": dict(job.budget or {}),
            "checkpoints": list(job.checkpoints or []),
            "progress": dict(job.progress or {}),
            "final_artifacts": list(job.final_artifacts or []),
            "citations": list(job.citations or []),
            "evidence": dict(job.evidence or {}),
            "cancel_requested": bool(job.cancel_requested),
            "attempts": int(job.attempts or 0),
            "last_error": job.last_error,
        }
        if replay:
            payload["idempotent_replay"] = True
        return payload

    async def run_job(
        self,
        job_id: UUID,
        *,
        provider: SearchProvider | None = None,
    ) -> dict:
        """Run one bounded research step; every state transition is durable."""
        job = await self._job(job_id)
        if job.status in {"completed", "cancelled"}:
            return self._job_payload(job, replay=True)
        now = datetime.now(UTC)
        deadline = job.deadline_at
        if deadline is not None and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        if deadline is not None and deadline <= now:
            job.status = "failed"
            job.last_error = "deadline_exceeded"
            job.progress = {**(job.progress or {}), "phase": "failed"}
            job.evidence = {"source": "research_job", "accepted": False, "observed": False, "error": job.last_error}
            await self.session.flush()
            return self._job_payload(job)
        if job.cancel_requested:
            job.status = "cancelled"
            job.progress = {**(job.progress or {}), "phase": "cancelled"}
            await self.session.flush()
            return self._job_payload(job)
        job.status = "running"
        job.cancel_requested = False
        job.attempts = int(job.attempts or 0) + 1
        job.last_error = None
        job.progress = {**(job.progress or {}), "phase": "collecting"}
        await self.session.flush()
        if "web_search" not in set(job.allowed_tools or []):
            job.status = "failed"
            job.last_error = "search_tool_not_allowed"
            job.progress = {**(job.progress or {}), "phase": "failed"}
            await self.session.flush()
            return self._job_payload(job)
        provider = provider or get_search_provider()
        if provider is None:
            job.status = "failed"
            job.last_error = "provider_not_connected"
            job.progress = {**(job.progress or {}), "phase": "failed"}
            job.evidence = {
                "source": "research_provider",
                "accepted": False,
                "observed": False,
                "error": "not_connected",
            }
            await self.session.flush()
            return self._job_payload(job)
        budget = dict(job.budget or {})
        limit = max(1, min(int(budget.get("max_results") or 5), 20))
        timeout = max(0.01, min(float(budget.get("timeout_seconds") or 10.0), 60.0))
        # Research is a network side effect even though its stored output is
        # read-only.  Gate the provider call through the same policy decision
        # used by live/HTTP tool dispatch; the injected provider remains an
        # adapter double for tests and does not bypass authorization.
        from app.ev.policy import authorize

        decision = await authorize(
            self.session,
            "search_web",
            actor=self.actor,
            channel="action",
            arguments={"query": str(job.goal or job.question), "limit": limit},
            provider_connected_override=True,
        )
        if not decision.allowed:
            job.status = "paused" if decision.effect == "confirm" else "failed"
            job.last_error = decision.reason
            job.progress = {
                **(job.progress or {}),
                "phase": "paused" if decision.effect == "confirm" else "failed",
            }
            job.evidence = {
                "source": "policy",
                "accepted": False,
                "observed": False,
                "error": decision.effect,
                "reason": decision.reason,
                "risk_class": decision.risk_class,
            }
            await self.session.flush()
            return self._job_payload(job)
        try:
            results = await asyncio.wait_for(
                provider.search(job.goal or job.question, limit=limit),
                timeout=timeout,
            )
        except TimeoutError:
            job.status = "paused"
            job.last_error = "provider_timeout"
            job.progress = {**(job.progress or {}), "phase": "paused"}
            job.evidence = {
                "source": getattr(provider, "name", "research_provider"),
                "accepted": False,
                "observed": False,
                "error": "timeout",
            }
            await self.session.flush()
            return self._job_payload(job)
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.cancel_requested = True
            job.progress = {**(job.progress or {}), "phase": "cancelled"}
            await self.session.flush()
            return self._job_payload(job)
        except Exception as exc:  # noqa: BLE001 - provider boundary
            job.status = "failed"
            job.last_error = f"provider_error:{type(exc).__name__}"
            job.progress = {**(job.progress or {}), "phase": "failed"}
            job.evidence = {
                "source": getattr(provider, "name", "research_provider"),
                "accepted": False,
                "observed": False,
                "error": str(exc)[:512],
            }
            await self.session.flush()
            return self._job_payload(job)
        notes = await list_notes(self.session, job.id)
        seen_urls = {note.source_url for note in notes if note.source_url}
        stored = 0
        citations: list[dict] = []
        for result in results[:limit]:
            if job.cancel_requested:
                job.status = "cancelled"
                job.progress = {**(job.progress or {}), "phase": "cancelled"}
                await self.session.flush()
                return self._job_payload(job)
            if isinstance(result, SearchResult):
                title, url, snippet = result.title, result.url, result.snippet
            elif isinstance(result, dict):
                title = str(result.get("title") or "")
                url = str(result.get("url") or "")
                snippet = str(result.get("snippet") or result.get("description") or "")
            else:
                title = str(getattr(result, "title", "") or "")
                url = str(getattr(result, "url", "") or "")
                snippet = str(getattr(result, "snippet", "") or "")
            if not snippet or (url and url in seen_urls):
                continue
            await self.add_note(
                job.id,
                ResearchNoteCreate(note=snippet[:10_000], source_url=url[:1024] or None, source_title=title[:512] or None),
            )
            seen_urls.add(url)
            citations.append({"title": title[:512], "url": url[:1024], "snippet": snippet[:500]})
            stored += 1
        checkpoints = [dict(item) for item in (job.checkpoints or []) if isinstance(item, dict)]
        for checkpoint in checkpoints:
            checkpoint["status"] = "completed"
        job.checkpoints = checkpoints
        job.citations = citations
        job.status = "completed"
        job.progress = {"phase": "completed", "completed": len(checkpoints), "total": len(checkpoints), "percent": 100}
        job.final_artifacts = [{"kind": "research_notes", "count": stored, "job_id": str(job.id)}]
        job.evidence = {
            "source": getattr(provider, "name", "research_provider"),
            "accepted": True,
            "observed": True,
            "source_count": stored,
            "completed_at": datetime.now(UTC).isoformat(),
        }
        await log_access(
            self.session,
            actor=self.actor,
            action="research.job.run",
            endpoint="POST /v1/research/jobs/{job_id}/run",
            resource_type="research_job",
            resource_ids=[job.id],
            details={"status": job.status, "source_count": stored, "evidence": job.evidence},
        )
        await self.session.flush()
        return self._job_payload(job)

    async def cancel_job(self, job_id: UUID) -> dict:
        job = await self._job(job_id)
        if job.status in {"completed", "cancelled"}:
            return self._job_payload(job, replay=True)
        job.cancel_requested = True
        job.status = "cancelled"
        job.progress = {**(job.progress or {}), "phase": "cancelled"}
        job.evidence = {"source": "research_job", "accepted": True, "observed": True, "status": "cancelled"}
        await log_access(
            self.session,
            actor=self.actor,
            action="research.job.cancel",
            endpoint="POST /v1/research/jobs/{job_id}/cancel",
            resource_type="research_job",
            resource_ids=[job.id],
            details={"status": "cancelled"},
        )
        await self.session.flush()
        return self._job_payload(job)

    async def resume_job(self, job_id: UUID) -> dict:
        job = await self._job(job_id)
        if job.status == "completed":
            return self._job_payload(job, replay=True)
        if job.status == "cancelled":
            raise ValueError("cancelled research jobs cannot be resumed")
        job.status = "queued"
        job.cancel_requested = False
        job.last_error = None
        job.progress = {**(job.progress or {}), "phase": "queued"}
        await log_access(
            self.session,
            actor=self.actor,
            action="research.job.resume",
            endpoint="POST /v1/research/jobs/{job_id}/resume",
            resource_type="research_job",
            resource_ids=[job.id],
            details={"status": "queued"},
        )
        await self.session.flush()
        return self._job_payload(job)

    async def add_note(
        self,
        session_id: UUID,
        data: ResearchNoteCreate,
    ) -> ResearchNote:
        research = await self.session.get(ResearchSession, session_id)
        if research is None:
            raise KeyError(f"Research session {session_id} not found")
        allowed_statuses = {"open"}
        if research.mode == "job":
            allowed_statuses = {"queued", "running"}
        if research.status not in allowed_statuses:
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

    async def remember(
        self,
        session_id: UUID,
        note_id: UUID,
        *,
        actor: str = "api",
    ) -> Memory:
        """Persist a finding as a durable memory with real citation provenance.

        ``source_url`` / ``source_title`` are carried from the research note
        (which itself only ever comes from a search provider's results), so a
        remembered finding always has a traceable source. The memory is linked
        to the note's raw event and every event in the session.
        """
        note = (
            await self.session.execute(
                select(ResearchNote).where(
                    ResearchNote.session_id == session_id,
                    ResearchNote.id == note_id,
                )
            )
        ).scalar_one_or_none()
        if note is None:
            raise KeyError(f"Research note {note_id} not found in session {session_id}")
        research = await self.session.get(ResearchSession, session_id)
        if research is None:
            raise KeyError(f"Research session {session_id} not found")

        event = await EventService(self.session, actor=actor).create(
            EventCreate(
                source="research",
                event_type="research.remember",
                text=f"Remembered finding: {note.note}",
                metadata={
                    "research_session_id": str(session_id),
                    "research_note_id": str(note.id),
                    "source_url": note.source_url,
                    "source_title": note.source_title,
                },
            )
        )
        memory = Memory(
            memory_type="fact",
            text=f"Research finding: {note.note}",
            payload={
                "kind": "research_finding",
                "question": research.question,
                "research_session_id": str(session_id),
                "research_note_id": str(note.id),
                "source_url": note.source_url,
                "source_title": note.source_title,
            },
            importance=0.7,
            confidence=1.0,  # user explicitly asked EV to remember this finding
            source_type="explicit",
            privacy_level="normal",
            event_time=event.occurred_at,
            valid_from=event.occurred_at,
            version_group=uuid4(),
            version=1,
            fingerprint=fingerprint(
                {
                    "memory_type": "fact",
                    "research_note_id": str(note.id),
                    "text": normalize_text(note.note),
                }
            ),
        )
        try:
            memory.embedding = (await get_embedder().embed([memory.text]))[0]
        except Exception:
            memory.embedding = None
        self.session.add(memory)
        await self.session.flush()

        event_ids = await self._session_event_ids(session_id)
        event_ids.add(note.event_id)
        event_ids.add(event.id)
        for event_id in event_ids:
            self.session.add(MemoryEvent(memory_id=memory.id, event_id=event_id))
        await self.session.flush()
        return memory

    async def web_search(
        self,
        session_id: UUID,
        query: str,
        *,
        limit: int = 5,
    ) -> list[ResearchNote]:
        """Search the web into an open session, one cited note per result.

        Memory-only mode (no search provider configured) raises KeyError so the
        caller can surface a clear "web search disabled" outcome.
        """

        research = await self.session.get(ResearchSession, session_id)
        if research is None:
            raise KeyError(f"Research session {session_id} not found")
        if research.status != "open":
            raise ValueError("Research session is already concluded")
        provider = get_search_provider()
        if provider is None:
            raise KeyError(
                "Web search is disabled: set EV_SEARCH_PROVIDER and an API key to enable it"
            )
        from app.ev.policy import authorize

        decision = await authorize(
            self.session,
            "search_web",
            actor=self.actor,
            channel="action",
            arguments={"query": query, "limit": limit},
            provider_connected_override=True,
        )
        if not decision.allowed:
            raise PermissionError(decision.reason)
        results = await provider.search(query, limit=limit)
        notes: list[ResearchNote] = []
        for result in results:
            notes.append(
                await self.add_note(
                    session_id,
                    ResearchNoteCreate(
                        note=result.snippet,
                        source_url=result.url,
                        source_title=result.title,
                    ),
                )
            )
        return notes

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
    research: ResearchSession | None,
    event: Event,
) -> Memory:
    """Create the derived summary memory for a research.conclusion event."""
    session_id = (
        research.id
        if research is not None
        else UUID((event.metadata_ or {}).get("research_session_id", ""))
    )
    if research is not None:
        conclusion = research.conclusion or ""
        question = research.question
    else:
        conclusion = ((event.content or {}).get("text") or "").removeprefix("Research concluded: ")
        question = await _session_question(session, session_id)
    memory = Memory(
        memory_type="summary",
        text=f"Research conclusion: {conclusion}",
        payload={
            "kind": "research_conclusion",
            "question": question,
            "conclusion": conclusion,
            "research_session_id": str(session_id),
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
                "research_session_id": str(session_id),
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
    if research is not None:
        service = ResearchService(session)
        session_events = await service._session_event_ids(research.id)
    else:
        session_events = await _session_event_ids_from_events(session, session_id)
    session_events.add(event.id)
    for event_id in session_events:
        session.add(MemoryEvent(memory_id=memory.id, event_id=event_id))
    return memory


async def _session_question(session: AsyncSession, session_id: UUID) -> str:
    rows = (
        await session.execute(select(Event).where(Event.event_type == "research.session"))
    ).scalars().all()
    for event in rows:
        if (event.metadata_ or {}).get("research_session_id") != str(session_id):
            continue
        text = (event.content or {}).get("text") or ""
        if text.startswith("Research: "):
            return text.removeprefix("Research: ")
    return ""


async def _session_event_ids_from_events(session: AsyncSession, session_id: UUID) -> set[UUID]:
    rows = (await session.execute(select(Event).where(Event.source == "research"))).scalars().all()
    return {
        event.id
        for event in rows
        if (event.metadata_ or {}).get("research_session_id") == str(session_id)
    }


async def recreate_conclusion_memory(session: AsyncSession, event: Event) -> Memory | None:
    """Replay a research.conclusion event into its derived summary memory.

    Fully event-sourced: the question and provenance are reconstructed from the
    session's raw events, so import/restore does not depend on research_sessions
    rows surviving.
    """
    session_id = (event.metadata_ or {}).get("research_session_id")
    if not session_id:
        return None
    conclusion = ((event.content or {}).get("text") or "").removeprefix("Research concluded: ")
    if not conclusion:
        return None
    return await _build_conclusion_memory(session, None, event)


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
