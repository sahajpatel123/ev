"""Layer 1: durable conversational turns as immutable Events.

Typed chat already writes message.user / message.assistant. Live Realtime
must use the same Event type so memory belongs to Evie, not a provider session.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.observe import log_memory
from app.models import Event
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.utils.text import utcnow

logger = logging.getLogger("ev.memory")

_TURN_TYPES = {"message.user", "message.assistant"}
_DEDUP_WINDOW = timedelta(seconds=12)


def _as_uuid(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return UUID(text)
    except ValueError:
        return None


async def record_conversation_turn(
    session: AsyncSession,
    *,
    text: str,
    role: str,
    source: str,
    conversation_id: UUID | str | None,
    device_id: str | None = None,
    live_session_id: str | None = None,
    actor: str = "owner",
    modality: str | None = None,
    transcript_source: str | None = None,
) -> Event | None:
    """Persist one spoken or typed turn. Skips empty text and near-duplicate echoes."""

    spoken = (text or "").strip()
    if not spoken:
        return None
    event_type = "message.assistant" if role in {"assistant", "evie", "ev"} else "message.user"
    thread_id = _as_uuid(conversation_id)
    if await _is_duplicate(session, spoken, event_type, thread_id):
        return None
    event = await EventService(session, actor=actor).create(
        EventCreate(
            source=source[:32],
            event_type=event_type,
            text=spoken[:8000],
            conversation_id=thread_id,
            device_id=(device_id or None),
            metadata={
                "modality": modality or source,
                "live_session_id": live_session_id,
                "speaker": "assistant" if event_type == "message.assistant" else "owner",
                "relationship_turn": True,
                "transcript_source": transcript_source or source,
            },
        )
    )
    log_memory(
        "memory.event_persisted",
        extra={
            "event_id": str(event.id),
            "event_type": event_type,
            "source": source,
            "chars": len(spoken),
            "conversation_id": str(thread_id) if thread_id else None,
        },
    )
    return event


async def _is_duplicate(
    session: AsyncSession,
    text: str,
    event_type: str,
    conversation_id: UUID | None,
) -> bool:
    cutoff = utcnow() - _DEDUP_WINDOW
    stmt = (
        select(Event)
        .where(
            Event.event_type == event_type,
            Event.tombstoned_at.is_(None),
        )
        .order_by(Event.occurred_at.desc())
        .limit(8)
    )
    if conversation_id is not None:
        stmt = stmt.where(Event.conversation_id == conversation_id)
    rows = (await session.execute(stmt)).scalars().all()
    return any(
        _as_utc(row.occurred_at) >= cutoff
        and ((row.content or {}).get("text") or "").strip() == text
        for row in rows
    )


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def schedule_live_turn(
    *,
    text: str,
    role: str,
    conversation_id: str | None,
    device_id: str | None,
    live_session_id: str | None,
    transcript_source: str | None = None,
) -> None:
    """Fire-and-forget persist so the realtime audio loop never waits on DB."""

    spoken = (text or "").strip()
    if not spoken:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(
        _live_turn_task(
            text=spoken,
            role=role,
            conversation_id=conversation_id,
            device_id=device_id,
            live_session_id=live_session_id,
            transcript_source=transcript_source,
        ),
        name="ev-memory-live-turn",
    )
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


_PENDING: set[asyncio.Task] = set()
_FLUSH_TIMEOUT_S = 2.5


async def flush_live_turns(*, timeout_s: float = _FLUSH_TIMEOUT_S) -> int:
    """Await outstanding live persist tasks. Never called from the PCM path."""

    pending = [task for task in _PENDING if not task.done()]
    if not pending:
        return 0
    done, still = await asyncio.wait(pending, timeout=max(0.05, timeout_s))
    log_memory(
        "memory.live_flush",
        extra={"flushed": len(done), "timed_out": len(still)},
    )
    return len(done)


async def _live_turn_task(
    *,
    text: str,
    role: str,
    conversation_id: str | None,
    device_id: str | None,
    live_session_id: str | None,
    transcript_source: str | None = None,
) -> None:
    from sqlalchemy.exc import OperationalError

    from app.db import SessionLocal
    from app.ev.continuity import classify_memory_intent
    from app.memory.episodes import maybe_update_episode
    from app.memory.select import apply_forget_intent, apply_pin_intent
    from app.services.processor import ensure_processed

    await asyncio.sleep(0.2)
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            event_id = None
            thread_id = None
            async with SessionLocal() as session:
                event = await record_conversation_turn(
                    session,
                    text=text,
                    role=role,
                    source="voice",
                    conversation_id=conversation_id,
                    device_id=device_id,
                    live_session_id=live_session_id,
                    modality="live_realtime",
                    transcript_source=transcript_source,
                )
                if event is None:
                    await session.commit()
                    return
                event_id = event.id
                thread_id = event.conversation_id
                await session.commit()
            from app.voice.live.voice_memory import note_event_committed

            note_event_committed()
            log_memory("memory.extraction_started", extra={"event_id": str(event_id)})
            await ensure_processed(event_id)
            intent = classify_memory_intent(text) if role == "user" else "none"
            async with SessionLocal() as session:
                if intent == "forget":
                    await apply_forget_intent(session, text, conversation_id=thread_id)
                elif intent == "pin":
                    await apply_pin_intent(session, conversation_id=thread_id)
                if role == "user" and thread_id is not None:
                    from app.models import Event

                    seed = await session.get(Event, event_id)
                    if seed is not None:
                        await maybe_update_episode(session, thread_id, seed_event=seed)
                if thread_id is not None:
                    from app.ev import conversation as conversation_mod

                    working = {
                        "modality": "live_realtime",
                        "live_session_id": live_session_id,
                    }
                    if role == "user":
                        working["last_user_message"] = text[:1000]
                    else:
                        working["last_assistant_message"] = text[:1000]
                    try:
                        await conversation_mod.update_state(
                            session,
                            thread_id,
                            focus=text[:240] if role == "user" else None,
                            working_context=working,
                        )
                    except Exception:  # noqa: BLE001 - missing thread must not drop the event
                        log_memory("memory.degraded", extra={"error": "working_memory_update_failed"})
                await session.commit()
            log_memory(
                "memory.extraction_completed",
                extra={"event_id": str(event_id), "intent": intent},
            )
            try:
                from app.memory.bootstrap import build_bootstrap
                from app.memory.curator import schedule_curation
                from app.memory.router import observe_turn

                async with SessionLocal() as session:
                    if role == "user":
                        await observe_turn(session, text)
                    if role == "user":
                        await build_bootstrap(session)
                    await session.commit()
                schedule_curation(limit=2 if intent == "pin" else 1)
                if role == "user":
                    try:
                        from app.memory.loops import infer_scope
                        from app.memory.prefetch import prefetch, prefetch_mode

                        if prefetch_mode() in {"shadow", "on"}:
                            async with SessionLocal() as session:
                                await prefetch(session, infer_scope(text))
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001 - Pipeline B must not undo capture
                log_memory("memory.degraded", extra={"error": "memory_os_post_persist_failed"})
            return
        except OperationalError as exc:
            last_error = exc
            await asyncio.sleep(0.05 * (attempt + 1))
        except Exception:  # noqa: BLE001 - persistence must not take down live audio
            logger.exception("live conversation turn persist failed")
            log_memory("memory.degraded", extra={"error": "live_turn_persist_failed"})
            from app.voice.live.voice_memory import note_persist_failed

            note_persist_failed(reason="live_turn_persist_failed")
            return
    logger.warning("live conversation turn persist failed after retries: %s", last_error)
    log_memory("memory.degraded", extra={"error": "live_turn_persist_locked"})
    from app.voice.live.voice_memory import note_persist_failed

    note_persist_failed(reason="live_turn_persist_locked")
