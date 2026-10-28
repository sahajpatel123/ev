"""FAST LOOP: observe() and snapshot(). Deterministic, no network.

observe() appends one row to our SQLite queue (WAL, NORMAL sync) and
returns; snapshot() serves from an in-memory state cache that is
invalidated by version check, applying decay at read time. Both are
no-ops returning None/None-effect while MIND_ENABLED is False.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

from app.persona_mind import store
from app.persona_mind.render import render_block
from app.persona_mind.room import read_room
from app.persona_mind.settings import MIND_VERSION
from app.persona_mind.state import apply_decay, coerce_ts
from app.persona_mind.types import MindSnapshot, TurnRecord

_cache: dict[str, tuple[int, dict[str, Any], str]] = {}
_cache_lock = threading.Lock()
_kick: threading.Event | None = None


def _enabled() -> bool:
    from app.persona_mind import settings

    return bool(settings.MIND_ENABLED)


def register_kick(event: threading.Event | None) -> None:
    global _kick
    _kick = event


def observe(record: TurnRecord) -> None:
    """Enqueue one turn record. Never raises, never blocks on the brain.

    Starts the worker lazily on first call (idempotent, file-locked), so
    no boot hook in another lane is needed.
    """
    try:
        if not _enabled():
            return
        if not isinstance(record, TurnRecord):
            return
        store.enqueue_turn(record)
        try:
            import os as _os

            if not _os.environ.get("EV_PERSONA_MIND_NO_WORKER"):
                from app.persona_mind.worker import start_worker, worker_running

                if not worker_running():
                    start_worker()
        except Exception:
            pass
        kick = _kick
        if kick is not None:
            kick.set()
    except Exception:
        pass


def _cached_state(owner_id: str) -> tuple[dict[str, Any], str] | None:
    version = store.state_version(owner_id)
    if version <= 0:
        with _cache_lock:
            _cache.pop(owner_id, None)
        return None
    with _cache_lock:
        hit = _cache.get(owner_id)
    if hit is not None and hit[0] == version:
        return hit[1], hit[2]
    row = store.get_state(owner_id)
    if row is None:
        with _cache_lock:
            _cache.pop(owner_id, None)
        return None
    with _cache_lock:
        _cache[owner_id] = (row["version"], row["data"], row["updated_at"])
    return row["data"], row["updated_at"]


def invalidate(owner_id: str | None = None) -> None:
    with _cache_lock:
        if owner_id is None:
            _cache.clear()
        else:
            _cache.pop(owner_id, None)


def snapshot(
    owner_id: str, thread_id: str, query: str, channel: str, budget_tokens: int
) -> MindSnapshot | None:
    """Render the cached MIND block. None when disabled or no state yet."""
    try:
        if not _enabled():
            return None
        cached = _cached_state(owner_id)
        if cached is None:
            return None
        data, updated_at = cached
        now = datetime.now(UTC)
        vector = apply_decay(data, now)
        try:
            room_line = read_room(
                owner_id, query or "", now=now,
                banter=float(data.get("banter_appetite", 0.5)),
                patterns=list(data.get("owner_patterns") or []),
            ).room_line
        except Exception:
            room_line = None
        try:
            from app.persona_mind.tune import active as active_config

            lessons = list(active_config().get("lessons") or [])
        except Exception:
            lessons = []
        text, size = render_block(
            data, vector, query or "", budget_tokens, now, room_line, lessons
        )
        try:
            age = max(0.0, (now - coerce_ts(updated_at)).total_seconds())
        except Exception:
            age = 0.0
        return MindSnapshot(
            text=text, token_estimate=size, version=MIND_VERSION, state_age_s=age
        )
    except Exception:
        return None
