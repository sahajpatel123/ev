"""Receipts for the HANDS skills package (docs/contracts/SKILLS_API.md §1).

Every effectful or perceiving call returns one receipt shape. `verified` is
only ever set by a verify step that read the world back, and `say` is only
ever a sentence that the receipt itself supports (R6).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any

CONTRACT = "evie-hands/1"
API_VERSION = "1.0.0"

CODES = frozenset({
    "not_running", "not_found", "ambiguous", "permission_missing", "js_disabled",
    "locked", "display_asleep", "unsupported", "precondition_failed", "timeout",
    "denied", "confirm_required", "unverified", "bridge_unavailable", "stale_ref",
    "invalid_args", "internal",
})

PHASES = ("resolve", "perceive", "act", "wait", "verify", "recover")


def error(code: str, message: str, *, fix: str | None = None,
          retryable: bool = False) -> dict[str, Any]:
    if code not in CODES:
        code = "internal"
    return {"code": code, "message": message, "fix": fix, "retryable": retryable}


def receipt(ok: bool, *, verified: bool = False, evidence: dict | None = None,
            err: dict | None = None, undo: dict | None = None, skill: str | None = None,
            channel: str | None = None, plan: list | None = None,
            steps: list | None = None, provenance: str = "owner",
            needs_confirm: bool = False, confirm: dict | None = None,
            say: str | None = None, learned: dict | None = None,
            timings_ms: dict | None = None) -> dict[str, Any]:
    """Build a receipt. `verified` without `ok` is meaningless and is clamped."""
    return {
        "contract": CONTRACT,
        "ok": bool(ok),
        "verified": bool(verified and ok),
        "evidence": evidence or {},
        "error": err,
        "undo": undo,
        "skill": skill,
        "channel": channel,
        "plan": plan or [],
        "steps": steps or [],
        "provenance": provenance,
        "needs_confirm": bool(needs_confirm),
        "confirm": confirm,
        "say": say,
        "learned": learned,
        "timings_ms": timings_ms or {},
    }


def fail(code: str, message: str, *, fix: str | None = None, retryable: bool = False,
         evidence: dict | None = None, **kw: Any) -> dict[str, Any]:
    return receipt(False, err=error(code, message, fix=fix, retryable=retryable),
                   evidence=evidence, say=kw.pop("say", None) or message, **kw)


def is_ok(r: dict | None) -> bool:
    return bool(r and r.get("ok"))


class Recorder:
    """Collects ordered, timed steps and builds the final receipt."""

    def __init__(self, skill: str | None = None, *, provenance: str = "owner",
                 clock=time.monotonic):
        self.skill = skill
        self.provenance = provenance
        self.steps: list[dict[str, Any]] = []
        self._clock = clock
        self._t0 = clock()
        self._first_effect_ms: int | None = None
        self.channel: str | None = None

    def add(self, phase: str, op: str, ok: bool, *, channel: str | None = None,
            ms: int | None = None, evidence: dict | None = None,
            err: str | None = None) -> dict[str, Any]:
        step = {"n": len(self.steps) + 1, "phase": phase if phase in PHASES else "act",
                "op": op, "channel": channel, "ok": bool(ok),
                "ms": 0 if ms is None else int(ms),
                "evidence": evidence or {}, "error": err}
        if phase == "act" and ok and self._first_effect_ms is None:
            self._first_effect_ms = int((self._clock() - self._t0) * 1000)
        if channel and phase in ("act", "perceive"):
            self.channel = channel
        self.steps.append(step)
        return step

    @contextmanager
    def step(self, phase: str, op: str, channel: str | None = None):
        """`with rec.step("act", "dom.click") as s: ...; s["ok"]=True; s["evidence"]=...`"""
        t = self._clock()
        slot: dict[str, Any] = {"ok": False, "evidence": {}, "error": None}
        try:
            yield slot
        finally:
            self.add(phase, op, bool(slot.get("ok")), channel=channel,
                     ms=int((self._clock() - t) * 1000),
                     evidence=slot.get("evidence"), err=slot.get("error"))

    def verified_ok(self) -> bool:
        return any(s["phase"] == "verify" and s["ok"] for s in self.steps)

    def total_ms(self) -> int:
        return int((self._clock() - self._t0) * 1000)

    def finish(self, ok: bool, *, verified: bool | None = None,
               evidence: dict | None = None, err: dict | None = None,
               undo: dict | None = None, say: str | None = None,
               plan: list | None = None, learned: dict | None = None) -> dict[str, Any]:
        if verified is None:
            verified = self.verified_ok()
        if not ok:
            verified = False
        timings = {"total": self.total_ms()}
        if self._first_effect_ms is not None:
            timings["first_effect"] = self._first_effect_ms
        return receipt(ok, verified=verified, evidence=evidence, err=err, undo=undo,
                       skill=self.skill, channel=self.channel, plan=plan,
                       steps=self.steps, provenance=self.provenance, say=say,
                       learned=learned, timings_ms=timings)
