"""Rolling conversation summary for the single lifelong thread.

The rollup is compact derived state: it is rebuilt from immutable events (or
merged incrementally after each turn), token-bounded, and never a substitute
for the raw history. It exists so EV can resume mid-thought after hundreds of
turns without loading the entire lifetime into every prompt.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import ConversationRollup, Event
from app.utils.text import token_estimate, utcnow

DECISION_PATTERN = re.compile(
    r"\b(decided|decision|decide on|chose|choose|prefer|going with|settle on)\b",
    re.IGNORECASE,
)
RESUME_PATTERN = re.compile(
    r"\b(continue|resume|pick up|where were we|what was i doing|what were we doing|"
    r"remind me what|start where|carry on)\b",
    re.IGNORECASE,
)

MAX_TOPICS = 10
MAX_QUESTIONS = 5
MAX_DECISIONS = 5
MAX_ARC_TURNS = 12
MAX_ARC_CHARS = 160
MAX_EVENTS_PER_REBUILD = 5000

MODEL_EXCLUDED_PRIVACY = ("never_send_to_model", "sensitive")


def wants_deep_context(message: str) -> bool:
    """Resume/continuity phrasings automatically request progressive depth."""
    return bool(RESUME_PATTERN.search(message))


def _event_text(event: Event) -> str:
    return str((event.content or {}).get("text") or "")[:2000]


def _question_text(text: str) -> str | None:
    stripped = text.strip()
    if not stripped.endswith("?"):
        return None
    return stripped[:240]


def _is_decision(text: str) -> bool:
    return bool(DECISION_PATTERN.search(text))


def _topics_from(text: str) -> Counter[str]:
    words = re.findall(r"[a-z0-9_\-']{4,}", text)
    lowered = [w.lower() for w in words]
    stopwords = {
        "the", "and", "that", "this", "with", "from", "have", "been", "was",
        "were", "will", "would", "should", "could", "can", "has", "had",
        "for", "but", "not", "are", "you", "your", "about", "into", "than",
        "then", "there", "they", "them", "what", "why", "how", "when",
    }
    display: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for word, low in zip(words, lowered):
        if low in stopwords:
            continue
        display.setdefault(low, word)
        counts[low] += 1
    return Counter({display[key]: count for key, count in counts.items()})


async def _load_events(
    session: AsyncSession,
    thread_id: UUID,
    *,
    after_event_id: UUID | None = None,
    access: str = "user",
) -> list[Event]:
    stmt = (
        select(Event)
        .where(
            Event.conversation_id == thread_id,
            Event.tombstoned_at.is_(None),
            Event.event_type.in_(["message.user", "message.assistant"]),
            # The rollup is assembled into model context; content the user
            # marked never_send_to_model (or sensitive without opt-in) must not
            # be folded into derived summaries.
            Event.privacy_level.notin_(("never_send_to_model", "sensitive")),
        )
        .order_by(Event.occurred_at.asc(), Event.id.asc())
        .limit(MAX_EVENTS_PER_REBUILD)
    )
    if after_event_id is not None:
        after = await session.get(Event, after_event_id)
        if after is not None:
            stmt = stmt.where(
                (Event.occurred_at > after.occurred_at)
                | (
                    (Event.occurred_at == after.occurred_at)
                    & (Event.id > after.id)
                )
            )
    if access == "model":
        stmt = stmt.where(Event.privacy_level.notin_(MODEL_EXCLUDED_PRIVACY))
    rows = await session.execute(stmt)
    return list(rows.scalars().all())


def _turn_number(rollup: ConversationRollup) -> int:
    """Next user-turn number, counting existing covered turns."""
    return (rollup.covered_turn_count or 0) + 1


def _merge_events(
    rollup: ConversationRollup,
    events: list[Event],
) -> None:
    """Fold new message events into the rollup's topics/questions/decisions/arc."""
    existing_topics: dict[str, tuple[str, int]] = {}
    for entry in rollup.topics or []:
        existing_topics[entry["topic"].lower()] = (entry["topic"], entry["count"])
    questions = deque((rollup.open_questions or [])[:MAX_QUESTIONS])
    decisions = deque((rollup.decisions or [])[:MAX_DECISIONS])
    arc = deque((rollup.arc or [])[-MAX_ARC_TURNS:])

    current_user: str | None = None
    current_turn_no: int | None = None
    for event in events:
        text = _event_text(event)
        if not text:
            continue
        if event.event_type == "message.user":
            current_user = text
            current_turn_no = _turn_number(rollup)
            rollup.covered_turn_count = (rollup.covered_turn_count or 0) + 1
            for topic, count in _topics_from(text).items():
                key = topic.lower()
                if key in existing_topics:
                    display, old_count = existing_topics[key]
                    existing_topics[key] = (display, old_count + count)
                else:
                    existing_topics[key] = (topic, count)
            if _is_decision(text):
                decision = text[:240]
                if decision not in decisions:
                    decisions.append(decision)
            question = _question_text(text)
            if question is not None and question not in questions:
                questions.append(question)
        elif event.event_type == "message.assistant" and current_user is not None:
            turn_line = (
                f"#{current_turn_no} U: {current_user[:MAX_ARC_CHARS]} "
                f"| EV: {text[:MAX_ARC_CHARS]}"
            )
            if not arc or arc[-1] != turn_line:
                arc.append(turn_line)
            current_user = None
            current_turn_no = None

    rollup.topics = [
        {"topic": topic, "count": count}
        for topic, count in sorted(
            existing_topics.values(), key=lambda item: item[1], reverse=True
        )[:MAX_TOPICS]
    ]
    rollup.open_questions = list(questions)[-MAX_QUESTIONS:]
    rollup.decisions = list(decisions)[-MAX_DECISIONS:]
    rollup.arc = list(arc)[-MAX_ARC_TURNS:]
    rollup.last_event_id = events[-1].id if events else rollup.last_event_id
    rollup.updated_at = utcnow()


