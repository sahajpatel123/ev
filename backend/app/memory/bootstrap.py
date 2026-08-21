"""Precomputed MemoryBootstrap. Read on session start; never call DeepSeek here."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory.episodes import recent_episodes
from app.memory.os_health import note_bootstrap
from app.memory.paths import atomic_write_json, ensure_tree, memory_root, read_json
from app.models import Event, Memory
from app.utils.text import token_estimate, utcnow

SCHEMA_VERSION = 1
_RAM: dict[str, Any] | None = None


def _budget() -> int:
    return max(400, int(settings.memory_bootstrap_max_tokens))


async def _latest_event_id(session: AsyncSession) -> str | None:
    row = (
        await session.execute(
            select(Event.id)
            .where(
                Event.tombstoned_at.is_(None),
                Event.event_type.in_(("message.user", "message.assistant")),
            )
            .order_by(Event.occurred_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return str(row) if row else None


async def _texts(session: AsyncSession, memory_type: str, *, limit: int) -> list[str]:
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
    return [row.text.strip()[:160] for row in rows if (row.text or "").strip()]


async def build_bootstrap(session: AsyncSession) -> dict[str, Any]:
    """Compact pack for a brand-new Realtime session. Postgres is authority."""

    from app.ev.assistant import get_profile
    from app.ev.user_state import build_user_state
    from app.memory.relationship import MEMORY_BEHAVIOR

    profile = await get_profile(session)
    state = await build_user_state(session, access="model")
    prefs = await _texts(session, "preference", limit=4)
    goals = await _texts(session, "goal", limit=3)
    decisions = await _texts(session, "decision", limit=3)
    facts = await _texts(session, "fact", limit=4)
    from app.memory.loops import list_loops, rank_open_loops

    opens = await list_loops(session, k=12)
    ranked_loops = rank_open_loops(opens, active_project=state.active_project, k=3)
    loop_titles = [
        str((row.payload or {}).get("title") or row.text)[:80] for row in ranked_loops
    ]
    episodes = await recent_episodes(session, k=2)
    through = await _latest_event_id(session)
    owner = profile.owner_preferred_name or profile.nickname or "owner"
    lines = [
        f"Owner: {owner}.",
        "Provider sessions are disposable; this relationship is not.",
    ]
    if state.active_project:
        lines.append(f"Active project: {state.active_project}.")
    if state.current_task:
        lines.append(f"Current task: {state.current_task}.")
    if prefs:
        lines.append("Preferences: " + "; ".join(prefs) + ".")
    if goals:
        lines.append("Goals: " + "; ".join(goals) + ".")
    if decisions:
        lines.append("Decisions: " + "; ".join(decisions) + ".")
    if facts:
        lines.append("Stable facts: " + "; ".join(facts) + ".")
    if loop_titles:
        lines.append("Active open loops: " + "; ".join(loop_titles) + ".")
    episode_text = ""
    if episodes:
        episode_text = " | ".join(row.text[:180] for row in episodes)
        lines.append("Last episode: " + episode_text + ".")
    prose = " ".join(lines)
    budget = _budget()
    if token_estimate(prose) > budget:
        prose = prose[: budget * 4]
    card_version = int(_RAM["card_version"] if _RAM else 0) + 1
    pack = {
        "schema_version": SCHEMA_VERSION,
        "card_version": card_version,
        "updated_at": utcnow().isoformat(),
        "through_event_id": through,
        "memory_behavior": MEMORY_BEHAVIOR,
        "relationship": prose,
        "active_project": state.active_project,
        "last_episode": episode_text or None,
        "open_loops": loop_titles,
        "tokens": token_estimate(prose),
    }
    cache_bootstrap(pack)
    return pack


def cache_bootstrap(pack: dict[str, Any]) -> None:
    global _RAM
    _RAM = dict(pack)
    note_bootstrap(
        version=int(pack.get("card_version") or 0),
        updated_at=pack.get("updated_at"),
        through_event_id=pack.get("through_event_id"),
        tokens=int(pack.get("tokens") or 0),
    )
    try:
        root = ensure_tree()
        atomic_write_json(root / "cache" / "bootstrap.json", pack)
        atomic_write_json(
            root / "manifest.json",
            {
                "schema_version": SCHEMA_VERSION,
                "authority": "postgres",
                "mirror": "rebuildable",
                "card_version": pack.get("card_version"),
                "updated_at": pack.get("updated_at"),
                "through_event_id": pack.get("through_event_id"),
            },
        )
    except OSError:
        pass


def reset_bootstrap_cache() -> None:
    global _RAM
    _RAM = None


def load_cached_bootstrap() -> dict[str, Any] | None:
    global _RAM
    if _RAM:
        return dict(_RAM)
    packed = read_json(memory_root() / "cache" / "bootstrap.json")
    if packed:
        _RAM = packed
        note_bootstrap(
            version=int(packed.get("card_version") or 0),
            updated_at=packed.get("updated_at"),
            through_event_id=packed.get("through_event_id"),
            tokens=int(packed.get("tokens") or 0),
        )
    return packed


async def get_bootstrap(session: AsyncSession | None = None) -> dict[str, Any]:
    cached = load_cached_bootstrap()
    if cached and session is None:
        return cached
    if session is None:
        from app.memory.relationship import MEMORY_BEHAVIOR

        return {
            "schema_version": SCHEMA_VERSION,
            "card_version": 0,
            "memory_behavior": MEMORY_BEHAVIOR,
            "relationship": "",
            "tokens": 0,
        }
    latest = await _latest_event_id(session)
    if cached and cached.get("relationship") and cached.get("through_event_id") == latest:
        return cached
    return await build_bootstrap(session)


def bootstrap_instructions(pack: dict[str, Any] | None) -> str:
    from app.memory.relationship import MEMORY_BEHAVIOR

    raw = pack if isinstance(pack, dict) else {}
    parts = [str(raw.get("memory_behavior") or MEMORY_BEHAVIOR).strip()]
    card = str(raw.get("relationship") or "").strip()
    if card:
        parts.append("MEMORY BOOTSTRAP (precomputed, Evie-owned): " + card)
    return "\n".join(parts)
