"""Silent prefetch: hot cache only. Never injects into the model turn.

Default EV_MEMORY_PREFETCH=off. shadow records hits; on only fills RAM.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory.observe import log_memory
from app.memory.os_health import note_prefetch
from app.utils.text import utcnow

_TTL_S = 90.0
_CACHE: dict[str, dict[str, Any]] = {}
_STATS = {"triggers": 0, "hits": 0, "misses": 0, "useful": 0, "wasted": 0}


def prefetch_mode() -> str:
    return (settings.memory_prefetch or "off").strip().lower()


def reset_prefetch() -> None:
    _CACHE.clear()
    _STATS.update({"triggers": 0, "hits": 0, "misses": 0, "useful": 0, "wasted": 0})


def snapshot() -> dict[str, Any]:
    denom = _STATS["hits"] + _STATS["misses"]
    return {
        "prefetch_mode": prefetch_mode(),
        "prefetch_hit_rate": round(_STATS["hits"] / denom, 3) if denom else None,
        "prefetch_triggers": _STATS["triggers"],
        "prefetch_useful_hits": _STATS["useful"],
        "prefetch_wasted": _STATS["wasted"],
        "prefetch_entries": len(_CACHE),
    }


def _scope_key(scope: str) -> str:
    return (scope or "evie").strip().lower()[:80]


async def prefetch(session: AsyncSession, scope: str | None) -> dict[str, Any] | None:
    mode = prefetch_mode()
    if mode not in {"shadow", "on"}:
        return None
    name = _scope_key(scope or "")
    if not name:
        return None
    started = time.perf_counter()
    from app.memory.state import get_project_state

    state = await get_project_state(session, scope)
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    entry = {
        "scope": name,
        "fetched_at": utcnow().isoformat(),
        "expires_at": time.time() + _TTL_S,
        "card_version": int(time.time()),
        "state": state if mode == "on" else {"scope": name, "open_loop_count": len(state.get("open_loops") or [])},
    }
    _CACHE[name] = entry
    _STATS["triggers"] += 1
    note_prefetch(ms=elapsed, trigger="scope")
    log_memory(
        "memory.prefetch_started",
        extra={"scope": name, "prefetch_ms": elapsed, "mode": mode, "cards": 1},
    )
    return entry


def lookup(scope: str | None) -> dict[str, Any] | None:
    name = _scope_key(scope or "")
    entry = _CACHE.get(name)
    if not entry:
        _STATS["misses"] += 1
        return None
    if float(entry.get("expires_at") or 0) < time.time():
        _CACHE.pop(name, None)
        _STATS["misses"] += 1
        return None
    _STATS["hits"] += 1
    _STATS["useful"] += 1
    log_memory("memory.prefetch_hit", extra={"scope": name})
    note_prefetch(hit=True)
    return entry


def note_wasted() -> None:
    _STATS["wasted"] += 1
