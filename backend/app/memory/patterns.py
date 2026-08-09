"""Behavioral pattern detection: repeated behavior with evidence, frequency, confidence."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event, Memory, MemoryEvent
from app.utils.text import fingerprint, normalize_text, utcnow

DECISION_RE = r"\b(decided|decision|choose|choosing|compare|comparing|which one|which model)\b"
TOOL_CHURN_RE = r"\b(switched|switching|moved to|migrating|trying out|switching to)\b"
QUESTION_RE = r"\?"


def classify_topic(text: str) -> tuple[str, str]:
    """Return (topic, kind) for one event."""
    lowered = text.lower()
    mention = next((tok for tok in lowered.split() if tok.startswith("@")), None)
    if mention:
        topic = mention
    elif re.search(DECISION_RE, lowered) or re.search(TOOL_CHURN_RE, lowered):
        topic = " ".join(lowered.split()[:5])
    else:
        topic = " ".join(lowered.split()[:3])
    if not topic:
        return "", "unknown"

    if re.search(DECISION_RE, lowered):
        kind = "research_loop"
    elif re.search(TOOL_CHURN_RE, lowered):
        kind = "tool_churn"
    elif text.rstrip().endswith("?"):
        kind = "repeated_question"
    else:
        kind = "repeated_topic"
    return topic, kind


class PatternEngine:
    """Derives repeated-behavior patterns from events (non-destructive)."""

    def __init__(self, session: AsyncSession, embeddings=None) -> None:
        self.session = session
        self.embeddings = embeddings

    async def analyze(
        self,
        *,
        window_days: int = 30,
        min_count: int = 3,
        as_of=None,
    ) -> list[str]:
        """Derive behavioral patterns. `as_of` replays a historical analysis job."""
        anchor = as_of or utcnow()
        since = anchor - timedelta(days=window_days)
        stmt = select(Event).where(
            Event.occurred_at >= since,
            Event.event_type.in_(["message.user", "note", "voice", "share", "text"]),
        )
        if as_of is not None:
            stmt = stmt.where(Event.occurred_at <= as_of)
        else:
            stmt = stmt.where(Event.tombstoned_at.is_(None))
        rows = (await self.session.execute(stmt)).scalars().all()

        groups: dict[tuple[str, str], list[Event]] = defaultdict(list)
        for event in rows:
            text = (event.content or {}).get("text") or ""
            if not text.strip():
                continue
            topic, kind = classify_topic(text)
            if topic:
                groups[(normalize_text(topic), kind)].append(event)

        written: list[str] = []
        for (topic, kind), events in groups.items():
            if len(events) < min_count:
                continue
            count = len(events)
            event_ids = [str(e.id) for e in events]
            first_observed = min(e.occurred_at for e in events)
            latest_observed = max(e.occurred_at for e in events)
            confidence = round(min(0.95, 0.5 + 0.08 * count), 3)
            payload = {
                "behavior": f"Repeatedly engaged with '{topic}'",
                "topic": topic,
                "kind": kind,
                "count": count,
                "window_days": window_days,
                "first_observed": first_observed.isoformat(),
                "latest_observed": latest_observed.isoformat(),
                "evidence": event_ids,
                "frequency": round(count / window_days, 3),
            }
            text = (
                f"Pattern ({kind}): '{topic}' engaged {count} times in the last "
                f"{window_days} days (first {first_observed.date().isoformat()}, "
                f"latest {latest_observed.date().isoformat()})."
            )
            memory_id = await self._write_pattern(
                topic,
                kind,
                text,
                payload,
                confidence,
                events,
                latest_observed,
            )
            written.append(memory_id)
        return written

    async def _write_pattern(
        self,
        topic: str,
        kind: str,
        text: str,
        payload: dict,
        confidence: float,
        events: list[Event],
        latest_observed,
    ) -> str:
        existing = (
            await self.session.execute(
                select(Memory).where(
                    Memory.memory_type == "pattern",
                    Memory.is_current.is_(True),
                    Memory.redacted.is_(False),
                )
            )
        ).scalars().all()
        match = next(
            (
                m
                for m in existing
                if normalize_text((m.payload or {}).get("topic") or "") == topic
                and (m.payload or {}).get("kind") == kind
            ),
            None,
        )
        if match and (match.payload or {}).get("count", 0) >= payload["count"]:
            # Same or stronger evidence already stored; just add provenance.
            await self._link_events(match, events)
            return str(match.id)

        memory = Memory(
            memory_type="pattern",
            text=text,
            payload=payload,
            importance=0.6,
            confidence=confidence,
            source_type="derived",
            privacy_level="normal",
            event_time=latest_observed,
            valid_from=latest_observed,
            version_group=match.version_group if match else uuid4(),
            version=(match.version + 1) if match else 1,
            supersedes_id=match.id if match else None,
            reason_for_change="Pattern recomputed" if match else None,
            fingerprint=fingerprint({"memory_type": "pattern", "topic": topic, "kind": kind}),
        )
        if self.embeddings is not None:
            try:
                memory.embedding = (await self.embeddings.embed([text]))[0]
            except Exception:
                memory.embedding = None
        if match:
            match.is_current = False
            match.superseded_by_id = memory.id
            match.valid_until = memory.valid_from
        self.session.add(memory)
        await self.session.flush()
        await self._link_events(memory, events)
        return str(memory.id)

    async def _link_events(self, memory: Memory, events: list[Event]) -> None:
        existing_rows = (
            await self.session.execute(
                select(MemoryEvent).where(MemoryEvent.memory_id == memory.id)
            )
        ).scalars().all()
        existing_event_ids = {row.event_id for row in existing_rows}
        for event in events:
            if event.id not in existing_event_ids:
                self.session.add(MemoryEvent(memory_id=memory.id, event_id=event.id))

    async def decision_loops(
        self,
        *,
        window_days: int = 30,
        min_count: int = 2,
    ) -> list[dict]:
        """Convenience wrapper around decision-memory grouping."""
        from app.ev.decisions import find_decision_loops

        return await find_decision_loops(
            self.session,
            window_days=window_days,
            min_count=min_count,
        )
