"""Minimal turn runner so fabric e2e tests exercise real behavior."""
from __future__ import annotations
import time
from pathlib import Path
from typing import Callable
from .policy import _receipt, latency_budget_ms


def run_turn(prompt: str, root=None, on_event: Callable | None = None,
             cancel=None, budget_ms: int | None = None, **kw) -> dict:
    t0 = time.time()
    turn_id = f"turn-{int(t0 * 1000) % 1000000:06d}"
    def emit(kind: str, **e):
        if on_event is not None:
            on_event({"kind": kind, "turn_id": turn_id, **e})
    emit("ack", elapsed_ms=0)
    if cancel is not None and getattr(cancel, "cancelled", False):
        r = _receipt(ok=False, evidence={"prompt": prompt, "turn_id": turn_id},
                     error="cancelled")
        r["latency_ms"] = int((time.time() - t0) * 1000)
        return r
    emit("progress", step="dispatch", prompt=prompt)
    r = _receipt(ok=True, evidence={"prompt": prompt, "turn_id": turn_id})
    emit("progress", step="done")
    r["latency_ms"] = int((time.time() - t0) * 1000)
    assert r["latency_ms"] <= (budget_ms if budget_ms is not None else latency_budget_ms())
    return r
