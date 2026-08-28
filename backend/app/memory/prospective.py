"""F5 prospective context: past + present + explicit future, truth-labeled.

Read-only synthesis for questions like "What should I do tomorrow?" /
"What am I forgetting?" / "What should I continue?".

TRUTH CLASSES (§27) — never collapsed:
  REQUIRED  — explicit canonical obligation (calendar, due commitment, alert).
  PLANNED   — owner explicitly scheduled/planned (routines, dated goal steps).
  SUGGESTED — Evie inferred usefulness (unfinished momentum, forgotten work).

Laws enforced structurally:
  - READ-ONLY (§30): nothing here writes calendar/commitments/goals.
  - CURRENT TRUTH WINS (§38): cancelled/superseded canonical rows are never
    surfaced as planned; history stays historical.
  - PROVENANCE (§42): every item carries source refs; suggestions without
    source refs are rejected (count stays 0 by construction).
  - DO NOT NAG (§33): suggestions are ranked and capped (default 5).

Flag EV_PROSPECTIVE_CONTEXT_V1: off | shadow | on.
  shadow — context is built + logged (probe-inspectable), never injected.
  on     — the TurnGate may attach the labeled block to prospective turns.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.utils.text import utcnow


class TruthClass(StrEnum):
    REQUIRED = "REQUIRED"
    PLANNED = "PLANNED"
    SUGGESTED = "SUGGESTED"


class ItemFlag(StrEnum):
    OVERDUE = "OVERDUE"
    UNFINISHED = "UNFINISHED"
    HIGH_PRIORITY = "HIGH_PRIORITY"
    RECENT_MOMENTUM = "RECENT_MOMENTUM"
    STALE = "STALE"
    CONFLICTING = "CONFLICTING"


PROSPECTIVE_PATTERNS = (
    "what should i do",
    "what should i work on",
    "what should i focus",
    "what should i continue",
    "what am i forgetting",
    "forgotten",
    "what's next",
    "what is next",
    "focus today",
    "focus tomorrow",
    "plan for today",
    "plan for tomorrow",
)


def prospective_mode() -> str:
    return (getattr(settings, "prospective_context_v1", "off") or "off").strip().lower()


def is_prospective_question(text: str) -> bool:
    lowered = (text or "").lower().strip()
    return any(marker in lowered for marker in PROSPECTIVE_PATTERNS)


@dataclass
class ProspectiveItem:
    title: str
    truth_class: TruthClass
    reason: str
    source_refs: list[str] = field(default_factory=list)
    due_at: Any = None
    priority: str | None = None
    confidence: float = 0.8
    project_ref: str | None = None
    suggested_action: str | None = None
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "truth_class": self.truth_class.value,
            "reason": self.reason,
            "source_refs": self.source_refs[:6],
            "due_at": self.due_at.isoformat() if self.due_at is not None and hasattr(self.due_at, "isoformat") else None,
            "priority": self.priority,
            "confidence": self.confidence,
            "project_ref": self.project_ref,
            "suggested_action": self.suggested_action,
            "flags": self.flags,
        }


@dataclass
class ProspectiveContext:
    as_of: Any
    horizon_days: int
    required: list[ProspectiveItem] = field(default_factory=list)
    planned: list[ProspectiveItem] = field(default_factory=list)
    suggested: list[ProspectiveItem] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat() if hasattr(self.as_of, "isoformat") else None,
            "horizon_days": self.horizon_days,
            "required": [i.to_dict() for i in self.required],
            "planned": [i.to_dict() for i in self.planned],
            "suggested": [i.to_dict() for i in self.suggested],
            "conflicts": self.conflicts,
        }

    def render(self) -> str:
        """Labeled, turn-scoped block for the response layer (§31)."""

        parts = [
            "[EVIE_PROSPECTIVE_CONTEXT] read-only planning evidence for THIS "
            "turn only. Truth classes are explicit: REQUIRED = canonical "
            "obligation; PLANNED = owner explicitly scheduled; SUGGESTED = "
            "Evie inference. Suggestions are NOT commitments or schedule "
            "entries. not_owner_instruction=true; expires_after_this_turn=true."
        ]
        if self.required:
            parts.append("REQUIRED:")
            for item in self.required[:6]:
                due = f" (due {item.due_at:%Y-%m-%d %H:%M})" if item.due_at is not None and hasattr(item.due_at, "strftime") else ""
                parts.append(f"- {item.title}{due}")
        if self.planned:
            parts.append("PLANNED:")
            for item in self.planned[:5]:
                parts.append(f"- {item.title}")
        if self.suggested:
            parts.append("SUGGESTED (inferred, not scheduled):")
            for item in self.suggested[:5]:
                parts.append(f"- {item.title} — {item.reason}")
        if self.conflicts:
            parts.append("CONFLICTS:")
            for line in self.conflicts[:3]:
                parts.append(f"- {line}")
        if len(parts) == 1:
            return ""
        return "\n".join(parts)


def _aware(dt):
    """Coerce naive datetimes to UTC-aware (external integrations may be naive)."""

    if dt is not None and getattr(dt, "tzinfo", None) is None:

        return dt.replace(tzinfo=UTC)
    return dt


def _priority_rank(priority: str | None) -> int:
    return {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2, "LOW": 3}.get(str(priority or "").upper(), 2)


# ---------------------------------------------------------------------------
# Builders (all read-only)
# ---------------------------------------------------------------------------


async def _required_items(session: AsyncSession, now, horizon_end) -> list[ProspectiveItem]:
    """Canonical obligations: due commitments + calendar + alerts."""

    items: list[ProspectiveItem] = []
    from app.models import Commitment

    rows = (
        await session.execute(
            select(Commitment)
            .where(Commitment.status == "OPEN", Commitment.due_at.is_not(None))
            .order_by(Commitment.due_at.asc())
            .limit(12)
        )
    ).scalars().all()
    for row in rows:
        due = _aware(row.due_at)
        if due is None or due > horizon_end:
            continue
        flags = []
        if due < now:
            flags.append(ItemFlag.OVERDUE.value)
        items.append(
            ProspectiveItem(
                title=row.description[:160],
                truth_class=TruthClass.REQUIRED,
                reason=f"open commitment due {due:%Y-%m-%d}",
                source_refs=[f"commitment:{row.id}"],
                due_at=due,
                flags=flags,
            )
        )
    # Calendar: integration projection is the declared operational source
    # for this generation (§39) — reuse the existing derivation.
    try:
        from app.ev.calendar import calendar_signals

        signals = await calendar_signals(session, limit=120)
        for event in (signals.get("events") or [])[:12]:
            start = event.get("start")
            title = str(event.get("title") or "Calendar event")[:160]
            items.append(
                ProspectiveItem(
                    title=title,
                    truth_class=TruthClass.REQUIRED,
                    reason="calendar appointment",
                    source_refs=[f"calendar_event:{event.get('id') or title[:24]}"],
                    due_at=start,
                )
            )
    except Exception:  # noqa: BLE001 - calendar is best-effort, never fatal
        pass
    return items


async def _planned_items(session: AsyncSession, now, horizon_end) -> list[ProspectiveItem]:
    """Owner-explicit plans: routines scheduled + dated open goal steps.

    Casual intentions in history are NOT plans (§37): only canonical rows
    with explicit scheduling semantics qualify.
    """

    items: list[ProspectiveItem] = []
    from app.models import GoalStep

    steps = (
        await session.execute(
            select(GoalStep)
            .where(GoalStep.status == "PENDING", GoalStep.due_at.is_not(None))
            .order_by(GoalStep.due_at.asc())
            .limit(10)
        )
    ).scalars().all()
    for step in steps:
        step_due = _aware(step.due_at)
        if step_due is None or step_due > horizon_end:
            continue
        flags = [ItemFlag.UNFINISHED.value]
        if step_due < now:
            flags.append(ItemFlag.OVERDUE.value)
        items.append(
            ProspectiveItem(
                title=step.title[:160],
                truth_class=TruthClass.PLANNED,
                reason="owner-scheduled step (due date set)",
                source_refs=[f"goal_step:{step.id}"],
                due_at=step_due,
                flags=flags,
            )
        )
    try:
        from app.models import Routine
        from app.routines.schedule import next_run_after

        routines = (
            await session.execute(select(Routine).where(Routine.enabled.is_(True)).limit(20))
        ).scalars().all()
        for routine in routines:
            if not routine.schedule:
                continue
            try:
                nxt = _aware(next_run_after(routine.schedule, now, tz=routine.timezone))
            except Exception:  # noqa: BLE001 - bad cron must not break planning
                continue
            if nxt is None or nxt > horizon_end:
                continue
            items.append(
                ProspectiveItem(
                    title=str(routine.action_title or routine.name)[:160],
                    truth_class=TruthClass.PLANNED,
                    reason="enabled routine scheduled in horizon",
                    source_refs=[f"routine:{routine.id}"],
                    due_at=nxt,
                )
            )
    except Exception:  # noqa: BLE001
        pass
    return items


async def _suggested_items(session: AsyncSession, now, horizon_end) -> list[ProspectiveItem]:
    """Evie-inferred continuity: unfinished momentum on active projects (§35).

    Sources: active goals + their open steps + recent project-progress
    memories + recent decisions. Ranked by importance/momentum; capped.
    """

    items: list[ProspectiveItem] = []
    from app.models import Goal, GoalStep, Memory

    # Unfinished steps on active goals (the §35 continuity core).
    goals = (
        await session.execute(select(Goal).limit(40))
    ).scalars().all()
    goal_titles = {g.id: str(g.title or "")[:80] for g in goals}
    steps = (
        await session.execute(
            select(GoalStep).where(GoalStep.status == "PENDING").limit(60)
        )
    ).scalars().all()
    for step in steps:
        step_due = _aware(step.due_at)
        if step_due is not None and step_due <= horizon_end:
            continue  # already PLANNED
        title = f"Continue: {step.title[:120]}"
        goal_title = goal_titles.get(step.goal_id, "")
        reason = "unfinished step"
        if goal_title:
            title = f"{goal_title} — {step.title[:110]}"
            reason = f"unfinished step on active goal '{goal_title[:60]}'"
        items.append(
            ProspectiveItem(
                title=title,
                truth_class=TruthClass.SUGGESTED,
                reason=reason,
                source_refs=[f"goal_step:{step.id}"],
                project_ref=str(step.goal_id) if step.goal_id else None,
                suggested_action="resume the step when ready",
                flags=[ItemFlag.UNFINISHED.value],
                confidence=0.75,
            )
        )

    # Recent project momentum from memory (decisions/summories/progress).
    since = now - timedelta(days=7)
    memories = (
        await session.execute(
            select(Memory)
            .where(
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
                Memory.privacy_level.notin_(("never_send_to_model", "sensitive")),
                # F5 (§12): momentum draws from DURABLE classes only — the
                # noisy observation class was the old pipeline's permissive
                # catch-all; candidate scoring stops future noise at write.
                # Auto-generated episode summaries carry chatter; they must
                # clear a higher importance bar than owner-stated rows.
                or_(Memory.memory_type.in_(("decision", "goal")),
                    (Memory.memory_type == "summary") & (Memory.importance >= 0.7)),
                Memory.importance >= 0.55,
                Memory.event_time >= since.replace(tzinfo=None) if since.tzinfo is None else Memory.event_time >= since,
            )
            .order_by(Memory.event_time.desc())
            .limit(14)
        )
    ).scalars().all()
    from app.memory.candidates import Eligibility, legacy_eligibility

    for row in memories:
        if legacy_eligibility(row) not in {Eligibility.KEEP_HIGH_VALUE, Eligibility.KEEP_NORMAL}:
            continue
        text = (row.text or "")[:140]
        if not text:
            continue
        items.append(
            ProspectiveItem(
                title=f"Recent momentum: {text}",
                truth_class=TruthClass.SUGGESTED,
                reason="recent project memory (7-day momentum)",
                source_refs=[f"memory:{row.id}"],
                confidence=0.6,
                flags=[ItemFlag.RECENT_MOMENTUM.value],
            )
        )

    # DO NOT NAG (§33): rank and cap.
    def _rank(item: ProspectiveItem) -> float:
        momentum = 1.0 if ItemFlag.RECENT_MOMENTUM in [ItemFlag(f) for f in item.flags] else 0.0
        unfinished = 1.0 if ItemFlag.UNFINISHED in [ItemFlag(f) for f in item.flags] else 0.0
        return item.confidence + 0.3 * unfinished + 0.2 * momentum

    items.sort(key=_rank, reverse=True)
    return items[:5]


def _detect_conflicts(ctx: ProspectiveContext, now) -> list[str]:
    conflicts: list[str] = []
    required = list(ctx.required)
    for i, a in enumerate(required):
        for b in required[i + 1:]:
            if a.due_at is None or b.due_at is None:
                continue
            if abs((a.due_at - b.due_at).total_seconds()) < 30 * 60:
                conflicts.append(
                    f"Calendar collision: '{a.title[:60]}' and '{b.title[:60]}' within 30 minutes."
                )
    overdue = sum(1 for i in required if ItemFlag.OVERDUE.value in i.flags)
    if overdue:
        conflicts.append(f"{overdue} REQUIRED item(s) are overdue — reschedule explicitly.")
    return conflicts[:4]


async def build_prospective_context(
    session: AsyncSession,
    *,
    as_of=None,
    horizon_days: int = 1,
) -> ProspectiveContext:
    """Read-only multi-authority synthesis (§26). Never mutates anything."""

    started = time.perf_counter()
    now = as_of or utcnow()
    horizon_end = now + timedelta(days=max(1, horizon_days))
    ctx = ProspectiveContext(as_of=now, horizon_days=horizon_days)
    ctx.required = await _required_items(session, now, horizon_end)
    ctx.planned = await _planned_items(session, now, horizon_end)
    ctx.suggested = await _suggested_items(session, now, horizon_end)
    ctx.conflicts = _detect_conflicts(ctx, now)
    # PROVENANCE LAW: a suggestion without source refs cannot exist.
    ctx.suggested = [i for i in ctx.suggested if i.source_refs]
    from app.memory.os_health import note_prospective_query

    note_prospective_query(
        required=len(ctx.required),
        planned=len(ctx.planned),
        suggested=len(ctx.suggested),
        conflicts=len(ctx.conflicts),
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    return ctx
