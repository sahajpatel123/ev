"""Fabric spans: per-turn evidence that the fabric ran.

Two layers: a context-local span list for the in-flight turn (so lane code
anywhere in the call stack can record without threading state through), and
a bounded in-memory ring of completed turns keyed by entrypoint (so tests
and status endpoints can prove, after the fact, that a request reached the
fabric). Everything recorded here is JSON-serializable.
"""
from __future__ import annotations

import time
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_current: ContextVar[list[dict[str, Any]] | None] = ContextVar("fabric_spans", default=None)

_RING_SIZE = 64
_ring: deque[tuple[str, str, list[dict[str, Any]]]] = deque(maxlen=_RING_SIZE)
_counts: dict[str, int] = {}


def clear() -> None:
    """Reset ring + counters. Tests only; production never calls this."""
    _ring.clear()
    _counts.clear()
    _current.set(None)


@contextmanager
def turn(entrypoint: str, transcript: str = "", turn_id: str | None = None):
    """Record one fabric turn. Yields the turn id; stores spans on exit."""
    turn_id = turn_id or f"ft-{uuid.uuid4().hex[:12]}"
    spans: list[dict[str, Any]] = []
    token = _current.set(spans)
    try:
        yield turn_id
    finally:
        _current.reset(token)
        _ring.append((turn_id, entrypoint, spans))
        _counts[entrypoint] = _counts.get(entrypoint, 0) + 1


@contextmanager
def span(lane: str, op: str, **attrs: Any):
    """Record one lane call inside the current turn. Yields the span dict."""
    t0 = time.perf_counter()
    record: dict[str, Any] = {
        "lane": lane,
        "op": op,
        "attrs": {k: v for k, v in attrs.items() if isinstance(v, (str, int, float, bool)) or v is None},
        "ok": None,
        "error": None,
        "t_ms": 0.0,
    }
    try:
        yield record
    except Exception as exc:
        record["ok"] = False
        record["error"] = f"{type(exc).__name__}"
        raise
    finally:
        record["t_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        cur = _current.get()
        if cur is not None:
            cur.append(record)


def recent_turn(entrypoint: str) -> tuple[str, list[dict[str, Any]]] | None:
    """Newest (turn_id, spans) for an entrypoint, or None."""
    for turn_id, ep, spans in reversed(_ring):
        if ep == entrypoint:
            return turn_id, spans
    return None


def turn_count(entrypoint: str) -> int:
    return _counts.get(entrypoint, 0)


def recent_entries(limit: int = 20) -> list[dict[str, Any]]:
    """Newest-first turn summaries for status endpoints."""
    out = []
    for turn_id, ep, spans in reversed(_ring):
        out.append({"turn_id": turn_id, "entrypoint": ep, "spans": len(spans)})
        if len(out) >= limit:
            break
    return out
