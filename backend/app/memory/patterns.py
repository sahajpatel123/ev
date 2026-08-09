"""Behavioral pattern detection: repeated behavior with evidence, frequency, confidence."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entity, Event, Memory, MemoryEvent
from app.utils.text import fingerprint, normalize_text, utcnow

DECISION_RE = r"\b(decided|decision|choose|choosing|compare|comparing|which one|which model)\b"
TOOL_CHURN_RE = r"\b(switched|switching|moved to|migrating|trying out|switching to)\b"
QUESTION_RE = r"\?"


def _as_aware(value, anchor):
    """SQLite returns naive datetimes; normalize against the analysis anchor."""
    if value.tzinfo is None:
        return value.replace(tzinfo=anchor.tzinfo)
    return value


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
        recent_days: int = 7,
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
        written.extend(
            await self.detect_stalled_goals(
                rows=rows,
                anchor=anchor,
                window_days=window_days,
                recent_days=recent_days,
                min_mentions=max(2, min_count - 1),
            )
        )
        written.extend(
            await self.detect_stalled_projects(
                rows=rows,
                anchor=anchor,
                window_days=window_days,
                recent_days=recent_days,
                min_mentions=max(2, min_count - 1),
            )
        )
        return written

    async def detect_stalled_goals(
        self,
        *,
        rows: Sequence[Event],
        anchor,
        window_days: int,
        recent_days: int,
        min_mentions: int,
    ) -> list[str]:
        """Emit goal_drift patterns when an active goal has recent evidence gaps."""
        recent_cutoff = anchor - timedelta(days=recent_days)
        goal_rows = (
            await self.session.execute(
                select(Memory).where(
                    Memory.memory_type == "goal",
                    Memory.is_current.is_(True),
                    Memory.redacted.is_(False),
                )
            )
        ).scalars().all()
        active_goals = [
            goal
            for goal in goal_rows
            if (goal.payload or {}).get("status", "active") == "active"
        ]
        written: list[str] = []
        for goal in active_goals:
            payload = goal.payload or {}
            goal_text = str(payload.get("goal") or goal.text)[:200]
            tokens = {t for t in re.findall(r"[a-z0-9']+", normalize_text(goal_text)) if len(t) >= 4}
            if not tokens:
                continue
            mentions = [
                event
                for event in rows
                if tokens.intersection(
                    set(re.findall(r"[a-z0-9']+", normalize_text((event.content or {}).get("text") or "")))
                )
            ]
            if len(mentions) < min_mentions:
                continue
            if any(_as_aware(event.occurred_at, anchor) >= recent_cutoff for event in mentions):
                continue
            latest_observed = max(event.occurred_at for event in mentions)
            latest_observed_aware = _as_aware(latest_observed, anchor)
            silence_days = max(0, int((anchor - latest_observed_aware).total_seconds() // 86400))
            count = len(mentions)
            confidence = round(min(0.9, 0.5 + 0.08 * count), 3)
            topic = " ".join(normalize_text(goal_text).split()[:6])
            pattern_payload = {
                "behavior": f"Active goal '{topic}' has not been engaged recently",
                "topic": topic,
                "kind": "goal_drift",
                "goal": goal_text,
                "count": count,
                "window_days": window_days,
                "recent_days": recent_days,
                "silence_days": silence_days,
                "first_observed": min(event.occurred_at for event in mentions).isoformat(),
                "latest_observed": latest_observed_aware.isoformat(),
                "evidence": [str(event.id) for event in mentions],
            }
            text = (
                f"Pattern (goal_drift): goal '{topic}' was engaged {count} times but "
                f"has been quiet for {silence_days} days (window {window_days}d, recent {recent_days}d)."
            )
            memory_id = await self._write_pattern(
                topic,
                "goal_drift",
                text,
                pattern_payload,
                confidence,
                mentions,
                latest_observed,
            )
            written.append(memory_id)
        return written

    async def detect_stalled_projects(
        self,
        *,
        rows: Sequence[Event],
        anchor,
        window_days: int,
        recent_days: int,
        min_mentions: int,
    ) -> list[str]:
        """Emit project_abandonment patterns when a project goes silent."""
        recent_cutoff = anchor - timedelta(days=recent_days)
        entity_rows = (
            await self.session.execute(
                select(Entity).where(Entity.entity_type == "project")
            )
        ).scalars().all()
        names = [normalize_text(entity.name) for entity in entity_rows]
        mentions: dict[str, list[Event]] = defaultdict(list)
        for event in rows:
            text = normalize_text((event.content or {}).get("text") or "")
            for name in names:
                if name and name in text:
                    mentions[name].append(event)
            for token in re.findall(r"@([a-z0-9_]+)", text):
                mentions[token].append(event)

        written: list[str] = []
        for topic, events in mentions.items():
            if len(events) < min_mentions:
                continue
            if any(_as_aware(event.occurred_at, anchor) >= recent_cutoff for event in events):
                continue
            latest_observed = max(event.occurred_at for event in events)
            latest_observed_aware = _as_aware(latest_observed, anchor)
            silence_days = max(0, int((anchor - latest_observed_aware).total_seconds() // 86400))
            count = len(events)
            confidence = round(min(0.9, 0.5 + 0.08 * count), 3)
            pattern_payload = {
                "behavior": f"Project '{topic}' has not been engaged recently",
                "topic": topic,
                "kind": "project_abandonment",
                "count": count,
                "window_days": window_days,
                "recent_days": recent_days,
                "silence_days": silence_days,
                "first_observed": min(event.occurred_at for event in events).isoformat(),
                "latest_observed": latest_observed_aware.isoformat(),
                "evidence": [str(event.id) for event in events],
            }
            text = (
                f"Pattern (project_abandonment): project '{topic}' was engaged {count} "
                f"times but has been quiet for {silence_days} days (window {window_days}d, "
                f"recent {recent_days}d)."
            )
            memory_id = await self._write_pattern(
                topic,
                "project_abandonment",
                text,
                pattern_payload,
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
        existing_count = (match.payload or {}).get("count", 0) if match else 0
        existing_silence = (match.payload or {}).get("silence_days", 0) if match else 0
        if (
            match
            and existing_count >= payload.get("count", 0)
            and existing_silence >= payload.get("silence_days", 0)
        ):
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
