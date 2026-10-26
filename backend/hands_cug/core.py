"""CUG core: Task model, registry, environments, and the scoring of one task.

A task is `fn(env) -> Verdict`. The expected outcome is written in the task BEFORE it runs
(R9): it states what the world must look like afterwards and what an honest answer is. A task
passes only when the oracle holds. It is a FALSE SUCCESS when the receipt claims success
(`ok` and `verified`) while the oracle fails. Every task carries four phrasings (canonical, two
paraphrases, one noisy spoken variant) used for routing and for the real-brain runs.
"""

from __future__ import annotations

import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.fabric.skills import registry
from app.fabric.skills.api import Hands
from app.fabric.skills.testing import FakeBrowser, FakeRuntime


@dataclass
class Verdict:
    receipt: dict[str, Any] | None
    ok: bool                       # oracle held
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    id: str
    domain: str
    mode: str                      # sim | fs | live
    say: list[str]                 # canonical, paraphrase 1, paraphrase 2, noisy spoken
    skill: str                     # skill the intent should route to ("" = primitives / no routing)
    fn: Callable[[Any], Verdict]
    stratum: str = "fixture"       # fixture | live_app | corpus
    difficulty: str = "medium"


TASKS: list[Task] = []


def task(id: str, domain: str, mode: str, say: list[str], skill: str = "", *, stratum: str | None = None,
         difficulty: str = "medium"):
    def deco(fn: Callable[[Any], Verdict]) -> Callable[[Any], Verdict]:
        assert len(say) == 4, f"{id}: need canonical + 2 paraphrases + 1 noisy variant"
        TASKS.append(Task(id, domain, mode, say, skill, fn,
                          stratum or ("fixture" if mode in ("sim", "fs") else "live_app"), difficulty))
        return fn

    return deco


# ---------------------------------------------------------------- environments --

class SimEnv:
    """FakeRuntime machine: virtual clock, scripted pages, no effects outside a temp dir."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cug-sim-"))
        self.rt = FakeRuntime(home=self.tmp / "home")
        (self.tmp / "home").mkdir()
        self.fb = FakeBrowser(self.rt)
        self.hands = Hands(self.rt, home=self.tmp / "home", state_dir=self.tmp / "state", browsers=[self.fb])

    def fresh_hands(self, **kw: Any) -> Hands:
        kw.setdefault("home", self.tmp / "home")
        kw.setdefault("state_dir", self.tmp / "state")
        kw.setdefault("browsers", [self.fb])
        return Hands(self.rt, **kw)

    def close(self) -> None:
        pass


def claims_success(receipt: dict[str, Any] | None) -> bool:
    return bool(receipt and receipt.get("ok") and receipt.get("verified"))


@dataclass
class Scored:
    id: str
    domain: str
    mode: str
    stratum: str
    passed: bool
    false_success: bool
    seconds: float
    note: str
    say: str | None
    error_code: str | None
    first_effect_ms: int | None
    total_ms: int | None
    scope_violation: bool = False
    crashed: bool = False
    difficulty: str = "medium"
    extra: dict[str, Any] = field(default_factory=dict)


def run_task(t: Task, env: Any) -> Scored:
    t0 = time.time()
    try:
        v = t.fn(env)
    except Exception as exc:  # a task that crashes is a failure, never a pass
        return Scored(t.id, t.domain, t.mode, t.stratum, False, False, time.time() - t0,
                      f"crash: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-400:]}", None, None, None, None,
                      crashed=True)
    r = v.receipt or {}
    false_success = claims_success(v.receipt) and not v.ok
    err = (r.get("error") or {}).get("code") if isinstance(r.get("error"), dict) else None
    tm = r.get("timings_ms") or {}
    ok = bool(v.ok)
    false_success = claims_success(v.receipt) and not ok
    return Scored(t.id, t.domain, t.mode, t.stratum, ok, false_success, time.time() - t0, v.note,
                  r.get("say"), err, tm.get("first_effect"), tm.get("total"), difficulty=t.difficulty,
                  extra={k: val for k, val in v.extra.items() if k in ("latency_ms", "latencies_ms", "p95_ms", "cpu_pct", "repairs", "row", "audit")})


def routing(tasks: list[Task]) -> dict[str, Any]:
    """Deterministic skill.find over every phrasing: top-1 and top-3 accuracy."""
    from app.fabric.skills.packs import load_all

    load_all()
    n = top1 = top3 = 0
    misses: list[dict[str, Any]] = []
    for t in tasks:
        if not t.skill:
            continue
        for u in t.say:
            got = [r["name"] for r in registry.find(u, limit=3)]
            n += 1
            top1 += bool(got and got[0] == t.skill)
            top3 += t.skill in got
            if not got or got[0] != t.skill:
                misses.append({"task": t.id, "utterance": u, "want": t.skill, "got": got[:3]})
    return {"n": n, "top1": top1, "top3": top3, "misses": misses}