def _format_summary(rollup: ConversationRollup, *, budget_tokens: int | None = None) -> str:
    budget = budget_tokens or settings.rollup_budget_tokens
    lines = [
        f"ROLLING CONVERSATION SUMMARY ({rollup.covered_turn_count} user turns so far):"
    ]
    used = sum(token_estimate(line) for line in lines)

    topics = [entry["topic"] for entry in (rollup.topics or [])]
    if topics:
        line = f"Topics: {', '.join(topics[:8])}."
        lines.append(line)
        used += token_estimate(line)

    decisions = rollup.decisions or []
    if decisions:
        lines.append("Decisions/choices mentioned:")
        used += token_estimate(lines[-1])
        for decision in decisions[-3:]:
            line = f"- {decision}"
            if used + token_estimate(line) > budget:
                break
            lines.append(line)
            used += token_estimate(line)

    questions = rollup.open_questions or []
    if questions:
        lines.append("Open questions still on the table:")
        used += token_estimate(lines[-1])
        for question in questions[-3:]:
            line = f"- {question}"
            if used + token_estimate(line) > budget:
                break
            lines.append(line)
            used += token_estimate(line)

    arc = rollup.arc or []
    if arc:
        lines.append(f"Recent arc (last {len(arc)} turns):")
        used += token_estimate(lines[-1])
        for turn in arc:
            line = f"- {turn}"
            if used + token_estimate(line) > budget:
                break
            lines.append(line)
            used += token_estimate(line)

    rollup.token_count = used
    return "\n".join(lines)


async def build_rollup(
    session: AsyncSession,
    thread_id: UUID,
    *,
    force: bool = False,
) -> ConversationRollup:
    """Get or create the thread's rolling summary, merging new events."""
    result = await session.execute(
        select(ConversationRollup).where(ConversationRollup.thread_id == thread_id)
    )
    rollup = result.scalars().first()
    if rollup is None:
        rollup = ConversationRollup(thread_id=thread_id)
        session.add(rollup)
        await session.flush()
        force = True

    events = await _load_events(
        session,
        thread_id,
        after_event_id=None if force else rollup.last_event_id,
    )
    if events:
        _merge_events(rollup, events)
    rollup.summary = _format_summary(rollup)
    rollup.token_count = token_estimate(rollup.summary)
    rollup.updated_at = utcnow()
    await session.flush()
    return rollup


async def rebuild_rollup(
    session: AsyncSession,
    thread_id: UUID,
) -> ConversationRollup:
    """Regenerate the rollup from all thread events (e.g. after tombstone)."""
    result = await session.execute(
        select(ConversationRollup).where(ConversationRollup.thread_id == thread_id)
    )
    rollup = result.scalars().first()
    if rollup is None:
        rollup = ConversationRollup(thread_id=thread_id)
        session.add(rollup)
        await session.flush()
    rollup.topics = []
    rollup.open_questions = []
    rollup.decisions = []
    rollup.arc = []
    rollup.last_event_id = None
    rollup.covered_turn_count = 0
    events = await _load_events(session, thread_id)
    if events:
        _merge_events(rollup, events)
    rollup.summary = _format_summary(rollup)
    rollup.token_count = token_estimate(rollup.summary)
    rollup.updated_at = utcnow()
    await session.flush()
    return rollup


class ModelSafeRollup:
    """Model-facing view of the rolling summary.

    Computed on demand from events the model is allowed to see, so
    ``never_send_to_model`` (and ``sensitive`` without opt-in) content is
    physically excluded from the prompt boundary.
    """

    def __init__(self, summary: str, open_questions: list[str], covered_turn_count: int) -> None:
        self.summary = summary
        self.open_questions = open_questions
        self.covered_turn_count = covered_turn_count


async def model_safe_rollup(
    session: AsyncSession,
    thread_id: UUID,
    *,
    budget_tokens: int | None = None,
) -> ModelSafeRollup:
    """Build a prompt-safe rolling summary without touching the persisted one."""
    events = await _load_events(session, thread_id, access="model")
    scratch = ConversationRollup(thread_id=thread_id)
    _merge_events(scratch, events)
    scratch.summary = _format_summary(scratch, budget_tokens=budget_tokens)
    return ModelSafeRollup(
        summary=scratch.summary,
        open_questions=list(scratch.open_questions or [])[:MAX_QUESTIONS],
        covered_turn_count=scratch.covered_turn_count,
    )
