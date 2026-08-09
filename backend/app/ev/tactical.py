"""Tactical mode: E.V.-style pre-event briefings grounded in memory (ev.hud.briefing.v1)."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings import get_embedder
from app.ev.decisions import find_decision_loops
from app.memory.retrieval import Retriever
from app.models import DecisionOutcome, Entity, MemoryEntity, TacticalCard
from app.schemas import (
    HudQuickCardOut,
    TacticalBriefOut,
    TacticalBriefRequest,
    TacticalOption,
    TacticalQuickRequest,
    TacticalRisk,
)
from app.schemas import (
    ProvenanceItem as ProvenanceSchema,
)
from app.utils.text import normalize_text, utcnow


def _aware(value):
    return value if value.tzinfo is not None else value.replace(tzinfo=utcnow().tzinfo)


async def build_briefing(session: AsyncSession, request: TacticalBriefRequest) -> TacticalBriefOut:
    retriever = Retriever(session, embeddings=get_embedder())
    memories = await retriever.search(request.topic, k=20, access="model")
    decisions = [m for m in memories if m.memory_type == "decision"]
    goals = [m for m in memories if m.memory_type == "goal"]
    patterns = [m for m in memories if m.memory_type == "pattern"]

    # People connected to the relevant memories.
    people: list[dict] = []
    if memories:
        memory_ids = [UUID(mem.memory_id) for mem in memories[:10]]
        links = (
            await session.execute(
                select(MemoryEntity, Entity)
                .join(Entity, Entity.id == MemoryEntity.entity_id)
                .where(MemoryEntity.memory_id.in_(memory_ids), Entity.entity_type == "person")
            )
        ).all()
        seen: set[str] = set()
        for link, entity in links:
            if entity.name.lower() in seen:
                continue
            seen.add(entity.name.lower())
            people.append(
                {
                    "name": entity.name,
                    "role": link.role,
                    "weight": link.weight,
                    "source": "memory-link",
                }
            )

    # Risks from reviewed decision outcomes.
    outcome_rows = (
        await session.execute(select(DecisionOutcome).where(DecisionOutcome.status == "reviewed"))
    ).scalars().all()
    risks: list[TacticalRisk] = []
    for outcome in outcome_rows:
        if not outcome.lesson:
            continue
        negative = any(
            token in normalize_text(outcome.lesson)
            for token in ("not preferred", "poor", "failed", "worse", "don't", "revisit")
        )
        if not negative:
            continue
        risks.append(
            TacticalRisk(
                description=outcome.lesson,
                likelihood=0.6,
                impact=0.7,
                mitigation="Re-check the prior outcome before committing to the same choice.",
            )
        )
    risk_count = len(risks)

    # Options from decision history.
    options: list[TacticalOption] = []
    if request.include_options:
        for i, decision in enumerate(decisions[:3]):
            label = f"Prior choice {i + 1}"
            options.append(
                TacticalOption(
                    label=label,
                    summary=decision.text,
                    pros=["Consistent with your stated reasoning and prior context."],
                    cons=["Repeating without new evidence if the prior outcome was not reviewed."],
                    evidence=decision.source_event_ids[:5],
                )
            )
        if not options:
            options.append(
                TacticalOption(
                    label="No prior decision found",
                    summary="EV found no recorded decision on this topic — treat this as a first evaluation.",
                    pros=["No sunk-cost bias."],
                    cons=["No personal evidence to lean on."],
                    evidence=[],
                )
            )

    # Recommendation: prior positive outcomes win; otherwise cautious if risks exist.
    recommendation = None
    if decisions:
        recommendation = (
            f"Proceed with your prior decision ({decisions[0].text}) unless new evidence changed the tradeoff."
        )
    if risk_count:
        recommendation = (
            (recommendation or "Evaluate the options")
            + " — but your outcome history flags a risk, so confirm the assumption that made it fail last time."
        )

    # Talking points.
    talking_points = []
    if goals:
        talking_points.append(f"Relevant active goal: {goals[0].text}")
    if patterns:
        talking_points.append(f"Pattern in play: {patterns[0].text}")
    if people:
        talking_points.append(f"People involved: {', '.join(p['name'] for p in people)}")
    talking_points.append("One decision at a time: state the choice, the reason, and when you'll review the outcome.")

    provenance = [
        ProvenanceSchema(
            memory_id=UUID(item.memory_id),
            text=item.text,
            memory_type=item.memory_type,
            score=item.score,
            components=item.components,
        )
        for item in memories[:8]
    ]
    loops = await find_decision_loops(session, min_count=2)
    decision_history = [
        {
            "topic": d["topic"],
            "count": d["count"],
            "confidence": d["confidence"],
            "memory_ids": d["memory_ids"],
            "latest_at": d["latest_at"].isoformat(),
        }
        for d in loops
        if normalize_text(request.topic) in normalize_text(d["topic"])
        or normalize_text(d["topic"]) in normalize_text(request.topic)
    ]

    return TacticalBriefOut(
        schema_version="ev.hud.briefing.v1",
        generated_at=utcnow(),
        objective=request.topic,
        context=request.context or (
            f"EV assembled this brief from {len(memories)} relevant memories "
            f"({len(decisions)} decisions, {len(goals)} goals, {len(patterns)} patterns)."
        ),
        people=people,
        risks=risks,
        options=options,
        recommendation=recommendation,
        decision_history=decision_history,
        talking_points=talking_points,
        provenance=provenance,
    )


def _quick_summary(brief: TacticalBriefOut) -> str:
    if brief.recommendation:
        return brief.recommendation
    if brief.risks:
        return f"Risks flagged: {brief.risks[0].description}"
    if brief.options:
        return brief.options[0].summary
    return "No prior evidence on this topic — treat as a first evaluation."


async def build_quick_card(
    session: AsyncSession,
    request: TacticalQuickRequest,
) -> HudQuickCardOut:
    """Assemble a compact quick card from a full briefing (used for caching)."""
    brief = await build_briefing(
        session,
        TacticalBriefRequest(
            topic=request.topic,
            stakes=request.stakes,
            context=request.context,
            include_options=True,
        ),
    )
    return HudQuickCardOut(
        schema_version="ev.hud.quickcard.v1",
        generated_at=utcnow(),
        objective=request.topic,
        summary=_quick_summary(brief),
        next_action=brief.recommendation,
        top_risk=brief.risks[0].description if brief.risks else None,
        people_count=len(brief.people),
        options_count=len(brief.options),
        decision_history_count=len(brief.decision_history),
        meta={
            "stakes": request.stakes,
            "provenance_count": len(brief.provenance),
        },
    )


async def get_quick_card(
    session: AsyncSession,
    request: TacticalQuickRequest,
    *,
    force: bool = False,
) -> tuple[HudQuickCardOut, str]:
    """Return a fresh cached quick card or build and cache one.

    Returns `(card, source)` where source is ``"cache"`` or ``"fresh"``.
    """
    now = utcnow()
    key = normalize_text(request.topic)
    row = (
        await session.execute(
            select(TacticalCard)
            .where(TacticalCard.topic == key)
            .order_by(TacticalCard.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if (
        row is not None
        and not force
        and request.ttl_seconds > 0
        and row.expires_at is not None
        and _aware(row.expires_at) > now
    ):
        row.hit_count = (row.hit_count or 0) + 1
        row.last_hit_at = now
        await session.flush()
        card = HudQuickCardOut.model_validate(row.payload)
        card.meta = {**card.meta, "source": "cache", "hit_count": row.hit_count}
        return card, "cache"

    card = await build_quick_card(session, request)
    if row is None:
        row = TacticalCard(topic=key, schema_version="ev.hud.quickcard.v1")
        session.add(row)
    row.payload = card.model_dump(mode="json")
    row.expires_at = now + timedelta(seconds=max(1, request.ttl_seconds))
    row.hit_count = (row.hit_count or 0) + 1
    row.last_hit_at = now
    await session.flush()
    card.meta = {**card.meta, "source": "fresh", "hit_count": row.hit_count}
    return card, "fresh"


async def prepare_quick_card(
    session: AsyncSession,
    request: TacticalQuickRequest,
) -> HudQuickCardOut:
    """Precompute and cache a quick card (background pre-event path)."""
    card, _ = await get_quick_card(session, request, force=True)
    return card
