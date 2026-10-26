"""Real-brain mode: the brain chooses skills through SKILLS_API with a thin tool-calling loop.

Credentials are obtained the way the live service obtains them (repo .env, then the production
secret file) and are never printed. Cost is read from the provider's own usage fields. The run
is capped by the SMALLER of EV_HANDS_EVAL_BUDGET_USD and the key's remaining credit minus a
reserve, because the key is shared with the live assistant (B7: state the number, run what fits).
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any

import httpx

from app.fabric.skills import registry

from . import tasks_sim  # noqa: F401  (registers tasks)
from .core import TASKS, SimEnv, Task, Verdict

PRICE_PROMPT, PRICE_COMPLETION = 0.10 / 1e6, 0.20 / 1e6  # contributor tier (docs/PROVIDER_FACTS.md), fallback only
RESERVE_USD = 0.25

SYSTEM = (
    "You operate the owner's Mac through a small API. The owner speaks to you; you act with the tools. "
    "Use skill_find to look up skills for what was asked, then skill_run with the right arguments. "
    "Rules: do only what was asked. If a receipt has verified=false or ok=false, say so plainly; never claim "
    "success the receipt does not support. If a receipt needs confirmation, stop and say what needs confirming. "
    "Keep your final answer to one short sentence."
)

TOOLS = [
    {"type": "function", "function": {
        "name": "skill_find", "description": "Find skills for an intent. Returns ranked skills with argument schemas.",
        "parameters": {"type": "object", "properties": {"intent": {"type": "string"}}, "required": ["intent"]}}},
    {"type": "function", "function": {
        "name": "skill_run", "description": "Run a skill by name with arguments. Returns a receipt.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "args": {"type": "object"}},
                       "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "world_query", "description": "Read the Mac's state: select is one of apps, windows, tabs, terminals, media, system, focus.",
        "parameters": {"type": "object", "properties": {"select": {"type": "string"}, "where": {"type": "object"}},
                       "required": ["select"]}}},
]


def _key() -> str | None:
    from dotenv import dotenv_values

    for path in (Path(__file__).resolve().parents[2] / ".env", Path.home() / ".ev" / "secrets" / "production.env"):
        if path.is_file():
            v = (dotenv_values(path).get("EV_OPENROUTER_API_KEY") or "").strip()
            if v:
                return v
    return (os.environ.get("EV_OPENROUTER_API_KEY") or "").strip() or None


def headroom() -> dict[str, Any]:
    key = _key()
    if not key:
        return {"ok": False, "reason": "no key"}
    r = httpx.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    d = r.json().get("data", {})
    return {"ok": True, "limit": d.get("limit"), "remaining": d.get("limit_remaining"), "usage": d.get("usage")}


class Budget:
    def __init__(self) -> None:
        from app.config import settings

        h = headroom()
        self.key_remaining = float(h.get("remaining") or 0.0) if h.get("ok") else 0.0
        self.configured = float(settings.hands_eval_budget_usd)
        self.cap = max(0.0, min(self.configured, self.key_remaining - RESERVE_USD))
        self.spent = 0.0
        self.calls = 0
        self.model = settings.brain_model
        self.effort = settings.brain_reasoning_effort
        self.base = settings.openrouter_base_url.rstrip("/")
        self.cost_per_call: list[float] = []

    def can_afford(self, n_calls: int = 4) -> bool:
        per = (sum(self.cost_per_call) / len(self.cost_per_call)) if self.cost_per_call else 0.002
        return self.spent + per * n_calls <= self.cap

    def chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        key = _key()
        assert key
        body = {"model": self.model, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
                "reasoning": {"effort": self.effort}, "usage": {"include": True}, "max_tokens": 1200}
        r = httpx.post(f"{self.base}/chat/completions", json=body, timeout=90,
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        data = r.json()
        u = data.get("usage") or {}
        cost = u.get("cost")
        if cost is None:
            cost = (u.get("prompt_tokens", 0) * PRICE_PROMPT) + (u.get("completion_tokens", 0) * PRICE_COMPLETION)
        self.spent += float(cost)
        self.calls += 1
        self.cost_per_call.append(float(cost))
        return data


class BrainHands:
    """Stands in for `env.hands` inside one task: `skill_run` is decided by the brain."""

    def __init__(self, real: Any, budget: Budget, utterance: str):
        self._real, self._b, self._utt = real, budget, utterance
        self.chosen: list[str] = []
        self.steps = 0
        self.last: dict[str, Any] | None = None
        self.final_text = ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def skill_run(self, name: str, args: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": self._utt}]
        for _ in range(6):
            data = self._b.chat(msgs)
            ch = (data.get("choices") or [{}])[0].get("message") or {}
            self.steps += 1
            calls = ch.get("tool_calls") or []
            msgs.append({"role": "assistant", "content": ch.get("content") or "", **({"tool_calls": calls} if calls else {})})
            if not calls:
                self.final_text = (ch.get("content") or "").strip()
                break
            for c in calls:
                fn = c["function"]["name"]
                try:
                    a = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    a = {}
                if fn == "skill_find":
                    rows = self._real.skill_find(a.get("intent", ""), limit=4)["evidence"]["skills"]
                    out: Any = [{k: r[k] for k in ("name", "score", "summary", "args_schema", "risk")} for r in rows]
                elif fn == "skill_run":
                    self.chosen.append(a.get("name", ""))
                    self.last = self._real.skill_run(a.get("name", ""), a.get("args") or None)
                    out = {k: self.last.get(k) for k in ("ok", "verified", "say", "error", "needs_confirm", "evidence")}
                    out["evidence"] = json.dumps(out["evidence"], default=str)[:600]
                elif fn == "world_query":
                    out = self._real.world_query(a.get("select", "apps"), a.get("where"))["evidence"]["items"][:8]
                else:
                    out = {"error": "unknown tool"}
                msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": json.dumps(out, default=str)[:2500]})
        return self.last or {"ok": False, "verified": False, "evidence": {}, "error": {"code": "internal", "message": "the brain ran no skill"},
                             "say": None, "steps": [], "needs_confirm": False, "confirm": None, "undo": None,
                             "contract": "evie-hands/1", "skill": None, "channel": None, "plan": [], "provenance": "owner",
                             "learned": None, "timings_ms": {}}


class _Spy:
    def __init__(self, real: Any):
        self._real, self.n, self.names = real, 0, []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def skill_run(self, name: str, args: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        self.n += 1
        self.names.append(name)
        return self._real.skill_run(name, args, **kw)


def eligible(domains: set[str]) -> list[Task]:
    """Sim tasks that make exactly one low-risk skill_run (the brain's choice replaces that call)."""
    out = []
    for t in TASKS:
        if t.mode != "sim" or t.domain not in domains or not t.skill:
            continue
        env = SimEnv()
        spy = _Spy(None)
        try:
            env.hands = _Spy(env.hands)
            t.fn(env)
        except Exception:
            continue
        spy = env.hands
        if spy.n == 1 and spy.names[0] == t.skill:
            out.append(t)
    return out


def run(domains: set[str], limit: int = 60, seed: int = 5) -> dict[str, Any]:
    budget = Budget()
    result: dict[str, Any] = {"model": budget.model, "effort": budget.effort, "cap_usd": round(budget.cap, 4),
                              "configured_usd": budget.configured, "key_remaining_usd": round(budget.key_remaining, 4),
                              "reserve_usd": RESERVE_USD, "tasks": []}
    if budget.cap < 0.02:
        result["owner_action"] = (f"raise the OpenRouter key limit: ${budget.key_remaining:.2f} remaining, "
                                  f"${RESERVE_USD:.2f} reserved for the live assistant, nothing left to evaluate with")
        return result
    pool = eligible(domains)
    random.Random(seed).shuffle(pool)
    by_dom: dict[str, list[Task]] = {}
    for t in pool:
        by_dom.setdefault(t.domain, []).append(t)
    sample: list[Task] = []
    while len(sample) < limit and any(by_dom.values()):
        for d in sorted(by_dom):
            if by_dom[d] and len(sample) < limit:
                sample.append(by_dom[d].pop())
    result["eligible"] = len(pool)
    for i, t in enumerate(sample):
        if not budget.can_afford():
            result["stopped"] = f"budget cap ${budget.cap:.3f} reached after {len(result['tasks'])} tasks"
            break
        env = SimEnv()
        utter = t.say[i % 4]
        bh = BrainHands(env.hands, budget, utter)
        env.hands = bh
        t0 = time.time()
        try:
            v: Verdict = t.fn(env)
            ok, note = bool(v.ok), v.note
            claims = bool(v.receipt and v.receipt.get("ok") and v.receipt.get("verified"))
        except Exception as exc:
            ok, note, claims = False, f"crash {type(exc).__name__}", False
        result["tasks"].append({"id": t.id, "domain": t.domain, "utterance": utter, "variant": i % 4, "expected_skill": t.skill,
                                "chosen": bh.chosen, "right_skill": bool(bh.chosen) and bh.chosen[0] == t.skill,
                                "passed": ok, "false_success": claims and not ok, "steps": bh.steps,
                                "seconds": round(time.time() - t0, 1), "answer": bh.final_text[:140], "note": note[:100]})
    result["spent_usd"] = round(budget.spent, 5)
    result["calls"] = budget.calls
    result["mean_cost_per_call_usd"] = round(sum(budget.cost_per_call) / max(1, len(budget.cost_per_call)), 6)
    return result


_ = registry
