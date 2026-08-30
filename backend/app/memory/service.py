"""Evie Memory OS facade. One service; not a second memory stack.

Pipeline A (critical): Event commit. No DeepSeek, embeddings, worker, or Redis.
Pipeline B (async): DeepSeek curator + cards + bootstrap cache.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory.bootstrap import build_bootstrap, get_bootstrap
from app.memory.curator import curator_available, process_curation_jobs
from app.memory.materialize import rebuild_memory_cards
from app.memory.os_health import snapshot as os_snapshot
from app.memory.outbox import job_counts, pending_counts
from app.memory.paths import memory_root
from app.memory.recall import build_explicit_recall_payload
from app.memory.router import observe_turn, select_context
from app.memory.turns import flush_live_turns, record_conversation_turn
from app.models import Event
from app.voice.live.voice_memory import health_snapshot as voice_snapshot


class MemoryService:
    """Owner-global memory. Provider session ids are not identity."""

    async def record_turn(self, session: AsyncSession, **kwargs) -> Event | None:
        return await record_conversation_turn(session, **kwargs)

    async def flush_turn(self, *, timeout_s: float = 2.5) -> int:
        return await flush_live_turns(timeout_s=timeout_s)

    async def recall(self, session: AsyncSession, query: str, *, k: int = 8) -> dict[str, Any]:
        return await build_explicit_recall_payload(session, query, k=k)

    async def search_events(self, session: AsyncSession, query: str, *, k: int = 40):
        from app.memory.index import search_event_ids

        return await search_event_ids(session, query, k=k)

    async def search_memories(self, session: AsyncSession, query: str, *, k: int = 8) -> dict[str, Any]:
        return await build_explicit_recall_payload(session, query, k=k)

    async def resolve_history(self, session: AsyncSession, query: str, *, k: int = 8) -> dict[str, Any]:
        return await self.recall(session, query, k=k)

    async def select_context(self, session: AsyncSession, query: str, **kwargs) -> dict[str, Any]:
        return await select_context(session, query, **kwargs)

    async def observe_turn(self, session: AsyncSession, query: str) -> dict[str, Any] | None:
        return await observe_turn(session, query)

    async def build_bootstrap(self, session: AsyncSession) -> dict[str, Any]:
        return await build_bootstrap(session)

    async def get_bootstrap(self, session: AsyncSession | None = None) -> dict[str, Any]:
        return await get_bootstrap(session)

    async def rebuild_memory_cards(self, session: AsyncSession) -> dict[str, Any]:
        return await rebuild_memory_cards(session)

    async def curate(self, session: AsyncSession, *, limit: int = 4) -> int:
        return await process_curation_jobs(session, limit=limit)

    async def get_open_loops(self, session: AsyncSession, scope: str | None = None, **kwargs):
        from app.memory.loops import list_loops, loop_public

        rows = await list_loops(session, scope=scope, **kwargs)
        return [loop_public(row) for row in rows]

    async def get_project_state(self, session: AsyncSession, scope: str | None = None) -> dict[str, Any]:
        from app.memory.state import get_project_state

        return await get_project_state(session, scope)

    async def get_decisions(self, session: AsyncSession, scope: str | None = None) -> list[dict[str, Any]]:
        state = await self.get_project_state(session, scope)
        return list(state.get("decisions") or [])

    async def get_state_as_of(self, session: AsyncSession, when, *, k: int = 24):
        from app.memory.loops import loop_public
        from app.memory.state import memories_as_of

        rows = await memories_as_of(session, boundary=when, k=k)
        return [
            loop_public(row) if row.memory_type == "open_loop" else {
                "id": str(row.id),
                "text": row.text,
                "memory_type": row.memory_type,
                "is_current": row.is_current,
                "when": row.event_time.isoformat() if row.event_time else None,
            }
            for row in rows
        ]

    async def get_changes(self, session: AsyncSession, *, since=None, until=None) -> dict[str, Any]:
        from app.memory.state import get_changes

        return await get_changes(session, since=since, until=until)

    async def prefetch(self, session: AsyncSession, scope: str | None = None):
        from app.memory.prefetch import prefetch

        return await prefetch(session, scope)

    async def resolve_loop(self, session: AsyncSession, query: str, *, k: int = 8) -> dict[str, Any]:
        return await self.recall(session, query, k=k)

    def mirror_root(self) -> str:
        return str(memory_root())


async def health(session: AsyncSession | None = None) -> dict[str, Any]:
    voice = voice_snapshot()
    os_part = os_snapshot()
    pending = failed = retryable = 0
    last_user = None
    open_loop_count = 0
    active_project_count = 0
    if session is not None:
        pending, failed = await pending_counts(session)
        counts = await job_counts(session)
        retryable = counts["retryable_failed"]
        failed = counts["permanent_failed"]
        last_user = (
            await session.execute(
                select(func.max(Event.occurred_at)).where(
                    Event.event_type == "message.user",
                    Event.tombstoned_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        from app.memory.loops import list_loops

        opens = await list_loops(session, k=200)
        open_loop_count = len(opens)
        active_project_count = len(
            {
                str((row.payload or {}).get("scope") or "").strip().lower()
                for row in opens
                if (row.payload or {}).get("scope")
            }
        )
    from app.memory.prefetch import prefetch_mode
    from app.memory.prefetch import snapshot as prefetch_snap

    raw_ready = True
    prefetch_part = prefetch_snap()
    return {
        "raw_event_store_ready": raw_ready,
        "curator_ready": curator_available(),
        "vector_index_ready": os_part.get("vector_ready"),
        "fulltext_ready": os_part.get("fulltext_ready"),
        "bootstrap_ready": bool(os_part.get("bootstrap_version")),
        "cache_ready": bool(os_part.get("bootstrap_version")),
        "deep_recall_ready": raw_ready,
        "temporal_retrieval_ready": True,
        "curation_degraded": not curator_available(),
        "memory_gate_mode": (settings.memory_gate or "off").strip().lower(),
        "pending_jobs": pending,
        "failed_jobs": failed,
        "curator_pending_jobs": pending,
        "curator_retryable_failed": retryable,
        "curator_permanent_failed": failed,
        "open_loop_count": open_loop_count,
        "active_project_count": active_project_count,
        "last_reflection_at": os_part.get("last_reflection_at"),
        "reflection_lag_events": os_part.get("reflection_lag_events"),
        "curator_version": settings.memory_curator_version,
        "prefetch_mode": prefetch_mode(),
        "prefetch_hit_rate": prefetch_part.get("prefetch_hit_rate"),
        "last_user_event_committed_at": last_user.isoformat() if last_user else voice.get("last_voice_event_commit_at"),
        "last_curated_event_id": os_part.get("last_curated_event_id"),
        "bootstrap_version": os_part.get("bootstrap_version"),
        "bootstrap_age": os_part.get("bootstrap_age"),
        "memory_gate_p50_ms": os_part.get("memory_gate_p50_ms"),
        "memory_gate_p95_ms": os_part.get("memory_gate_p95_ms"),
        "curator_status": os_part.get("curator_status"),
        "mirror_root_configured": True,
        "provider_is_not_source_of_truth": True,
        **{key: voice[key] for key in (
            "pending_voice_turns",
            "last_voice_transcript_status",
            "durable_voice_memory_ready",
            "realtime_input_transcription",
        ) if key in voice},
    }
