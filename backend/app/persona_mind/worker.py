"""Background worker: one per process, guarded by a file lock.

Wakes on observe() kicks and on a poll interval. Reflects when 3+ turns
are queued, when any queued turn is salient, or on the first turn after
2 h idle (decay + consolidate). Every 50th reflection runs a drift audit.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.persona_mind import store
from app.persona_mind.fast import invalidate, register_kick
from app.persona_mind.paths import lock_path
from app.persona_mind.reflect import (
    audit_drift,
    brain_available,
    ensure_state,
    gateway_reflect,
    is_salient,
    reflect_batch,
)
from app.persona_mind.settings import (
    AUDIT_EVERY,
    IDLE_REFLECT_S,
    REFLECT_BATCH,
    WORKER_POLL_S,
)
from app.persona_mind.state import apply_decay, coerce_ts, expire_items

_lock_guard = threading.Lock()
_worker: threading.Thread | None = None
_stop: threading.Event | None = None
_kick_event: threading.Event | None = None
_lock_handle: Any = None


def _try_acquire(path: Path) -> Any:
    """Non-blocking exclusive file lock. Returns the handle or None."""
    try:
        import fcntl

        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("w")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        handle.write(str(__import__("os").getpid()))
        handle.flush()
        return handle
    except Exception:
        return None


def _release(handle: Any) -> None:
    with suppress(Exception):
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with suppress(Exception):
        handle.close()


def _gateway_sync(messages: list[dict[str, str]]) -> dict[str, Any] | None:
    try:
        return asyncio.run(gateway_reflect(messages))
    except Exception:
        return None


def _commit(owner_id: str, state: dict[str, Any]) -> None:
    store.put_state(owner_id, state)
    invalidate(owner_id)


def drain_once(now: datetime | None = None) -> dict[str, Any]:
    """Process whatever is queued right now. Returns a drain report."""
    moment = coerce_ts(now) if now is not None else datetime.now(UTC)
    batch = store.queued_turns(REFLECT_BATCH * 4)
    report: dict[str, Any] = {"queued": len(batch), "reflected": 0, "idle": False}
    if not batch:
        _idle_pass(moment, report)
        return report
    salient = [t for t in batch if is_salient(t)]
    if len(batch) < REFLECT_BATCH and not salient:
        return report
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for turn in batch:
        by_owner.setdefault(str(turn.get("owner_id") or "owner"), []).append(turn)
    use_brain = brain_available()
    for owner_id, turns in by_owner.items():
        state = ensure_state(owner_id, moment)
        context = store.recent_done_turns(owner_id, 5)
        result = reflect_batch(
            owner_id, turns, state, moment,
            reflector=_gateway_sync if use_brain else None,
            context_turns=context,
        )
        for kind, item, reason in result["rejected"]:
            store.log_rejection(kind, item, reason)
        count = store.bump_reflection_count()
        if count % AUDIT_EVERY == 0 and use_brain:
            audit = audit_drift(state, moment, reflector=_gateway_sync)
            for kind, item, reason in audit["rejected"]:
                store.log_rejection(kind, item, reason)
        _commit(owner_id, state)
        store.mark_done([str(t.get("turn_id")) for t in turns])
        store.meta_set("last_turn_at", str(turns[-1].get("ts") or moment.isoformat()))
        store.meta_set("last_owner_id", owner_id)
        store.log_reflection(
            [str(t.get("turn_id")) for t in turns],
            {"summary": result["summary"], "reflector": result["reflector"]},
        )
        report["reflected"] += len(turns)
    return report


def _idle_pass(moment: datetime, report: dict[str, Any]) -> None:
    last = store.meta_get("last_turn_at", "")
    if not last:
        return
    try:
        idle_s = (moment - coerce_ts(last)).total_seconds()
    except Exception:
        return
    if idle_s < IDLE_REFLECT_S:
        return
    if store.meta_get("idle_consolidated_at", "") == last:
        return
    # First quiet check after 2 h: persist decayed affect, expire items.
    owner_id = store.meta_get("last_owner_id", "owner")
    state = ensure_state(owner_id, moment)
    state["affect"] = apply_decay(state, moment)
    state["affect_updated_at"] = moment.isoformat()
    expire_items(state, moment)
    _commit(owner_id, state)
    store.meta_set("idle_consolidated_at", last)
    report["idle"] = True


def _loop(stop: threading.Event, kick: threading.Event) -> None:
    while not stop.is_set():
        with suppress(Exception):
            drain_once()
        kick.clear()
        kick.wait(timeout=WORKER_POLL_S)


def start_worker() -> bool:
    """Start the background worker. False when disabled or already running."""
    from app.persona_mind import settings

    global _worker, _stop, _kick_event, _lock_handle
    if not settings.MIND_ENABLED:
        return False
    with _lock_guard:
        if _worker is not None and _worker.is_alive():
            return True
        handle = _try_acquire(lock_path())
        if handle is None:
            return False
        _lock_handle = handle
        _stop = threading.Event()
        _kick_event = threading.Event()
        register_kick(_kick_event)
        _worker = threading.Thread(
            target=_loop, args=(_stop, _kick_event),
            name="persona-mind-worker", daemon=True,
        )
        _worker.start()
        return True


def stop_worker(timeout: float = 5.0) -> None:
    global _worker, _stop, _kick_event, _lock_handle
    with _lock_guard:
        stop, worker, handle = _stop, _worker, _lock_handle
        _stop, _worker, _kick_event, _lock_handle = None, None, None, None
        register_kick(None)
    if stop is not None:
        stop.set()
    if worker is not None:
        worker.join(timeout=timeout)
    if handle is not None:
        _release(handle)


def worker_running() -> bool:
    return _worker is not None and _worker.is_alive()
