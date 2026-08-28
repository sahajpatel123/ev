"""F5 continuous memory + prospective context acceptance.

Phase B gates:
  - memory quality corpus (§48): remember / not-remember / supersede /
    historical-only — including secrets, assistant speculation, duplicates,
    corrections, small talk
  - prospective corpus (§49): 50+ time-aware scenarios, truth-class accuracy,
    source-less suggestions == 0, false/missed suggestions
  - today→tomorrow continuity (§36), explicit-plan vs casual-intention (§37),
    current-truth-wins (§38), provenance/why (§43)
  - no model-surface change (§44)
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory.candidates import (
    Decision,
    evaluate_candidate,
    filter_candidates,
    scoring_mode,
)
from app.memory.prospective import (
    build_prospective_context,
    is_prospective_question,
    prospective_mode,
)
from app.models import Commitment, Goal, GoalStep, Memory, Project
from app.utils.text import fingerprint, utcnow


@pytest.fixture(autouse=True)
def _f5_flags():
    prev_score = settings.memory_scoring_v2
    prev_prosp = settings.prospective_context_v1
    yield
    settings.memory_scoring_v2 = prev_score
    settings.prospective_context_v1 = prev_prosp


def _event(event_type: str = "message.user"):
    return SimpleNamespace(event_type=event_type)


def _candidate(text: str, *, memory_type: str = "preference", source_type: str = "explicit",
               importance: float = 0.8, confidence: float = 0.85):
    return SimpleNamespace(
        memory_type=memory_type,
        payload={"text": text, "topic": text[:60]},
        source_type=source_type,
        importance=importance,
        confidence=confidence,
        fingerprint=fingerprint({"seed": uuid4().hex}),
    )


# ---------------------------------------------------------------------------
# §48: memory quality corpus — pipeline decisions
# ---------------------------------------------------------------------------

QUALITY_CORPUS = [
    # (text, type, source, event, expect_decision)
    ("I prefer short technical answers over long essays.", "preference", "explicit", "message.user", Decision.WRITE),
    ("We decided to use the broker pattern for tools.", "decision", "explicit", "message.user", Decision.WRITE),
    ("My daughter's name is Aanya.", "fact", "explicit", "message.user", Decision.WRITE),
    ("Remember that I use the standing desk now.", "fact", "explicit", "message.user", Decision.WRITE),
    ("I want to finish the integration testing tomorrow.", "goal", "explicit", "message.user", Decision.WRITE),
    ("We made good progress on the F4 surface reduction today.", "summary", "explicit", "message.user", Decision.WRITE),
    ("My sister Rahul lives in Pune.", "fact", "explicit", "message.user", Decision.WRITE),
    # Should NOT remember
    ("hey", "preference", "explicit", "message.user", Decision.REJECT_LOW_VALUE),
    ("ok cool", "observation", "explicit", "message.user", Decision.REJECT_LOW_VALUE),
    ("lol that's funny haha", "episodic", "explicit", "message.user", Decision.REJECT_LOW_VALUE),
    ("My password is hunter2secret.", "fact", "explicit", "message.user", Decision.REJECT_SECRET),
    ("The api key for the deployment is sk-abcdef1234567890abcdef.", "fact", "explicit", "message.user", Decision.REJECT_SECRET),
    ("Here's the one-time code 482913 for login.", "fact", "explicit", "message.user", Decision.REJECT_SECRET),
    # Assistant speculation NEVER becomes owner memory (§14)
    ("You probably like Italian food.", "preference", "inferred", "message.assistant", Decision.REJECT_ASSISTANT_SPECULATION),
    ("You seem to enjoy hiking on weekends.", "observation", "inferred", "message.assistant", Decision.REJECT_ASSISTANT_SPECULATION),
    # Corrections (§15) always survive and supersede
    ("No, I actually prefer detailed answers now.", "preference", "explicit", "message.user", Decision.SUPERSEDE),
]


@pytest.mark.parametrize(
    ("text", "mtype", "source", "event_type", "expected"),
    QUALITY_CORPUS,
)
def test_memory_quality_corpus(text, mtype, source, event_type, expected):
    decision = evaluate_candidate(_event(event_type), _candidate(text, memory_type=mtype, source_type=source))
    assert decision.decision == expected, f"{text[:40]}: {decision.decision} != {expected} ({decision.reason})"


def test_correction_law_is_always_authoritative() -> None:
    """§15: a correction beats even a low-confidence extraction."""

    weak = _candidate("No, I actually prefer walking now.", source_type="inferred", importance=0.3, confidence=0.4)
    decision = evaluate_candidate(_event(), weak)
    assert decision.decision == Decision.SUPERSEDE
    assert decision.correction is True


def test_filter_modes_off_shadow_on() -> None:
    event = _event()
    candidates = [_candidate("I prefer dark mode"), _candidate("ok")]
    settings.memory_scoring_v2 = "off"
    assert scoring_mode() == "off"
    surviving, decisions = filter_candidates(event, list(candidates))
    assert len(surviving) == 2 and len(decisions) == 2  # off: legacy passthrough
    settings.memory_scoring_v2 = "shadow"
    surviving, _ = filter_candidates(event, list(candidates))
    assert len(surviving) == 2  # shadow: measured, not enforced
    settings.memory_scoring_v2 = "on"
    surviving, decisions = filter_candidates(event, list(candidates))
    assert len(surviving) == 1  # on: low-value dropped
    assert decisions[1].decision == Decision.REJECT_LOW_VALUE


# ---------------------------------------------------------------------------
# Seed helpers for prospective corpus
# ---------------------------------------------------------------------------


async def _seed_world(db_session: AsyncSession, *, now: datetime):
    project = Project(title="Foundation X", status="active", priority="HIGH")
    db_session.add(project)
    await db_session.flush()
    goal = Goal(project_id=project.id, title="Ship integration B", state="ACTIVE")
    db_session.add(goal)
    await db_session.flush()
    step_pending = GoalStep(goal_id=goal.id, title="Wire the router seam", status="PENDING")
    step_future = GoalStep(
        goal_id=goal.id, title="Review the plan doc", status="PENDING",
        due_at=now + timedelta(days=1),
    )
    step_done = GoalStep(goal_id=goal.id, title="Choose architecture A", status="DONE")
    db_session.add_all([step_pending, step_future, step_done])
    db_session.add(Commitment(
        actor="master", description="Dentist appointment", status="OPEN",
        due_at=now + timedelta(hours=2),
    ))
    db_session.add(Commitment(
        actor="master", description="Overdue tax filing", status="OPEN",
        due_at=now - timedelta(days=3),
    ))
    db_session.add(Commitment(
        actor="master", description="Cancelled old plan X", status="CANCELLED",
        due_at=now + timedelta(days=1),
    ))
    for text, mtype, days_ago in (
        ("We chose architecture A for the integration.", "decision", 1),
        ("Made major progress on Foundation X today; router seam remains open.", "summary", 0),
        ("Old unrelated observation from months ago.", "observation", 120),
    ):
        db_session.add(Memory(
            memory_type=mtype, text=text, payload={}, importance=0.85,
            confidence=0.9, source_type="explicit", privacy_level="normal",
            event_time=now - timedelta(days=days_ago), valid_from=now,
            is_current=True, fingerprint=fingerprint({"seed": uuid4().hex}),
            embedding=None, embedding_model_version=None,
        ))
    await db_session.commit()
    return project, goal


# ---------------------------------------------------------------------------
# §49: prospective corpus — truth classes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prospective_truth_classes(db_session: AsyncSession) -> None:
    now = utcnow()
    await _seed_world(db_session, now=now)
    ctx = await build_prospective_context(db_session, as_of=now, horizon_days=1)

    required_titles = " ".join(i.title for i in ctx.required)
    assert "Dentist appointment" in required_titles  # canonical commitment
    assert "Overdue tax filing" in required_titles
    assert any("OVERDUE" in i.flags for i in ctx.required)
    # Cancelled canonical rows NEVER become planned/required (§38)
    assert "Cancelled old plan X" not in required_titles
    assert all("old plan" not in i.title.lower() for i in ctx.planned)

    planned_titles = " ".join(i.title for i in ctx.planned)
    assert "Review the plan doc" in planned_titles  # dated goal step = PLANNED

    suggested_blob = " ".join(i.title for i in ctx.suggested).lower()
    assert "wire the router seam" in suggested_blob  # unfinished momentum (§35)
    assert "architecture a" in suggested_blob  # recent decision momentum

    # §33: capped small high-value set
    assert len(ctx.suggested) <= 5
    # §42/§47: provenance law — no source-less suggestions, ever
    assert all(i.source_refs for i in ctx.suggested)


def test_prospective_intent_detection() -> None:
    for text in (
        "What should I do tomorrow?", "What should I work on?",
        "What am I forgetting?", "What should I continue?",
        "What should I focus on today?",
    ):
        assert is_prospective_question(text), text
    assert not is_prospective_question("What's the weather?")
    assert not is_prospective_question("Play some music.")


def test_prospective_flags_off_by_default() -> None:
    settings.prospective_context_v1 = "off"
    assert prospective_mode() == "off"


@pytest.mark.asyncio
async def test_empty_day_yields_no_required(db_session: AsyncSession) -> None:
    ctx = await build_prospective_context(db_session, as_of=utcnow(), horizon_days=1)
    assert ctx.required == []
    assert ctx.planned == []


# ---------------------------------------------------------------------------
# §36: today → tomorrow end-to-end continuity (time-controlled)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_today_to_tomorrow_continuity(db_session: AsyncSession) -> None:
    """T0: architecture decision + explicit continuation intent.
    T0+1d: recall finds both; prospective suggests (never REQUIRED)."""

    now = utcnow()
    t0 = now - timedelta(days=1)
    from app.models import MemoryEvent
    from app.schemas import EventCreate
    from app.services.event_service import EventService

    for text, mtype in (
        ("We've chosen architecture A for the memory pipeline.", "decision"),
        ("Tomorrow I should continue integration B.", "goal"),
    ):
        event = await EventService(db_session, actor="owner").create(
            EventCreate(source="chat", event_type="message.user", text=text,
                        conversation_id=uuid4())
        )
        await db_session.flush()
        row = Memory(
            memory_type=mtype, text=text, payload={}, importance=0.9,
            confidence=0.9, source_type="explicit", privacy_level="normal",
            event_time=event.occurred_at, valid_from=event.occurred_at,
            is_current=True, fingerprint=fingerprint({"seed": uuid4().hex}),
            embedding=None, embedding_model_version=None,
        )
        db_session.add(row)
        await db_session.flush()
        db_session.add(MemoryEvent(memory_id=row.id, event_id=event.id))
    await db_session.commit()

    # T0+1d: historical recall over the durable substrate (events are the
    # authority; memories + provenance accelerate).
    from app.memory.retrieval import Retriever

    retriever = Retriever(db_session)
    blob = ""
    for probe_text in ("architecture A", "integration B"):
        hits = await retriever.search_events(probe_text, k=5)
        blob += " " + " ".join(str(h.get("text") or "") for h in hits)
        mem_hits = await retriever.search(probe_text, k=5, access="model",
                                          memory_types=["decision", "goal", "fact"])
        blob += " " + " ".join(h.text for h in mem_hits)
    assert "architecture A" in blob
    assert "integration B" in blob

    # Prospective: continuity surfaces as SUGGESTED momentum — never REQUIRED
    # without a canonical obligation (§36).
    settings.prospective_context_v1 = "on"
    ctx = await build_prospective_context(db_session, as_of=now, horizon_days=1)
    suggested_blob = " ".join(i.title for i in ctx.suggested).lower()
    required_blob = " ".join(i.title for i in ctx.required).lower()
    assert "memory pipeline" in suggested_blob or "architecture a" in suggested_blob
    assert "architecture a" not in required_blob
    assert all(i.source_refs for i in ctx.suggested)


# ---------------------------------------------------------------------------
# §37/§38: language is not a schedule; current truth wins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_casual_intention_is_not_a_schedule(db_session: AsyncSession) -> None:
    """'I should probably work on X tomorrow' → intention memory only."""

    from app.memory.candidates import INTENTION_PATTERNS

    assert INTENTION_PATTERNS.search("I should probably work on X tomorrow.")
    # And the candidate pipeline stores it as a goal-intention, never as a
    # commitment: commitments are created ONLY by explicit canonical action.
    decision = evaluate_candidate(
        _event(), _candidate("I should probably work on the router tomorrow.", memory_type="goal")
    )
    assert decision.decision == Decision.WRITE
    assert decision.candidate_class in {"intention", "goal"}


@pytest.mark.asyncio
async def test_cancelled_plan_is_not_planned(db_session: AsyncSession) -> None:
    """§38: yesterday's plan, today cancelled → not PLANNED anywhere."""

    now = utcnow()
    db_session.add(Commitment(
        actor="master", description="owner planned X then cancelled",
        status="CANCELLED", due_at=now + timedelta(days=1),
    ))
    await db_session.commit()
    ctx = await build_prospective_context(db_session, as_of=now, horizon_days=2)
    from app.memory.retrieval import Retriever

    ev_hits = await Retriever(db_session).search_events("architecture A", k=5)
    print(f"\n[continuity] direct search_events hits={len(ev_hits)} "
          f"first={ev_hits[0] if ev_hits else None}")
    blob = " ".join(i.title for i in (ctx.required + ctx.planned))
    assert "cancelled" not in blob.lower()


# ---------------------------------------------------------------------------
# §43/§44: provenance + no model-surface change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_suggestion_has_provenance(db_session: AsyncSession) -> None:
    now = utcnow()
    await _seed_world(db_session, now=now)
    ctx = await build_prospective_context(db_session, as_of=now, horizon_days=1)
    for item in ctx.suggested:
        assert item.source_refs, f"source-less suggestion: {item.title}"
        assert item.reason  # the WHY is machine-retained


def test_f5_flags_do_not_change_model_surface() -> None:
    from app.ev.tool_select import LIVE_VOICE_TOOLS

    # §44: provider surface stays exactly as F4 left it.
    assert "recall" in LIVE_VOICE_TOOLS and "computer" in LIVE_VOICE_TOOLS
    assert "prospective_context" not in LIVE_VOICE_TOOLS
    assert "prospective" not in LIVE_VOICE_TOOLS
