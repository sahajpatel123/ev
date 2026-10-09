"""Owner relevance for retrieval + prompt context (memory+fetching plan Phase 3).

Two seams, both consent-gated on the ``life_data_personalization`` track and
dead ({} / None) unless the owner model is enabled:

- ``owner_boosts_for``: per-memory capped multipliers from token overlap with
  current owner rows. Consumed ONLY by the Retriever importance signal; the
  locked scoring formula is untouched.
- ``owner_context_for_prompt``: privacy-filtered owner snapshot for the
  context compiler. Rows marked sensitive/never_send_to_model are excluded.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.contracts import OwnerModelContext
from app.training.consent import active_consent
from app.training.personalization import MAX_MULTIPLIER, TRACK
from app.utils.text import simple_tokens

# Privacy tiers that never influence retrieval ranking or prompt context.
EXCLUDED_PRIVACY = frozenset({"never_send_to_model", "sensitive"})


def owner_boost_cap() -> float:
    """Effective boost ceiling: setting clamped into [1.0, MAX_MULTIPLIER]."""
    try:
        configured = float(getattr(settings, "owner_retrieval_boost_max", 1.2))
    except (TypeError, ValueError):
        return 1.0
    return min(MAX_MULTIPLIER, max(1.0, configured))


async def _gated_owner_rows(session: AsyncSession) -> list:
    """Current owner rows eligible to influence retrieval/context, or []."""
    if not settings.owner_model_enabled:
        return []
    if await active_consent(session, TRACK) is None:
        return []
    from app.ev.owner_model import current_owner_rows

    rows: list = []
    for kind in ("trait", "value", "thinking_style"):
        rows.extend(await current_owner_rows(session, kind))  # type: ignore[arg-type]
    return [row for row in rows if row.privacy_level not in EXCLUDED_PRIVACY]


async def owner_boosts_for(session: AsyncSession, memories: list) -> dict[UUID, float]:
    """Capped per-memory owner-relevance multipliers. {} when gated off."""
    cap = owner_boost_cap()
    if cap <= 1.0 or not memories:
        return {}
    rows = await _gated_owner_rows(session)
    if not rows:
        return {}
    owner_token_sets = [simple_tokens(row.text or "") for row in rows]
    boosts: dict[UUID, float] = {}
    for memory in memories:
        mem_tokens = simple_tokens(memory.text or "")
        if not mem_tokens:
            continue
        best = 0.0
        for owner_tokens in owner_token_sets:
            union = mem_tokens | owner_tokens
            if not union:
                continue
            overlap = len(mem_tokens & owner_tokens) / len(union)
            if overlap > best:
                best = overlap
        if best > 0:
            boosts[memory.id] = round(1.0 + (cap - 1.0) * best, 4)
    return boosts


async def owner_grounding_materials(session: AsyncSession) -> list:
    """Owner rows as grounding material for the output filter's audit.

    Gated exactly like the owner context block (enabled + consent +
    privacy-filtered). ``memory_id`` carries ``owner:{kind}:{row_id}`` so the
    chip stage can cite the owner row and its source events.
    """
    from app.filter.envelope import GroundingMaterial

    rows = await _gated_owner_rows(session)
    if not rows:
        return []
    from app.ev.owner_model import row_source_events

    materials = []
    for row in rows:
        text = (row.text or "").strip()
        if not text:
            continue
        table = row.__tablename__
        kind = {
            "owner_traits": "trait",
            "owner_values": "value",
            "owner_thinking_style": "thinking_style",
        }.get(table)
        if kind is None:
            continue
        event_ids = await row_source_events(session, kind, row.id)  # type: ignore[arg-type]
        materials.append(
            GroundingMaterial(
                text=text,
                memory_id=f"owner:{kind}:{row.id}",
                memory_type=f"owner_{kind}",
                source_event_ids=[str(eid) for eid in event_ids],
                confidence=row.confidence,
                event_time=row.event_time,
                privacy_level=row.privacy_level,
            )
        )
    return materials


async def owner_context_for_prompt(session: AsyncSession) -> OwnerModelContext | None:
    """Privacy-filtered owner snapshot for the context compiler, or None."""
    rows = await _gated_owner_rows(session)
    if not rows:
        return None
    from app.ev.owner_model import current_owner_state

    context = OwnerModelContext()
    for row in rows:
        text = (row.text or "").strip()
        if not text:
            continue
        table = row.__tablename__
        if table == "owner_traits":
            context.traits.append(text)
        elif table == "owner_values":
            context.values.append(text)
        elif table == "owner_thinking_style":
            context.thinking_style.append(text)
    snapshot = await current_owner_state(session)
    if snapshot is not None and snapshot.privacy_level not in EXCLUDED_PRIVACY:
        context.state_label = f"{snapshot.state_kind}: {snapshot.label}"
    if not context.traits and not context.values and not context.thinking_style:
        return None
    return context
