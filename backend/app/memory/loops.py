"""Open loops: unresolved project/conversation state. Stored as Memory rows.

Not a second database. Current open loops are is_current + payload.status
in {open, blocked, waiting}. Resolved loops stay historically available.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import EntityRef, MemoryCandidate
from app.ev.continuity import is_hypothetical
from app.memory.observe import log_memory
from app.models import Memory
from app.utils.text import normalize_text, simple_tokens, utcnow

OPEN_STATUSES = frozenset({"open", "blocked", "waiting", "unknown"})
CLOSED_STATUSES = frozenset({"resolved", "abandoned", "superseded"})
ALL_STATUSES = OPEN_STATUSES | CLOSED_STATUSES

_STOP = frozenset(
    {
        "a",
        "actually",
        "an",
        "and",
        "but",
        "can't",
        "cannot",
        "does",
        "doesn't",
        "dont",
        "evie",
        "fixed",
        "is",
        "not",
        "now",
        "still",
        "the",
        "through",
        "unresolved",
        "working",
        "works",
    }
)
_CASUAL = re.compile(
    r"\b(drinking|coffee|interesting|watch a movie|that's funny|lol|haha)\b",
    re.IGNORECASE,
)
_QUOTED_OTHER = re.compile(
    r"("
    r"\b(?:said|says|told me)\b.{0,80}['\"]"
    r"|"
    r"\b(?!i\b|we\b)[A-Za-z][A-Za-z'-]+\s+(?:said|says|told me)\b"
    r")",
    re.IGNORECASE,
)
_OPEN_HINT = re.compile(
    r"("
    r"\b(still (doesn'?t|does not|can't|cannot|isn'?t)|not working|"
    r"doesn't work|does not work|can't actually|cannot actually|"
    r"can'?t click|cannot click|can't get|cannot get|"
    r"still unresolved|still broken|haven't (decided|finished|fixed)|"
    r"have not (decided|finished)|come back to this|stuck on|"
    r"waiting for|unreliable|partial|unverified)\b"
    r")",
    re.IGNORECASE,
)
_RESOLVE_HINT = re.compile(
    r"("
    r"\b(now works|is (?:now )?fixed|is working now|that's fixed|"
    r"that(?:'s| is) (?:fixed|working|resolved) now|"
    r"we (?:fixed|solved|resolved)|issue is fixed|"
    r"that issue is (?:fixed|working|resolved)|it's fixed now)\b"
    r")",
    re.IGNORECASE,
)
_GENERIC_RESOLVE = re.compile(
    r"\b(that (?:issue|problem|bug|one) is (?:fixed|working|resolved)|"
    r"it(?:'s| is) (?:fixed|working) now|that's (?:fixed|working) now)\b",
    re.IGNORECASE,
)
_REJECT = re.compile(
    r"("
    r"\bdon'?t (?:build|create|rewrite)\b|"
    r"\bwe(?:'re| are) not (?:rewriting|building|creating)\b|"
    r"\bno memory_v[0-9]\b|"
    r"\bdo not (?:build|create|rewrite)\b"
    r")",
    re.IGNORECASE,
)
_HYPOTHESIS = re.compile(
    r"\b(may originate|might be (?:caused|coming)|could be (?:caused|coming)|"
    r"hypothesis is|i think it(?:'s| is) because)\b",
    re.IGNORECASE,
)


def _stem_tokens(text: str) -> set[str]:
    stems: set[str] = set()
    for token in simple_tokens(text) - _STOP:
        if len(token) <= 2:
            continue
        stems.add(token)
        if token.endswith("ing") and len(token) > 5:
            stems.add(token[:-3])
        elif token.endswith("ed") and len(token) > 4:
            stems.add(token[:-2])
        elif token.endswith("s") and not token.endswith("ss") and len(token) > 3:
            stems.add(token[:-1])
    return stems


def canonical_loop_key(text: str, *, scope: str = "") -> str:
    tokens = sorted(_stem_tokens(f"{scope} {text}"))[:6]
    return " ".join(tokens) or normalize_text(text)[:80]


def infer_scope(text: str, entities: list[EntityRef] | None = None) -> str:
    for entity in entities or []:
        if entity.entity_type in {"project", "topic"} and entity.name:
            return entity.name[:80]
    lowered = (text or "").lower()
    for name in ("mac control", "memory os", "safari", "music", "evie"):
        if name in lowered:
            return name.title() if name != "evie" else "Evie"
    return "Evie"


def loop_candidate(
    *,
    title: str,
    status: str,
    event,
    entities: list[EntityRef],
    importance: float,
    confidence: float,
    evidence_type: str = "owner_asserted",
    resolution: bool = False,
    scope: str | None = None,
) -> MemoryCandidate:
    scope = (scope or "").strip() or infer_scope(title, entities)
    key = canonical_loop_key(title, scope=scope)
    payload = {
        "kind": "open_loop",
        "scope_type": "project",
        "scope": scope,
        "title": title[:200],
        "status": status if status in ALL_STATUSES else "open",
        "loop_key": key,
        "evidence_type": evidence_type,
        "source_event_ids": [str(event.id)],
        "resolution_event_ids": [str(event.id)] if resolution else [],
        "curator_version": "1.1",
    }
    prefix = "Resolved" if status == "resolved" else "Open"
    return MemoryCandidate(
        memory_type="open_loop",
        text=f"{prefix}: {scope} — {title}"[:400],
        payload=payload,
        importance=importance,
        confidence=confidence,
        source_type="explicit" if evidence_type == "owner_asserted" else "inferred",
        privacy_level=event.privacy_level,
        event_time=event.occurred_at,
        entities=entities,
    )


def extract_loop_candidates(event, text: str, entities: list[EntityRef]) -> list[MemoryCandidate]:
    blob = (text or "").strip()
    if not blob or is_hypothetical(blob) or _CASUAL.search(blob) or _QUOTED_OTHER.search(blob):
        return []
    if event.event_type == "message.assistant":
        return []
    out: list[MemoryCandidate] = []
    if _RESOLVE_HINT.search(blob):
        out.append(
            loop_candidate(
                title=_title_from(blob),
                status="resolved",
                event=event,
                entities=entities,
                importance=0.86,
                confidence=0.9,
                resolution=True,
            )
        )
        return out
    if _OPEN_HINT.search(blob):
        out.append(
            loop_candidate(
                title=_title_from(blob),
                status="open",
                event=event,
                entities=entities,
                importance=0.84,
                confidence=0.88,
            )
        )
    return out


def extract_rejection_candidate(event, text: str, entities: list[EntityRef]) -> MemoryCandidate | None:
    blob = (text or "").strip()
    if not blob or is_hypothetical(blob) or _QUOTED_OTHER.search(blob):
        return None
    if not _REJECT.search(blob):
        return None
    topic = infer_scope(blob, entities)
    return MemoryCandidate(
        memory_type="rejection",
        text=f"Rejected: {blob[:220]}",
        payload={
            "kind": "rejected_option",
            "topic": topic,
            "value": blob[:240],
            "status": "rejected",
            "evidence_type": "owner_asserted",
            "source_event_ids": [str(event.id)],
        },
        importance=0.9,
        confidence=0.92,
        source_type="explicit",
        privacy_level=event.privacy_level,
        event_time=event.occurred_at,
        entities=entities,
    )


def extract_hypothesis_candidate(event, text: str, entities: list[EntityRef]) -> MemoryCandidate | None:
    blob = (text or "").strip()
    if not blob or is_hypothetical(blob) or _QUOTED_OTHER.search(blob) or blob.endswith("?"):
        return None
    if not _HYPOTHESIS.search(blob):
        return None
    topic = infer_scope(blob, entities)
    return MemoryCandidate(
        memory_type="hypothesis",
        text=f"Hypothesis: {blob[:220]}",
        payload={
            "kind": "hypothesis",
            "topic": topic,
            "value": blob[:240],
            "status": "active",
            "evidence_type": "inferred",
            "source_event_ids": [str(event.id)],
        },
        importance=0.55,
        confidence=0.6,
        source_type="inferred",
        privacy_level=event.privacy_level,
        event_time=event.occurred_at,
        entities=entities,
    )


def _title_from(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip()).rstrip(".")
    return cleaned[:200]


def token_overlap(a: str, b: str) -> float:
    left = _stem_tokens(a)
    right = _stem_tokens(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


async def find_similar_loop(session: AsyncSession, candidate: MemoryCandidate) -> Memory | None:
    """Update the same loop when overlap is high; otherwise keep separate."""

    payload = candidate.payload or {}
    scope = normalize_text(str(payload.get("scope") or ""))
    key = normalize_text(str(payload.get("loop_key") or ""))
    title = str(payload.get("title") or candidate.text or "")
    generic = bool(_GENERIC_RESOLVE.search(title)) and payload.get("status") == "resolved"
    rows = (
        await session.execute(
            select(Memory).where(
                Memory.memory_type == "open_loop",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
        )
    ).scalars().all()
    best: Memory | None = None
    best_score = 0.0
    for row in rows:
        other = row.payload or {}
        other_scope = normalize_text(str(other.get("scope") or ""))
        other_key = normalize_text(str(other.get("loop_key") or ""))
        if key and other_key == key:
            return row
        if scope and other_scope and scope != other_scope and not generic:
            continue
        score = token_overlap(title, str(other.get("title") or row.text or ""))
        if other_key and key:
            score = max(score, token_overlap(key, other_key))
        if score > best_score:
            best_score = score
            best = row
    open_rows = [
        row
        for row in rows
        if str((row.payload or {}).get("status") or "open").lower() in OPEN_STATUSES
    ]
    if generic:
        if best is not None and best_score >= 0.2:
            return best
        if open_rows:
            return max(open_rows, key=lambda row: _as_utc(row.event_time) or utcnow())
        return None
    closed = str(payload.get("status") or "").lower() in CLOSED_STATUSES
    if closed and scope:
        same_scope = [
            row
            for row in open_rows
            if normalize_text(str((row.payload or {}).get("scope") or "")) == scope
        ]
        if len(same_scope) == 1:
            return same_scope[0]
        if best is not None and best_score >= 0.2:
            return best
    if best is not None and best_score >= 0.45:
        return best
    return None


async def list_loops(
    session: AsyncSession,
    *,
    status: str | None = None,
    scope: str | None = None,
    k: int = 8,
    include_historical: bool = False,
) -> list[Memory]:
    stmt = select(Memory).where(Memory.memory_type == "open_loop", Memory.redacted.is_(False))
    if not include_historical:
        stmt = stmt.where(Memory.is_current.is_(True))
    stmt = stmt.order_by(Memory.importance.desc(), Memory.event_time.desc()).limit(80)
    rows = list((await session.execute(stmt)).scalars().all())
    wanted = None if not status else {part.strip() for part in status.split(",") if part.strip()}
    scope_norm = normalize_text(scope or "")
    out: list[Memory] = []
    for row in rows:
        payload = row.payload or {}
        row_status = str(payload.get("status") or "open").lower()
        if wanted and row_status not in wanted:
            continue
        if not wanted and not include_historical and row_status not in OPEN_STATUSES:
            continue
        if scope_norm:
            blob = f"{payload.get('scope') or ''} {row.text or ''}".lower()
            if scope_norm not in blob and not all(part in blob for part in scope_norm.split() if len(part) > 2):
                continue
        out.append(row)
        if len(out) >= k:
            break
    return out


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def inherit_loop_identity(prev: Memory, candidate: MemoryCandidate) -> None:
    """Keep the issue identity when a matched loop is resolved or updated."""

    prev_payload = dict(prev.payload or {})
    new_payload = dict(candidate.payload or {})
    if str(new_payload.get("status") or "").lower() in CLOSED_STATUSES:
        new_payload["title"] = prev_payload.get("title") or new_payload.get("title")
        new_payload["scope"] = prev_payload.get("scope") or new_payload.get("scope")
        new_payload["loop_key"] = prev_payload.get("loop_key") or new_payload.get("loop_key")
        new_payload["source_event_ids"] = list(
            dict.fromkeys(
                list(prev_payload.get("source_event_ids") or [])
                + list(new_payload.get("source_event_ids") or [])
            )
        )[:16]
        new_payload["resolution_event_ids"] = list(
            dict.fromkeys(
                list(prev_payload.get("resolution_event_ids") or [])
                + list(new_payload.get("resolution_event_ids") or [])
            )
        )[:16]
        candidate.payload = new_payload
        candidate.text = f"Resolved: {new_payload.get('scope')} — {new_payload.get('title')}"[:400]


def rank_open_loops(rows: list[Memory], *, active_project: str | None = None, k: int = 3) -> list[Memory]:
    now = utcnow()

    def score(row: Memory) -> float:
        payload = row.payload or {}
        base = float(row.importance or 0.5)
        if active_project and active_project.lower() in f"{payload.get('scope') or ''} {row.text}".lower():
            base += 0.15
        stamp = _as_utc(row.event_time) or now
        age_s = max(0.0, (now - stamp).total_seconds())
        recency = max(0.0, 1.0 - (age_s / (14 * 86400)))
        return base + 0.1 * recency

    ordered = sorted(rows, key=score, reverse=True)
    return ordered[:k]


def loop_public(row: Memory) -> dict[str, Any]:
    payload = row.payload or {}
    return {
        "id": str(row.id),
        "title": payload.get("title") or row.text,
        "scope": payload.get("scope"),
        "status": payload.get("status") or "open",
        "importance": row.importance,
        "confidence": row.confidence,
        "evidence_type": payload.get("evidence_type") or row.source_type,
        "when": row.event_time.isoformat() if row.event_time else None,
        "source_event_ids": payload.get("source_event_ids") or [],
        "resolution_event_ids": payload.get("resolution_event_ids") or [],
        "is_current": row.is_current,
    }


def log_loop_transition(status: str, memory_id: UUID | str | None = None) -> None:
    if status == "resolved":
        log_memory("memory.loop_resolved", extra={"memory_id": str(memory_id) if memory_id else None})
    elif status in OPEN_STATUSES:
        log_memory("memory.loop_opened", extra={"memory_id": str(memory_id) if memory_id else None})
    else:
        log_memory("memory.loop_updated", extra={"memory_id": str(memory_id) if memory_id else None, "status": status})
