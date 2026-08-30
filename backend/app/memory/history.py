"""Past-history retrieval — `recall_history` and shadow-memory injection.

Purpose (docs/VOICE_CONTROL_PLAN.md §2): past/history retrieval is a
deliberately separate engine from future event/reminder retrieval. Past
scoring runs the locked hybrid formula over typed, versioned `memories`
with optional time travel; event/reminder retrieval (`get_upcoming_alerts`,
`calendar_read`, ...) stays on its own deadline/priority engine.

This module provides:

* ``recall_history`` — chunked evidence packs (brief|full), time-range
  filters, ``as_of`` version-window time travel, memory-type filters, and
  deterministic cursor pagination. Results always carry provenance
  (``source_event_ids``) and per-component scores from the retriever.
* ``build_shadow_memory`` — a compact, budget-bounded ``SHADOW MEMORY`` text
  block injected into realtime session instructions so a speech-to-speech
  model can answer past questions with zero function calls. Read-only,
  deterministic, privacy-boundary-respecting (``access=model``).

Privacy invariants are enforced by the retriever boundary: ``never_send_to_model``
memories are excluded at SQL; ``sensitive`` requires explicit opt-in (never
defaulted here). Nothing in this module generates model calls.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.retrieval import Retriever
from app.utils.text import token_estimate

# Time-range presets map to a (start_delta, inclusive_end) window relative to now.
TIME_RANGES: dict[str, tuple[timedelta, bool]] = {
    "recent_week": (timedelta(days=7), True),
    "recent_month": (timedelta(days=30), True),
    "last_3_months": (timedelta(days=90), True),
    "last_year": (timedelta(days=365), True),
    "all_time": (timedelta(days=0), False),
}

_MEMORY_TYPE_CHOICES = (
    "decision",
    "goal",
    "preference",
    "fact",
    "observation",
    "episodic",
    "pattern",
    "summary",
    "lesson",
)

_BRIEF_LIMIT_CHARS = 200
_MAX_CURSOR_OFFSET = 500


def _params_fingerprint(params: dict[str, Any]) -> str:
    """Deterministic fingerprint of the retrieval parameters that define a page series."""

    canonical = json.dumps(
        params, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def encode_cursor(offset: int, params_fp: str) -> str:
    """Encode a page cursor. Opaque to callers; bound to the original query params."""

    payload = base64.urlsafe_b64encode(
        json.dumps({"o": int(offset), "f": params_fp}, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return payload


def decode_cursor(cursor: str, params_fp: str) -> int | None:
    """Decode a cursor; returns the page offset, or None when invalid/mismatched."""

    if not cursor or len(cursor) > 512:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or str(data.get("f") or "") != params_fp:
        return None
    try:
        offset = int(data["o"])
    except (KeyError, TypeError, ValueError):
        return None
    if offset < 0 or offset > _MAX_CURSOR_OFFSET:
        return None
    return offset


def _parse_iso_date(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    """Parse an ISO date (YYYY-MM-DD) or date-time; None on empty/invalid input."""

    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        date_only = date.fromisoformat(raw[:10])
        parsed = datetime.combine(
            date_only,
            datetime.max.time().replace(tzinfo=UTC) if end_of_day else datetime.min.time(),
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _resolve_window(
    time_range: str | None,
    start_date: str | None,
    end_date: str | None,
    now: datetime,
) -> tuple[datetime | None, datetime | None]:
    """Resolve the inclusive [start, end) retrieval window from the request.

    ``time_range`` presets apply when no explicit dates are given. Explicit
    start/end dates win over the preset for the side they specify.
    """

    start: datetime | None = None
    end: datetime | None = None
    preset = str(time_range or "all_time").strip().lower()
    delta, bounded = TIME_RANGES.get(preset, (timedelta(days=0), False))
    if bounded:
        start = now - delta
        end = now
    if start_date:
        start = _parse_iso_date(start_date) or start
    if end_date:
        end = _parse_iso_date(end_date, end_of_day=True) or end
    return start, end


def truncate_brief(text: str, limit: int = _BRIEF_LIMIT_CHARS) -> str:
    """Shorten a chunk to a voice-friendly brief at a word boundary."""

    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    boundary = cut.rfind(" ")
    if boundary > limit // 2:
        cut = cut[:boundary]
    return f"{cut.rstrip(' .,;:')} …"


def _chunk_item(
    hit: Any,
    chunk_id: int,
    chunk_mode: str,
) -> dict[str, Any]:
    """One chunk record: brief or full text plus provenance + transparency scores."""

    text = hit.text or ""
    if chunk_mode == "brief":
        text = truncate_brief(text)
    components = dict(hit.components or {})
    return {
        "chunk_id": chunk_id,
        "memory_id": hit.memory_id,
        "text": text,
        "memory_type": hit.memory_type,
        "date": hit.event_time.isoformat() if hit.event_time else None,
        "score": round(hit.score, 4),
        "components": {
            key: round(float(value), 4)
            for key, value in components.items()
            if isinstance(value, (int, float))
        },
        "importance": hit.importance,
        "confidence": hit.confidence,
        "source_type": hit.source_type,
        "source_event_ids": list(hit.source_event_ids or []),
    }


async def search_history(
    session: AsyncSession,
    query: str,
    *,
    k: int = 8,
    time_range: str = "all_time",
    start_date: str | None = None,
    end_date: str | None = None,
    memory_type: str | None = None,
    as_of: str | None = None,
    chunk_mode: str = "brief",
    min_score: float = 0.0,
) -> list[Any]:
    """Run the hybrid retriever and apply the resolved past-window filters.

    ``as_of`` enables version-window time travel (include_historical rows so
    ``valid_from/valid_until`` matters); date windows filter by event_time.
    Returns retriever hits already filtered to the resolved window, sorted by
    score (the retriever's order is preserved).
    """

    retriever = Retriever(session)
    memory_types: list[str] | None = None
    if memory_type:
        memory_types = [str(memory_type)]
    as_of_dt = _parse_iso_date(as_of) if as_of else None
    hits = await retriever.search(
        query,
        k=max(k, 12) + 16,
        access="model",
        memory_types=memory_types,
        include_historical=as_of_dt is not None,
        as_of=as_of_dt,
        min_score=min_score,
    )
    now = datetime.now(UTC)
    start_dt, end_dt = _resolve_window(time_range, start_date, end_date, now)
    if start_dt is None and end_dt is None and as_of_dt is None:
        await _attach_provenance(session, hits)
        return hits
    filtered: list[Any] = []
    for hit in hits:
        if hit.event_time is None:
            continue
        t = hit.event_time
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        t = t.astimezone(UTC)
        if start_dt is not None and t < start_dt:
            continue
        if end_dt is not None and t >= end_dt:
            continue
        filtered.append(hit)
    await _attach_provenance(session, filtered)
    return filtered


async def _attach_provenance(session: AsyncSession, hits: list[Any]) -> None:
    """Attach source-event provenance when the retriever fast-path skipped it.

    The F1.1 implicit-recall fast path omits provenance expansion (it only
    runs for historical/L2/L3 queries). Explicit history recall promises
    provenance on every chunk, so this one batched query fills the gap —
    read-only, deterministic, never fabricates.
    """

    if not hits:
        return
    missing = [h for h in hits if not getattr(h, "source_event_ids", None)]
    if not missing:
        return
    from uuid import UUID as _UUID

    from sqlalchemy import select

    from app.models import MemoryEvent

    ids: list = []
    for hit in missing:
        try:
            ids.append(_UUID(str(hit.memory_id)))
        except ValueError:
            continue
    if not ids:
        return
    rows = (
        await session.execute(
            select(MemoryEvent).where(MemoryEvent.memory_id.in_(ids))
        )
    ).scalars().all()
    prov: dict[str, list[str]] = {}
    for row in rows:
        prov.setdefault(str(row.memory_id), []).append(str(row.event_id))
    for hit in missing:
        hit.source_event_ids = prov.get(str(hit.memory_id), [])


async def recall_history(
    session: AsyncSession,
    query: str,
    *,
    k: int = 8,
    time_range: str = "all_time",
    start_date: str | None = None,
    end_date: str | None = None,
    memory_type: str | None = None,
    as_of: str | None = None,
    chunk_mode: str = "brief",
    cursor: str | None = None,
    min_score: float = 0.0,
) -> dict[str, Any]:
    """Chunked past-history recall with cursor pagination (see module docstring)."""

    started = time.perf_counter()
    query = (query or "").strip()
    if not query:
        return {
            "ok": False,
            "error": "empty_query",
            "count": 0,
            "results": [],
            "spoken": "I need a question to search your history.",
        }
    k = max(1, min(int(k or 8), 20))
    mode = str(chunk_mode or "brief").strip().lower()
    if mode not in {"brief", "full"}:
        mode = "brief"
    fp = _params_fingerprint(
        {
            "q": query,
            "k": k,
            "time_range": time_range,
            "start_date": start_date,
            "end_date": end_date,
            "memory_type": memory_type,
            "as_of": as_of,
        }
    )
    offset = 0
    if cursor:
        decoded = decode_cursor(cursor, fp)
        if decoded is None:
            return {
                "ok": False,
                "error": "invalid_cursor",
                "count": 0,
                "results": [],
                "spoken": "That history page has expired; ask me again.",
            }
        offset = decoded
    hits = await search_history(
        session,
        query,
        k=k,
        time_range=time_range,
        start_date=start_date,
        end_date=end_date,
        memory_type=memory_type,
        as_of=as_of,
        chunk_mode=mode,
        min_score=min_score,
    )
    page = hits[offset : offset + k]
    has_more = offset + k < len(hits)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    as_of_dt = _parse_iso_date(as_of) if as_of else None
    return {
        "ok": True,
        "count": len(page),
        "total": len(hits),
        "query": query,
        "time_range": str(time_range or "all_time"),
        "as_of": as_of_dt.isoformat() if as_of_dt else None,
        "memory_type": memory_type,
        "chunk_mode": mode,
        "offset": offset,
        "results": [
            _chunk_item(hit, offset + index + 1, mode)
            for index, hit in enumerate(page)
        ],
        "has_more": has_more,
        "next_cursor": encode_cursor(offset + k, fp) if has_more else None,
        "elapsed_ms": elapsed_ms,
    }


async def build_shadow_memory(
    session: AsyncSession,
    transcript: str,
    *,
    k: int = 5,
    budget_tokens: int = 900,
    min_score: float = 0.0,
) -> str:
    """Build the compact read-only ``SHADOW MEMORY`` block for one owner turn.

    Returns "" when nothing qualifies. Lines are brief chunks with date, type,
    score, and provenance ids; the block is trimmed to ``budget_tokens`` so
    injection never competes with the voice response budget. Deterministic:
    same memories + transcript produce the same block.
    """

    query = (transcript or "").strip()
    if not query:
        return ""
    k = max(1, min(int(k or 5), 10))
    budget = max(64, int(budget_tokens or 900))
    hits = await search_history(
        session, query, k=k, time_range="all_time", chunk_mode="brief", min_score=min_score
    )
    if not hits:
        return ""
    lines: list[str] = []
    used = token_estimate(
        "SHADOW MEMORY (from the owner's stored history; use only what the "
        "current question needs, silently; never invent history):"
    )
    for hit in hits[:k]:
        item = _chunk_item(hit, len(lines) + 1, "brief")
        date_part = str(item["date"])[:10] if item["date"] else "unknown"
        type_part = str(item["memory_type"] or "memory")
        score = float(item["score"] or 0.0)
        line = (
            f"- [{date_part} · {type_part} · {score:.2f}] {item['text']} "
            f"(memory {item['memory_id']})"
        )
        line_tokens = token_estimate(line)
        if used + line_tokens > budget and lines:
            break
        used += line_tokens
        lines.append(line)
    if not lines:
        return ""
    header = (
        "SHADOW MEMORY (from the owner's stored history; use only what the "
        "current question needs, silently; never invent history):"
    )
    return header + "\n" + "\n".join(lines)