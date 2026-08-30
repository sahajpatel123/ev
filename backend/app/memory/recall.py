"""Explicit-recall fusion: raw Events are the authority; memories accelerate.

Fresh implicit retrieval stays elsewhere. This module is for search_memory
and other explicit history questions — never for injecting old topics into
unrelated turns.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import re
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.continuity import classify_memory_intent, wants_historical_truth
from app.memory.episodes import recent_episodes
from app.memory.observe import log_memory
from app.memory.retrieval import Retriever
from app.models import Entity, Event, Memory
from app.utils.text import simple_tokens

_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "did",
        "do",
        "for",
        "give",
        "given",
        "had",
        "have",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "we",
        "what",
        "when",
        "which",
        "you",
    }
)
_NAMING_QUERY = re.compile(
    r"\b(name|named|call|called|calling|give|gave|label|title)\b",
    re.IGNORECASE,
)
_NAMING_LANG = re.compile(
    r"\b(i(?:'m| am) calling|called(?:\s+this|\s+it)?|the name is|"
    r"remember that|named|name is|is called|call (?:this|it))\b",
    re.IGNORECASE,
)
_PROPER = re.compile(
    r"\b(?:Project\s+[A-Z][A-Za-z0-9'-]+(?:\s+[A-Z][A-Za-z0-9'-]+)*|"
    r"[A-Z][a-zA-Z0-9']+(?:\s+[A-Z][a-zA-Z0-9']+){1,4})\b"
)
_CURRENT_TRUTH = re.compile(
    r"\b(now|currently|these days|called now|what(?:'s| is) it called now)\b",
    re.IGNORECASE,
)
_EXPAND_ARMS = (
    "calling this experiment",
    "calling this project",
    "the name is",
    "is called",
    "remember that",
    "project",
    "experiment",
    "named",
)


def expand_recall_queries(query: str) -> list[str]:
    """Search-term reformulation only. Does not invent owner facts."""

    original = (query or "").strip()
    if not original:
        return []
    out: list[str] = [original]
    lowered = original.lower()
    tokens = simple_tokens(original)
    if "experiment" in tokens:
        out.extend(["experiment", "calling this experiment", "project experiment"])
    if "project" in tokens:
        out.extend(["project", "calling this project"])
    if _NAMING_QUERY.search(original):
        out.extend(_EXPAND_ARMS)
        if "thing" in tokens or "it" in lowered.split():
            out.extend(["called", "named", "calling this"])
    seen: set[str] = set()
    unique: list[str] = []
    for arm in out:
        key = arm.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(arm.strip())
    return unique[:8]


def _query_fp(query: str) -> str:
    return hashlib.sha256((query or "").encode("utf-8")).hexdigest()[:12]


def _event_text(event: Event) -> str:
    return str((event.content or {}).get("text") or "").strip()


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _named_value_query(query: str) -> bool:
    return bool(_NAMING_QUERY.search(query or ""))


def _wants_current(query: str) -> bool:
    return bool(_CURRENT_TRUTH.search(query or "")) and not wants_historical_truth(query)


def _idf(token: str, df: dict[str, int], n: int) -> float:
    return math.log((n + 1) / (df.get(token, 0) + 1))


def _score_event(
    *,
    query: str,
    expanded: list[str],
    text: str,
    event_type: str,
    occurred_at: datetime | None,
    df: dict[str, int],
    n_docs: int,
) -> tuple[float, dict[str, float], str]:
    query_tokens = simple_tokens(" ".join(expanded)) - _STOP
    text_tokens = simple_tokens(text)
    content_tokens = text_tokens - _STOP
    union = query_tokens | content_tokens
    lexical = (len(query_tokens & content_tokens) / len(union)) if union else 0.0
    rare = 0.0
    for token in query_tokens & content_tokens:
        rare += _idf(token, df, n_docs)
    rare_norm = min(1.0, rare / 6.0)
    naming = 1.0 if _NAMING_LANG.search(text) else 0.0
    proper = 1.0 if _PROPER.search(text) else 0.0
    if _named_value_query(query) and naming and proper:
        proper = 1.0
        naming = 1.0
    speaker = 1.0 if event_type == "message.user" else 0.12 if event_type == "message.assistant" else 0.35
    now = datetime.now(UTC)
    days = max(0.0, (now - _as_utc(occurred_at)).total_seconds() / 86400.0)
    recency = math.exp(-days / 90.0)
    phrase = 0.0
    lowered = text.lower()
    for arm in expanded[1:]:
        if len(arm) >= 8 and arm.lower() in lowered:
            phrase = 1.0
            break
    score = (
        0.20 * lexical
        + 0.26 * rare_norm
        + 0.18 * naming
        + 0.16 * proper
        + 0.14 * speaker
        + 0.04 * recency
        + 0.02 * phrase
    )
    parts = {
        "lexical": round(lexical, 4),
        "rare": round(rare_norm, 4),
        "naming": naming,
        "proper": proper,
        "speaker": speaker,
        "recency": round(recency, 4),
        "phrase": phrase,
    }
    reason = "owner_naming" if naming and speaker >= 1.0 else "lexical" if lexical >= 0.04 else "weak"
    return score, parts, reason


_GENERIC = _STOP | {
    "call",
    "called",
    "calling",
    "experiment",
    "feature",
    "give",
    "gave",
    "name",
    "named",
    "originally",
    "project",
    "remember",
    "thing",
}


_WEAK_SPECIFIC = {
    "about",
    "it's",
    "its",
    "names",
    "that's",
    "thats",
    "these",
    "those",
    "what",
    "what's",
    "whats",
    "when",
    "which",
    "who",
    "whose",
}


def _stems(tokens: set[str]) -> set[str]:
    out = set(tokens)
    for token in tokens:
        if token.endswith("s") and len(token) > 4:
            out.add(token[:-1])
        else:
            out.add(token + "s")
    return out


def _supported(query: str, parts: dict[str, float], text: str) -> bool:
    specific = simple_tokens(query) - _GENERIC
    distinctive = {
        token
        for token in specific
        if token not in _WEAK_SPECIFIC and len(token) >= 4 and "'" not in token
    }
    text_tokens = simple_tokens(text)
    if distinctive and not (_stems(distinctive) & _stems(text_tokens)):
        return False
    if distinctive and (_stems(distinctive) & _stems(text_tokens)):
        return True
    if parts["lexical"] >= 0.04:
        return True
    if parts["phrase"] >= 1.0:
        return True
    return bool(_named_value_query(query) and parts["naming"] >= 1.0 and parts["speaker"] >= 1.0)


async def build_explicit_recall_payload(
    session: AsyncSession,
    query: str,
    *,
    k: int = 10,
    memory_type_hint: str | None = None,
) -> dict:
    started = time.perf_counter()
    intent = classify_memory_intent(query)
    expanded = expand_recall_queries(query)
    log_memory(
        "memory.recall_started",
        extra={
            "intent": intent,
            "query_chars": len(query or ""),
            "query_fp": _query_fp(query),
            "k": k,
        },
    )
    log_memory(
        "memory.query_expanded",
        extra={"arms": len(expanded), "named_value": _named_value_query(query)},
    )
    try:
        from app.memory.state import classify_temporal_query

        temporal = classify_temporal_query(query)
        log_memory(
            "memory.temporal_query",
            extra={"mode": temporal.mode, "query_fp": _query_fp(query)},
        )
        events, event_meta = await _search_events(
            session, query, expanded, k=max(12, k), until=temporal.until or temporal.as_of
        )
        log_memory("memory.event_search", extra={"candidates": len(events)})
        memories, semantic_ms = await _search_memories(
            session,
            query,
            k=max(12, k),
            memory_type_hint=memory_type_hint,
            as_of=temporal.as_of if temporal.mode in {"as_of", "historical"} else None,
            include_historical=temporal.mode in {"historical", "solved", "as_of", "changes"},
        )
        log_memory("memory.semantic_search", extra={"candidates": len(memories)})
        episodes = await recent_episodes(session, k=5)
        log_memory("memory.episode_search", extra={"candidates": len(episodes)})
        entities = await _search_entities(session, query, expanded)
        neighbors = await _expand_neighbors(session, events[:4])
        log_memory("memory.neighbor_expand", extra={"windows": len(neighbors)})
        evidence = _pack_evidence(
            query,
            events=events,
            memories=memories,
            episodes=episodes,
            entities=entities,
            neighbors=neighbors,
            k=max(1, min(k, 8)),
        )
        facet = await _facet_pack(session, temporal)
        if facet.get("evidence_extra"):
            evidence = list(facet["evidence_extra"]) + evidence
            evidence = evidence[: max(1, min(k, 12))]
        packed_ms = int((time.perf_counter() - started) * 1000)
        contains = any(
            bool(_PROPER.search(str(item.get("text") or "")))
            or _NAMING_LANG.search(str(item.get("text") or ""))
            for item in evidence
        )
        log_memory(
            "memory.recall_pack",
            extra={
                "evidence_count": len(evidence),
                "intent": intent,
                "elapsed_ms": packed_ms,
                "semantic_ms": semantic_ms,
                "has_owner_event": any(item.get("source") == "owner" for item in evidence),
            },
        )
        log_memory(
            "memory.tool_output_sent",
            extra={
                "contains_naming_or_proper": contains,
                "count": len(evidence),
            },
        )
        results = [
            {
                "id": item.get("id"),
                "text": item.get("text"),
                "memory_type": item.get("memory_type") or item.get("kind"),
                "score": item.get("score"),
                "date": item.get("when"),
                "provenance": item.get("provenance") or [],
            }
            for item in evidence
        ]
        return {
            "intent": "explicit_recall" if intent != "fresh" else intent,
            "question": (query or "")[:240],
            "count": len(evidence),
            "evidence": evidence,
            "results": results,
            "timeline": [
                {
                    "id": row["id"],
                    "occurred_at": row["when"],
                    "source": row.get("event_source"),
                    "event_type": row.get("event_type"),
                    "text": row["text"],
                    "score": row.get("score"),
                }
                for row in events[:8]
                if row.get("kind") == "event"
            ],
            "degraded": False,
            "grounding": "evidence" if evidence else "no_reliable_record",
            "elapsed_ms": packed_ms,
            "facet": temporal.mode,
            "open_loops": facet.get("open_loops") or [],
            "decisions": facet.get("decisions") or [],
            "changes": facet.get("changes"),
            "project_state": facet.get("project_state"),
        }
    except Exception:  # noqa: BLE001 - tool must not crash the live turn
        log_memory("memory.degraded", extra={"error": "explicit_recall_failed"})
        return {
            "intent": intent,
            "question": (query or "")[:240],
            "count": 0,
            "evidence": [],
            "results": [],
            "timeline": [],
            "degraded": True,
            "grounding": "no_reliable_record",
        }


async def _facet_pack(session, temporal) -> dict:
    from app.memory.loops import list_loops, loop_public, rank_open_loops
    from app.memory.state import get_changes, get_project_state, leave_off_packet

    extra: list[dict] = []
    pack: dict = {"open_loops": [], "decisions": [], "changes": None, "project_state": None, "evidence_extra": extra}
    if temporal.mode == "leave_off":
        packet = await leave_off_packet(session)
        pack["open_loops"] = packet.get("open_loops") or []
        pack["decisions"] = packet.get("decisions") or []
        pack["project_state"] = packet.get("current_state")
        for item in pack["open_loops"][:4]:
            extra.append(
                {
                    "id": item.get("id"),
                    "source": "memory",
                    "when": item.get("when"),
                    "text": item.get("title"),
                    "kind": "open_loop",
                    "memory_type": "open_loop",
                    "confidence": "current_state",
                    "score": 0.9,
                    "provenance": item.get("source_event_ids") or [],
                }
            )
        return pack
    if temporal.mode == "still_open":
        rows = rank_open_loops(await list_loops(session, k=12), k=8)
        pack["open_loops"] = [loop_public(row) for row in rows]
        for item in pack["open_loops"]:
            extra.append(
                {
                    "id": item.get("id"),
                    "source": "memory",
                    "when": item.get("when"),
                    "text": item.get("title"),
                    "kind": "open_loop",
                    "memory_type": "open_loop",
                    "confidence": "current_state",
                    "score": 0.92,
                    "provenance": item.get("source_event_ids") or [],
                }
            )
        return pack
    if temporal.mode == "solved":
        rows = await list_loops(session, status="resolved", k=8)
        pack["open_loops"] = [loop_public(row) for row in rows]
        for item in pack["open_loops"]:
            extra.append(
                {
                    "id": item.get("id"),
                    "source": "memory",
                    "when": item.get("when"),
                    "text": item.get("title"),
                    "kind": "open_loop",
                    "memory_type": "open_loop",
                    "confidence": "historical",
                    "score": 0.9,
                    "provenance": item.get("resolution_event_ids") or item.get("source_event_ids") or [],
                }
            )
        return pack
    if temporal.mode == "changes":
        pack["changes"] = await get_changes(session, since=temporal.since, until=temporal.until)
        return pack
    if temporal.mode in {"as_of", "historical"}:
        from app.memory.state import memories_as_of

        boundary = temporal.as_of or temporal.until
        if boundary is not None:
            rows = await memories_as_of(session, boundary=boundary, k=16)
            pack["project_state"] = {
                "as_of": boundary.isoformat(),
                "memories": [
                    {
                        "id": str(row.id),
                        "text": row.text,
                        "memory_type": row.memory_type,
                        "is_current": row.is_current,
                    }
                    for row in rows
                ],
            }
        else:
            pack["project_state"] = await get_project_state(session)
        return pack
    return pack


async def _search_events(
    session: AsyncSession,
    query: str,
    expanded: list[str],
    *,
    k: int,
    until=None,
) -> tuple[list[dict], dict]:
    stmt = (
        select(Event)
        .where(
            Event.tombstoned_at.is_(None),
            Event.event_type.in_(("message.user", "message.assistant", "voice.transcript")),
            Event.privacy_level != "never_send_to_model",
            Event.privacy_level != "sensitive",
        )
        .order_by(Event.occurred_at.desc())
            .limit(800)
    )
    if until is not None:
        stmt = stmt.where(Event.occurred_at <= until)
    rows = list((await session.execute(stmt)).scalars().all())
    try:
        from app.memory.index import search_event_ids

        extra_ids = await search_event_ids(session, query, k=max(24, k))
        have = {row.id for row in rows}
        missing = [event_id for event_id in extra_ids if event_id not in have]
        if missing:
            extra = list(
                (await session.execute(select(Event).where(Event.id.in_(missing)))).scalars().all()
            )
            rows = extra + rows
    except Exception:  # noqa: BLE001 - lexical scan remains authoritative
        pass
    df: dict[str, int] = {}
    texts: list[tuple[Event, str]] = []
    for event in rows:
        text = _event_text(event)
        texts.append((event, text))
        for token in simple_tokens(text):
            df[token] = df.get(token, 0) + 1
    n_docs = max(1, len(texts))
    scored: list[dict] = []
    for event, text in texts:
        if not text:
            continue
        score, parts, reason = _score_event(
            query=query,
            expanded=expanded,
            text=text,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            df=df,
            n_docs=n_docs,
        )
        selected = _supported(query, parts, text)
        if not selected:
            continue
        scored.append(
            {
                "id": str(event.id),
                "when": event.occurred_at.isoformat() if event.occurred_at else None,
                "text": text[:400],
                "score": round(score, 4),
                "kind": "event",
                "memory_type": "event",
                "source": "owner" if event.event_type == "message.user" else "evie",
                "confidence": "exact_owner_event"
                if event.event_type == "message.user"
                else "assistant_turn",
                "event_type": event.event_type,
                "event_source": event.source,
                "conversation_id": str(event.conversation_id) if event.conversation_id else None,
                "occurred_at": event.occurred_at,
                "parts": parts,
                "reason": reason,
            }
        )
    scored.sort(key=lambda row: row["score"], reverse=True)
    if _named_value_query(query) and not wants_historical_truth(query):
        scored.sort(
            key=lambda row: (
                0 if row.get("source") == "owner" and row.get("parts", {}).get("naming") else 1,
                -_as_utc(row.get("occurred_at")).timestamp(),
                -float(row.get("score") or 0),
            )
        )
    elif wants_historical_truth(query):
        scored.sort(
            key=lambda row: (
                0 if row.get("source") == "owner" and row.get("parts", {}).get("naming") else 1,
                _as_utc(row.get("occurred_at")),
            )
        )
    for row in scored[:6]:
        parts = row.get("parts") or {}
        log_memory(
            "memory.rerank",
            extra={
                "event_id": row["id"],
                "lexical": parts.get("lexical"),
                "recency": parts.get("recency"),
                "speaker": "user" if row.get("event_type") == "message.user" else "assistant",
                "selected": True,
                "reason": row.get("reason"),
            },
        )
    return scored[:k], {"scanned": len(rows)}


async def _search_memories(
    session: AsyncSession,
    query: str,
    *,
    k: int,
    memory_type_hint: str | None,
    as_of=None,
    include_historical: bool = False,
) -> tuple[list[dict], int]:
    started = time.perf_counter()
    retriever = Retriever(session)
    historical = wants_historical_truth(query) or include_historical
    hits = await retriever.search(
        query,
        k=k,
        access="model",
        min_score=0.0,
        include_historical=historical,
        as_of=as_of,
        memory_types=None,
    )
    rows: list[dict] = []
    hint = (memory_type_hint or "").strip()
    for hit in hits:
        boost = 0.08 if hint and hit.memory_type == hint else 0.0
        if not _memory_supported(query, hit.text):
            continue
        rows.append(
            {
                "id": hit.memory_id,
                "when": hit.event_time.isoformat() if hit.event_time else None,
                "text": (hit.text or "")[:400],
                "score": round(hit.score + boost, 4),
                "kind": "memory",
                "memory_type": hit.memory_type,
                "source": "memory",
                "confidence": "historical_semantic"
                if historical
                else "semantic_memory",
                "provenance": hit.source_event_ids,
            }
        )
    rows.sort(key=lambda row: row["score"], reverse=True)
    return rows[:k], int((time.perf_counter() - started) * 1000)


def _memory_supported(query: str, text: str) -> bool:
    distinctive = {
        token
        for token in simple_tokens(query) - _GENERIC
        if token not in _WEAK_SPECIFIC and len(token) >= 4
    }
    text_tokens = simple_tokens(text)
    if distinctive:
        return bool(_stems(distinctive) & _stems(text_tokens))
    left = simple_tokens(query) - _STOP
    if left and text_tokens and (left & text_tokens):
        return True
    return bool(_named_value_query(query) and (_NAMING_LANG.search(text) or _PROPER.search(text)))


async def _search_entities(
    session: AsyncSession, query: str, expanded: list[str]
) -> list[dict]:
    tokens = [token for token in simple_tokens(" ".join(expanded)) if token not in _STOP and len(token) >= 3]
    if not tokens:
        return []
    rows = list((await session.execute(select(Entity).limit(400))).scalars().all())
    hits: list[dict] = []
    for row in rows:
        blob = " ".join([row.name, " ".join(row.aliases or [])]).lower()
        if any(token in blob for token in tokens):
            hits.append(
                {
                    "id": str(row.id),
                    "when": None,
                    "text": f"{row.entity_type}: {row.name}",
                    "score": 0.55,
                    "kind": "entity",
                    "memory_type": "entity",
                    "source": "entity",
                    "confidence": "entity",
                }
            )
    return hits[:6]


async def _expand_neighbors(session: AsyncSession, top_events: list[dict]) -> list[dict]:
    extra: list[dict] = []
    seen: set[str] = {str(row.get("id")) for row in top_events}
    for row in top_events:
        occurred = row.get("occurred_at")
        conversation_id = row.get("conversation_id")
        if occurred is None:
            continue
        moment = _as_utc(occurred)
        stmt = (
            select(Event)
            .where(
                Event.tombstoned_at.is_(None),
                Event.event_type.in_(("message.user", "message.assistant")),
                Event.occurred_at >= moment - timedelta(minutes=20),
                Event.occurred_at <= moment + timedelta(minutes=20),
            )
            .order_by(Event.occurred_at.asc())
            .limit(16)
        )
        if conversation_id:
            from uuid import UUID

            with contextlib.suppress(ValueError):
                stmt = stmt.where(Event.conversation_id == UUID(str(conversation_id)))
        nearby = list((await session.execute(stmt)).scalars().all())
        for event in nearby:
            key = str(event.id)
            if key in seen:
                continue
            text = _event_text(event)
            if not text:
                continue
            seen.add(key)
            extra.append(
                {
                    "id": key,
                    "when": event.occurred_at.isoformat() if event.occurred_at else None,
                    "text": text[:400],
                    "score": 0.42,
                    "kind": "neighbor",
                    "memory_type": "event",
                    "source": "owner" if event.event_type == "message.user" else "evie",
                    "confidence": "neighbor_event",
                    "event_type": event.event_type,
                }
            )
            if len(extra) >= 8:
                return extra
    return extra


def _pack_evidence(
    query: str,
    *,
    events: list[dict],
    memories: list[dict],
    episodes: list[Memory],
    entities: list[dict],
    neighbors: list[dict],
    k: int,
) -> list[dict]:
    episode_rows = [
        {
            "id": str(row.id),
            "when": row.event_time.isoformat() if row.event_time else None,
            "text": (row.text or "")[:240],
            "score": 0.45,
            "kind": "episode",
            "memory_type": "summary",
            "source": "episode",
            "confidence": "episode_summary",
        }
        for row in episodes
        if (row.text or "").strip() and _memory_supported(query, row.text)
    ]
    merged: list[dict] = []
    seen: set[str] = set()
    for group in (events, neighbors, memories, episode_rows, entities):
        for item in group:
            key = f"{item.get('kind')}:{item.get('id')}:{item.get('text')}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    owner_first = [item for item in merged if item.get("source") == "owner"]
    rest = [item for item in merged if item.get("source") != "owner"]
    if _wants_current(query):
        semantic = [item for item in rest if item.get("kind") == "memory"]
        rest = [item for item in rest if item.get("kind") != "memory"]
        ordered = semantic + owner_first + rest
    elif wants_historical_truth(query):
        ordered = owner_first + rest
    else:
        ordered = owner_first + rest
    packed = []
    for item in ordered:
        packed.append(
            {
                "id": item.get("id"),
                "source": item.get("source"),
                "when": item.get("when"),
                "text": item.get("text"),
                "kind": item.get("kind"),
                "memory_type": item.get("memory_type"),
                "confidence": item.get("confidence"),
                "score": item.get("score"),
                "provenance": item.get("provenance") or [],
            }
        )
        if len(packed) >= k:
            break
    return packed
