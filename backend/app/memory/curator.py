"""DeepSeek Memory Curator. Background only. Never blocks Realtime speech.

The model proposes structured updates. MemoryWriter commits them.
Temporary DeepSeek outages leave jobs retryable. Raw Events remain the memory.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory.materialize import materialize_cards
from app.memory.os_health import note_curator
from app.memory.outbox import claim_jobs
from app.models import Event, MemoryCurationJob
from app.utils.text import utcnow

CURATOR_VERSION = "1.1"
MAX_ATTEMPTS = 8
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
ALLOWED_KINDS = {
    "decision",
    "preference",
    "goal",
    "fact",
    "observation",
    "episodic",
    "open_loop",
    "rejection",
    "hypothesis",
}
ALLOWED_ENTITY_TYPES = {"person", "place", "project", "topic", "other"}
ALLOWED_LOOP_STATUS = {
    "open",
    "blocked",
    "waiting",
    "resolved",
    "abandoned",
    "superseded",
    "unknown",
}
ALLOWED_EVIDENCE = {"owner_asserted", "verified_system", "derived", "inferred"}

SYSTEM_PROMPT = (
    "You are Evie's background memory curator and reflector. Organize new owner "
    "conversation events into structured updates. Respond with ONLY JSON:\n"
    '{"event_ids":["..."],"memories":[{"kind":"fact|decision|preference|goal|'
    'observation|open_loop|rejection|hypothesis","subject":"...","property":"...",'
    '"value":"...","text":"...","importance":0.0,"confidence":0.0,'
    '"evidence_type":"owner_asserted|verified_system|derived|inferred"}],'
    '"entities":[{"name":"...","entity_type":"person|place|project|topic|other",'
    '"aliases":[]}],"project_updates":[],"episode_update":{},'
    '"open_loops":[{"title":"...","scope":"...","status":"open|resolved|blocked|'
    'waiting","confidence":0.0,"evidence_type":"owner_asserted","source_event_ids":[]}],'
    '"resolved_loops":[],"decisions":[],"rejected_options":[],'
    '"current_hypotheses":[],"possible_next_steps":[],"supersessions":[],'
    '"search_aliases":[]}\n'
    "Rules: never invent owner facts. inferred claims must be observations. "
    "possible_next_steps are suggestions, never owner intent. "
    "Do not resolve a loop unless the owner or a verified system says it is "
    "fixed. Hypotheticals and quoted third-party speech are not owner state. "
    "Every item must be grounded in the provided events. Do not mention files, "
    "Postgres, or that you are a curator."
)


def curator_available() -> bool:
    if not settings.memory_curator_enabled:
        return False
    return bool((settings.deepseek_api_key or "").strip())


def validate_curator_payload(raw: dict[str, Any], *, event_ids: list[str]) -> dict[str, Any]:
    memories = []
    for item in raw.get("memories") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("memory_type") or "").strip()
        if kind not in ALLOWED_KINDS:
            continue
        if kind == "fact" and float(item.get("confidence") or 0) < 0.5:
            kind = "observation"
        text = str(item.get("text") or item.get("value") or "").strip()
        if not text:
            continue
        evidence = str(item.get("evidence_type") or "inferred")
        if evidence not in ALLOWED_EVIDENCE:
            evidence = "inferred"
        memories.append(
            {
                "kind": kind,
                "subject": str(item.get("subject") or "")[:120],
                "property": str(item.get("property") or "")[:120],
                "value": str(item.get("value") or text)[:500],
                "text": text[:800],
                "importance": max(0.0, min(1.0, float(item.get("importance") or 0.5))),
                "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.6))),
                "evidence_type": evidence,
            }
        )
    entities = []
    for item in raw.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        entity_type = str(item.get("entity_type") or "other")
        if not name or entity_type not in ALLOWED_ENTITY_TYPES:
            continue
        aliases = [str(alias)[:80] for alias in (item.get("aliases") or []) if str(alias).strip()][:8]
        entities.append({"name": name[:120], "entity_type": entity_type, "aliases": aliases})
    open_loops = []
    for item in list(raw.get("open_loops") or []) + list(raw.get("resolved_loops") or []):
        parsed_loop = _as_loop(item, event_ids=event_ids)
        if parsed_loop:
            open_loops.append(parsed_loop)
    next_steps = [
        str(item)[:200]
        for item in (raw.get("possible_next_steps") or [])
        if str(item).strip()
    ][:6]
    for kind, items in (
        ("decision", raw.get("decisions")),
        ("rejection", raw.get("rejected_options")),
        ("hypothesis", raw.get("current_hypotheses")),
    ):
        for item in items or []:
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict):
                continue
            text = str(
                item.get("text") or item.get("value") or item.get("decision") or item.get("title") or ""
            ).strip()
            if not text:
                continue
            default_evidence = "inferred" if kind == "hypothesis" else "owner_asserted"
            evidence = str(item.get("evidence_type") or default_evidence)
            if evidence not in ALLOWED_EVIDENCE:
                evidence = "inferred"
            memories.append(
                {
                    "kind": kind,
                    "subject": str(item.get("subject") or item.get("scope") or "")[:120],
                    "property": str(item.get("property") or kind)[:120],
                    "value": str(item.get("value") or text)[:500],
                    "text": text[:800],
                    "importance": max(0.0, min(1.0, float(item.get("importance") or 0.7))),
                    "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.7))),
                    "evidence_type": evidence,
                }
            )
    return {
        "event_ids": list(event_ids),
        "memories": memories[:24],
        "entities": entities[:16],
        "project_updates": [item for item in (raw.get("project_updates") or []) if isinstance(item, dict)][:8],
        "episode_update": raw.get("episode_update") if isinstance(raw.get("episode_update"), dict) else {},
        "open_loops": open_loops[:8],
        "possible_next_steps": next_steps,
        "next_steps_are_suggestions": True,
        "supersessions": [item for item in (raw.get("supersessions") or []) if isinstance(item, dict)][:8],
        "search_aliases": [str(item)[:80] for item in (raw.get("search_aliases") or []) if str(item).strip()][:16],
        "curator_provider": "deepseek",
        "curator_model": settings.deepseek_model,
        "curator_version": settings.memory_curator_version or CURATOR_VERSION,
    }


def _as_loop(item, *, event_ids: list[str]) -> dict[str, Any] | None:
    if isinstance(item, str) and item.strip():
        item = {"title": item.strip(), "status": "open"}
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or item.get("text") or "").strip()
    if not title:
        return None
    status = str(item.get("status") or "open").strip().lower()
    if status not in ALLOWED_LOOP_STATUS:
        status = "open"
    evidence = str(item.get("evidence_type") or "inferred")
    if evidence not in ALLOWED_EVIDENCE:
        evidence = "inferred"
    confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.6)))
    if status == "resolved" and evidence not in {"owner_asserted", "verified_system"} and confidence < 0.85:
        status = "open"
    ids = [str(eid) for eid in (item.get("source_event_ids") or event_ids) if eid][:8]
    return {
        "title": title[:200],
        "scope": str(item.get("scope") or "")[:80],
        "status": status,
        "confidence": confidence,
        "evidence_type": evidence,
        "source_event_ids": ids,
    }


def _parse_json(text: str) -> dict[str, Any]:
    blob = (text or "").strip()
    match = _JSON_BLOCK.search(blob)
    if match:
        blob = match.group(1).strip()
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError:
        start = blob.find("{")
        end = blob.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            payload = json.loads(blob[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}


async def _load_events(session: AsyncSession, ids: list[str]) -> list[Event]:
    parsed: list[UUID] = []
    for raw in ids:
        try:
            parsed.append(UUID(str(raw)))
        except ValueError:
            continue
    if not parsed:
        return []
    rows = (await session.execute(select(Event).where(Event.id.in_(parsed)))).scalars().all()
    return list(rows)


async def _apply(session: AsyncSession, event: Event, payload: dict[str, Any]) -> int:
    from app.contracts import EntityRef, MemoryCandidate
    from app.embeddings import get_embedder
    from app.memory.loops import loop_candidate
    from app.memory.writer import MemoryWriter

    candidates: list[MemoryCandidate] = []
    entities = [
        EntityRef(name=row["name"], entity_type=row["entity_type"], role="related")
        for row in payload.get("entities") or []
    ]
    for item in payload.get("memories") or []:
        kind = item["kind"]
        if kind == "open_loop":
            continue
        memory_type = "fact" if kind == "fact" else kind
        if memory_type == "episodic":
            memory_type = "observation"
        payload_body = {
            "subject": item.get("subject"),
            "property": item.get("property"),
            "value": item.get("value"),
            "topic": item.get("subject"),
            "decision": item.get("value") if kind == "decision" else None,
            "kind": {
                "rejection": "rejected_option",
                "hypothesis": "hypothesis",
                "decision": "decision",
            }.get(kind, kind),
            "status": {
                "rejection": "rejected",
                "hypothesis": "active",
            }.get(kind),
            "source_event_ids": payload.get("event_ids") or [str(event.id)],
            "curator_version": payload.get("curator_version"),
            "curator_provider": payload.get("curator_provider"),
            "evidence_type": item.get("evidence_type") or "inferred",
        }
        source_type = "explicit" if item.get("evidence_type") == "owner_asserted" else "inferred"
        candidates.append(
            MemoryCandidate(
                memory_type=memory_type,
                text=item["text"],
                payload=payload_body,
                importance=item.get("importance", 0.5),
                confidence=item.get("confidence", 0.6),
                source_type=source_type,
                entities=entities,
            )
        )
    for item in payload.get("open_loops") or []:
        candidates.append(
            loop_candidate(
                title=item["title"],
                status=item["status"],
                event=event,
                entities=entities,
                importance=0.8,
                confidence=item.get("confidence", 0.6),
                evidence_type=item.get("evidence_type") or "inferred",
                resolution=item.get("status") == "resolved",
                scope=item.get("scope") or None,
            )
        )
    if not candidates:
        return 0
    writer = MemoryWriter(session, embeddings=get_embedder())
    results = await writer.write_all(event, candidates)
    return len(results)


async def _call_deepseek(prompt: str) -> tuple[str, int]:
    from app.contracts import ChatMessage
    from app.gateway.providers import DeepSeekProvider

    provider = DeepSeekProvider(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        default_model=settings.deepseek_model,
    )
    result = await provider.chat(
        [
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(role="user", content=prompt),
        ],
        temperature=0.0,
    )
    text = getattr(result, "text", None) or ""
    usage = getattr(result, "usage", None) or {}
    tokens = int((usage.get("total_tokens") if isinstance(usage, dict) else 0) or 0)
    return text, tokens


def _backoff(attempts: int):
    seconds = min(3600, 30 * (2 ** max(0, int(attempts) - 1)))
    return utcnow() + timedelta(seconds=seconds)


def _mark_retryable(job: MemoryCurationJob, *, error_class: str, detail: str, events: list[Event]) -> None:
    job.status = "permanent_failed" if int(job.attempts or 0) >= MAX_ATTEMPTS else "retryable_failed"
    job.last_error = error_class
    job.result = {
        "error_class": error_class,
        "detail": detail[:300],
        "events": [str(row.id) for row in events],
    }
    job.available_at = _backoff(job.attempts or 1)
    job.completed_at = utcnow() if job.status == "permanent_failed" else None
    note_curator(
        status=job.status,
        event_id=str(events[-1].id) if events else (str(job.event_id) if job.event_id else None),
        events=len(events),
    )


async def run_job(session: AsyncSession, job: MemoryCurationJob) -> None:
    ids = [str(item) for item in (job.source_event_ids or []) if item]
    if job.event_id and str(job.event_id) not in ids:
        ids.append(str(job.event_id))
    events = await _load_events(session, ids)
    if not events:
        job.status = "permanent_failed"
        job.last_error = "no_events"
        job.result = {"error_class": "no_events"}
        job.completed_at = utcnow()
        note_curator(status="permanent_failed", event_id=str(job.event_id) if job.event_id else None)
        return
    if not curator_available():
        _mark_retryable(
            job,
            error_class="provider_unavailable",
            detail="deepseek_unavailable",
            events=events,
        )
        if job.kind == "remember":
            await materialize_cards(session, through_event_id=str(events[-1].id))
        return
    from app.memory.loops import list_loops
    from app.memory.os_health import note_reflection
    from app.memory.state import current_typed

    lines = []
    for row in events:
        speaker = (row.metadata_ or {}).get("speaker") or row.event_type
        lines.append(f"- {row.id} {speaker}: {(row.content or {}).get('text') or ''}"[:500])
    opens = await list_loops(session, k=6)
    decisions = await current_typed(session, "decision", k=4)
    prompt = "NEW EVENTS:\n" + "\n".join(lines)
    if opens:
        prompt += "\nCURRENT OPEN LOOPS:\n" + "\n".join(
            f"- {(row.payload or {}).get('title') or row.text}" for row in opens
        )
    if decisions:
        prompt += "\nCURRENT DECISIONS:\n" + "\n".join(row.text[:160] for row in decisions)
    try:
        raw_text, tokens = await _call_deepseek(prompt)
        parsed = validate_curator_payload(_parse_json(raw_text), event_ids=[str(row.id) for row in events])
        wrote = await _apply(session, events[-1], parsed)
        job.status = "completed"
        job.result = {"memories": wrote, "payload_keys": sorted(parsed)}
        job.completed_at = utcnow()
        note_curator(
            status="completed",
            event_id=str(events[-1].id),
            tokens=tokens,
            cards=1,
            events=len(events),
        )
        note_reflection(lag_events=0)
        await materialize_cards(session, through_event_id=str(events[-1].id))
    except Exception as exc:  # noqa: BLE001 - curator failure is degraded, not amnesia
        _mark_retryable(
            job,
            error_class="provider_error",
            detail=f"{type(exc).__name__}: {exc}",
            events=events,
        )


async def process_curation_jobs(session: AsyncSession, *, limit: int = 4) -> int:
    claimed = await claim_jobs(session, limit=limit)
    for job in claimed:
        await run_job(session, job)
    if claimed:
        await session.commit()
    return len(claimed)


_PENDING: set = set()


def schedule_curation(*, limit: int = 1) -> None:
    """Fire-and-forget Pipeline B. Never called from the PCM path."""

    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run() -> None:
        from app.db import SessionLocal

        try:
            async with SessionLocal() as session:
                await process_curation_jobs(session, limit=limit)
        except Exception:  # noqa: BLE001 - curator failure is degraded, not amnesia
            note_curator(status="failed")

    task = loop.create_task(_run(), name="ev-memory-curate")
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
