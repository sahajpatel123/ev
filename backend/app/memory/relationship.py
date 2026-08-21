"""Layer 31 + live/typed relationship context. Compact and evidence-backed."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.episodes import recent_episodes
from app.models import Memory
from app.utils.text import token_estimate

MEMORY_BEHAVIOR = (
    "You have one continuous relationship with this owner. Internal sessions "
    "and model providers are not new identities. Remember broadly, recall "
    "selectively, speak naturally. Use memory to resolve references and "
    "personalize silently. Do not recite that you remember, dump dates, or "
    "force old topics into a fresh question. Never say 'as you previously "
    "told me' unless they asked for history. Never mention files, databases, "
    "embeddings, or background curators. If they ask what you talked about, "
    "summarize a few meaningful topics, not transcripts. Call search_memory "
    "when history is required and the current context is not enough. "
    "search_memory returns an evidence pack: answer from that evidence. "
    "Prefer exact owner utterances over summaries. If a question asks for a "
    "name, title, or number and evidence has the wording, preserve it. If "
    "evidence is empty or grounding is no_reliable_record, say you cannot "
    "find a reliable record — never invent a memory. Distinguish current vs "
    "original when both exist. If they ask where you left off or what is still "
    "open, use current project state and unresolved loops. Do not mention those "
    "on a fresh unrelated question. Never mention loop IDs, cards, or curators. "
    "If retrieval is unavailable, say you cannot "
    "reliably pull older conversations. Do not close with automatic offers "
    "to elaborate."
)

MAX_CARD_TOKENS = 220


async def relationship_card(session: AsyncSession) -> str:
    """Compact owner context for prompts. Not a biography."""

    from app.ev.assistant import get_profile
    from app.ev.user_state import build_user_state

    profile = await get_profile(session)
    state = await build_user_state(session, access="model")
    prefs = await _current_texts(session, "preference", limit=4)
    goals = await _current_texts(session, "goal", limit=3)
    decisions = await _current_texts(session, "decision", limit=3)
    episodes = await recent_episodes(session, k=2)
    lines = [
        "RELATIONSHIP (Evie-owned persistent memory, not this model):",
        f"Owner name: {profile.owner_preferred_name or profile.nickname or 'owner'}.",
    ]
    if state.active_project:
        lines.append(f"Current project: {state.active_project}.")
    if state.current_task:
        lines.append(f"Current task: {state.current_task}.")
    if prefs:
        lines.append("Stable preferences: " + "; ".join(prefs) + ".")
    if goals:
        lines.append("Active goals: " + "; ".join(goals) + ".")
    if decisions:
        lines.append("Recent decisions: " + "; ".join(decisions) + ".")
    if episodes:
        lines.append("Recent episodes: " + " | ".join(row.text[:180] for row in episodes) + ".")
    card = " ".join(lines)
    if token_estimate(card) > MAX_CARD_TOKENS:
        card = card[: MAX_CARD_TOKENS * 4]
    return card


async def attach_relationship_memory(
    session: AsyncSession, manifest: dict | None
) -> dict:
    payload = dict(manifest or {})
    try:
        from app.memory.bootstrap import get_bootstrap

        pack = await get_bootstrap(session)
        payload["memory_bootstrap"] = {
            key: pack.get(key)
            for key in (
                "schema_version",
                "card_version",
                "updated_at",
                "through_event_id",
                "relationship",
                "active_project",
                "last_episode",
                "open_loops",
                "tokens",
                "memory_behavior",
            )
        }
        payload["relationship"] = pack.get("relationship") or await relationship_card(session)
        if pack.get("last_episode"):
            payload["recent_episodes"] = [str(pack["last_episode"])[:240]]
        else:
            episodes = await recent_episodes(session, k=3)
            payload["recent_episodes"] = [row.text[:240] for row in episodes]
        payload["memory_behavior"] = MEMORY_BEHAVIOR
    except Exception:  # noqa: BLE001 - live audio must still start
        payload.setdefault(
            "relationship",
            "RELATIONSHIP: persistent owner memory is temporarily unavailable.",
        )
        payload["memory_degraded"] = True
    return payload


def live_memory_instructions(manifest: dict | None) -> str:
    from app.memory.bootstrap import bootstrap_instructions

    raw = manifest if isinstance(manifest, dict) else {}
    pack = raw.get("memory_bootstrap")
    if isinstance(pack, dict) and (pack.get("relationship") or pack.get("memory_behavior")):
        text = bootstrap_instructions(pack)
        if raw.get("memory_degraded"):
            text += (
                "\nMemory retrieval is degraded. Do not claim to remember older conversations."
            )
        return text
    parts = [MEMORY_BEHAVIOR]
    card = str(raw.get("relationship") or "").strip()
    if card:
        parts.append(card)
    if raw.get("memory_degraded"):
        parts.append("Memory retrieval is degraded. Do not claim to remember older conversations.")
    return "\n".join(parts)


async def _current_texts(session: AsyncSession, memory_type: str, *, limit: int) -> list[str]:
    rows = (
        await session.execute(
            select(Memory)
            .where(
                Memory.memory_type == memory_type,
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
            .order_by(Memory.importance.desc(), Memory.event_time.desc())
            .limit(limit)
        )
    ).scalars().all()
    texts: list[str] = []
    for row in rows:
        text = (row.text or "").strip()
        if text:
            texts.append(text[:160])
    return texts
