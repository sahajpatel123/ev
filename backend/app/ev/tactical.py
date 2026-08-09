"""Tactical mode: E.V.-style pre-event briefings grounded in memory (ev.hud.briefing.v1)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings import get_embedder
from app.ev.decisions import find_decision_loops
from app.memory.retrieval import Retriever
from app.models import DecisionOutcome, Entity, MemoryEntity
from app.schemas import (
    ProvenanceItem as ProvenanceSchema,
)
from app.schemas import (
    TacticalBriefOut,
    TacticalBriefRequest,
    TacticalOption,
    TacticalRisk,
)
from app.utils.text import normalize_text, utcnow


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
