"""Rebuildable materialized cards and journal. Postgres remains authority."""

from __future__ import annotations

from datetime import UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory.bootstrap import build_bootstrap
from app.memory.episodes import recent_episodes
from app.memory.os_health import note_curator
from app.memory.paths import append_jsonl, atomic_write_json, ensure_tree
from app.models import Event, Memory
from app.utils.text import utcnow

SCHEMA_VERSION = 1


def _event_line(event: Event) -> dict:
    meta = event.metadata_ or {}
    return {
        "event_id": str(event.id),
        "timestamp": event.occurred_at.isoformat() if event.occurred_at else None,
        "speaker": meta.get("speaker") or (
            "assistant" if event.event_type == "message.assistant" else "owner"
        ),
        "source": event.source,
        "text": str((event.content or {}).get("text") or "")[:4000],
        "transcript_source": meta.get("transcript_source"),
    }


def append_journal(event: Event) -> None:
    if event.event_type not in {"message.user", "message.assistant"}:
        return
    if event.privacy_level in {"never_send_to_model", "sensitive"}:
        return
    text = str((event.content or {}).get("text") or "").strip()
    if not text:
        return
    root = ensure_tree()
    occurred = event.occurred_at
    if occurred is not None and occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    year = occurred.year if occurred else utcnow().year
    month = f"{occurred.month:02d}" if occurred else f"{utcnow().month:02d}"
    day = occurred.date().isoformat() if occurred else utcnow().date().isoformat()
    path = root / "journal" / str(year) / month / f"{day}.jsonl"
    append_jsonl(path, _event_line(event))


async def materialize_cards(session: AsyncSession, *, through_event_id: str | None = None) -> dict:
    root = ensure_tree()
    pack = await build_bootstrap(session)
    through = through_event_id or pack.get("through_event_id")
    from app.memory.state import get_project_state

    project_state = await get_project_state(session)
    stamp = {
        "schema_version": SCHEMA_VERSION,
        "card_version": pack.get("card_version"),
        "updated_at": pack.get("updated_at"),
        "through_event_id": through,
        "curator_provider": "deepseek",
        "curator_model": None,
        "curator_version": settings.memory_curator_version or "1.1",
        "generated_at": utcnow().isoformat(),
        "source_event_ids": [through] if through else [],
    }
    atomic_write_json(
        root / "cards" / "relationship.json",
        {**stamp, "kind": "relationship", "text": pack.get("relationship"), "open_loops": pack.get("open_loops") or []},
    )
    atomic_write_json(
        root / "cards" / "current_state.json",
        {
            **stamp,
            "kind": "current_state",
            "active_project": pack.get("active_project") or project_state.get("active_project"),
            "last_episode": pack.get("last_episode"),
            "open_loops": project_state.get("open_loops") or [],
            "recently_resolved": project_state.get("recently_resolved") or [],
            "decisions": project_state.get("decisions") or [],
            "rejected_options": project_state.get("rejected_options") or [],
            "hypotheses": project_state.get("hypotheses") or [],
        },
    )
    facts = (
        await session.execute(
            select(Memory)
            .where(
                Memory.memory_type == "fact",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
            .order_by(Memory.importance.desc())
            .limit(24)
        )
    ).scalars().all()
    project_items = [
        {"id": str(row.id), "text": row.text, "payload": row.payload, "source_type": row.source_type}
        for row in facts
        if "project" in (row.text or "").lower() or (row.payload or {}).get("subject")
    ]
    atomic_write_json(
        root / "cards" / "projects" / "evie.json",
        {
            **stamp,
            "kind": "project",
            "name": project_state.get("scope") or "Evie",
            "current_state": {
                "open_loops": project_state.get("open_loops") or [],
                "recently_resolved": project_state.get("recently_resolved") or [],
                "decisions": project_state.get("decisions") or [],
                "rejected_options": project_state.get("rejected_options") or [],
            },
            "items": project_items[:8],
        },
    )
    episodes = await recent_episodes(session, k=6)
    for row in episodes:
        name = f"{(row.event_time or utcnow()).date().isoformat()}-{str(row.id)[:8]}.json"
        atomic_write_json(
            root / "cards" / "episodes" / name,
            {
                **stamp,
                "kind": "episode",
                "memory_id": str(row.id),
                "text": row.text,
                "payload": row.payload,
            },
        )
    people = (
        await session.execute(
            select(Memory)
            .where(
                Memory.memory_type == "fact",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
            .limit(40)
        )
    ).scalars().all()
    for row in people:
        payload = row.payload or {}
        if payload.get("kind") not in {"person", "people"} and "person" not in (row.text or "").lower():
            continue
        slug = str(payload.get("subject") or row.id)[:40].replace(" ", "-").lower()
        atomic_write_json(
            root / "cards" / "people" / f"{slug}.json",
            {**stamp, "kind": "person", "text": row.text, "payload": payload, "memory_id": str(row.id)},
        )
    prefs = (
        await session.execute(
            select(Memory)
            .where(
                Memory.memory_type == "preference",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
            .order_by(Memory.importance.desc())
            .limit(16)
        )
    ).scalars().all()
    atomic_write_json(
        root / "cards" / "preferences" / "stable.json",
        {**stamp, "kind": "preferences", "items": [{"text": row.text, "id": str(row.id)} for row in prefs]},
    )
    atomic_write_json(root / "cache" / "active_context.json", pack)
    atomic_write_json(
        root / "diagnostics" / "memory-health.json",
        {
            "authority": "postgres",
            "mirror": "rebuildable",
            "card_version": pack.get("card_version"),
            "updated_at": pack.get("updated_at"),
            "through_event_id": through,
        },
    )
    note_curator(status="materialized", event_id=through, cards=1)
    return pack


async def rebuild_memory_cards(session: AsyncSession) -> dict:
    """Regenerate the private folder from Postgres. Folder is not authority."""

    root = ensure_tree()
    events = (
        await session.execute(
            select(Event)
            .where(
                Event.tombstoned_at.is_(None),
                Event.event_type.in_(("message.user", "message.assistant")),
            )
            .order_by(Event.occurred_at.asc())
            .limit(4000)
        )
    ).scalars().all()
    for event in events:
        append_journal(event)
    pack = await materialize_cards(session)
    return {"root": str(root), "events_mirrored": len(events), "bootstrap": pack}
