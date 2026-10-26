"""Skill registry: declarative manifests, deterministic find, policy-gated run.

A skill is a manifest (name, triggers, args schema, risk, permissions, channels)
plus code that returns a receipt. `find` is deterministic text scoring over the
manifests and cheap live preconditions; the brain chooses among the ranked
results. `run` goes through the ONE approval authority, `app.fabric.policy`,
and never keeps an approval store of its own: a confirm card is issued with a
`plan_id`, and the same call re-sent with `approved=True, approval_id=<plan_id>`
runs exactly that plan.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import receipts as R

RunFn = Callable[["Ctx", dict[str, Any]], dict[str, Any]]
PlanFn = Callable[["Ctx", dict[str, Any]], dict[str, Any]]

RISKS = ("read", "low", "confirm", "dynamic")


@dataclass
class Ctx:
    """Everything a skill may touch. Tests build one around FakeRuntime."""
    rt: Any
    provenance: str = "owner"
    home: Any = None
    state_dir: Any = None
    ghostty: Any = None
    browsers: list[Any] | None = None
    ax: Any = None
    ocr_fn: Callable[..., dict[str, Any]] | None = None
    vision_fn: Callable[..., dict[str, Any]] | None = None
    session_index: Any = None
    window_ids: dict[str, list[str]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Skill:
    name: str
    summary: str
    triggers: list[str]
    run: RunFn
    risk: str = "low"                         # read | low | confirm | dynamic
    args_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    permissions: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    plan: PlanFn | None = None                # builds the confirm card / dry-run plan (no effect)
    risk_fn: Callable[..., tuple[str, str | None]] | None = None  # (args, ctx) -> (action class, high-risk class)
    preconditions: Callable[[Ctx, dict[str, Any]], list[dict[str, Any]]] | None = None
    status: str = "stable"                    # stable | learned_unreviewed
    version: int = 1
    effect: bool = True                       # False: pure read/perception
    undo_name: str | None = None

    @property
    def pack(self) -> str:
        return self.name.split(".", 1)[0]

    def manifest(self) -> dict[str, Any]:
        return {"name": self.name, "pack": self.pack, "summary": self.summary,
                "args_schema": self.args_schema, "risk": self.risk, "permissions": self.permissions,
                "channels": self.channels, "status": self.status, "version": self.version,
                "effect": self.effect}


REGISTRY: dict[str, Skill] = {}


def skill(name: str, summary: str, triggers: list[str], **kw: Any) -> Callable[[RunFn], RunFn]:
    def deco(fn: RunFn) -> RunFn:
        REGISTRY[name] = Skill(name=name, summary=summary, triggers=triggers, run=fn, **kw)
        return fn

    return deco


def register(s: Skill) -> Skill:
    REGISTRY[s.name] = s
    return s


# ----------------------------------------------------------------- find ------

_WORD = re.compile(r"[a-z0-9']+")
_STOP = frozenset(["a", "an", "the", "my", "me", "please", "can", "you", "could", "would", "to", "for", "of", "on", "in", "at", "up", "it", "this", "that", "is", "are", "be", "do", "just", "now", "hey", "evie", "okay", "ok", "and", "or", "with", "from", "into", "i'd", "i'm", "let's", "lets"])


def _toks(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def score(intent: str, s: Skill) -> tuple[float, str]:
    """0..1 relevance of a skill to a spoken/typed intent, with a reason."""
    q = [_stem(w) for w in _toks(intent)]
    if not q:
        return 0.0, ""
    qset = set(q)
    best, why = 0.0, ""
    low = (intent or "").lower()
    for trig in s.triggers:
        t = [_stem(w) for w in _toks(trig)]
        if not t:
            continue
        tset = set(t)
        if re.search(r"(?<![a-z0-9'])" + re.escape(trig.lower()) + r"(?![a-z0-9'])", low):
            val = 0.9 + min(0.1, 0.02 * len(t))
        else:
            cover = len(tset & qset) / len(tset)
            extra = len(qset - tset) / max(1, len(qset))
            val = cover * (1.0 - 0.35 * extra)
            if cover == 1.0:
                val = max(val, 0.7)
        if val > best:
            best, why = val, f"matches “{trig}”"
    return round(best, 3), why


def find(intent: str, ctx: Ctx | None = None, *, limit: int = 5, context: dict[str, Any] | None = None
         ) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for s in REGISTRY.values():
        sc, why = score(intent, s)
        if sc < 0.3:
            continue
        pre: list[dict[str, Any]] = []
        if ctx is not None and s.preconditions is not None:
            try:
                pre = s.preconditions(ctx, {})
            except Exception as exc:  # a failing check is reported, never fatal
                pre = [{"check": "preconditions", "ok": False, "detail": str(exc)[:120]}]
            if any(not p.get("ok") for p in pre):
                sc = round(sc * 0.6, 3)
        rows.append({"name": s.name, "score": sc, "why": why, "summary": s.summary,
                     "args_schema": s.args_schema, "preconditions": pre,
                     "permissions": s.permissions, "risk": s.risk, "status": s.status,
                     "channels": s.channels})
    rows.sort(key=lambda r: (-float(r["score"]), str(r["name"])))
    return rows[:limit]


# ----------------------------------------------------------------- policy -----

_issued: dict[str, float] = {}
PLAN_TTL_S = 300.0


def plan_id(name: str, args: dict[str, Any], plan: dict[str, Any] | None) -> str:
    blob = json.dumps({"s": name, "a": args, "p": plan or {}}, sort_keys=True, default=str)
    return "plan-" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def _decision(s: Skill, args: dict[str, Any], provenance: str, ctx: Ctx | None = None) -> tuple[str, str | None]:
    """(AUTO|AUTO_JOURNAL|CONFIRM|DENY, high-risk class or None) from the policy engine."""
    from app.fabric import policy

    target = f"hands:{s.name}"
    cls = None
    if s.risk == "dynamic" and s.risk_fn is not None:
        action, cls = s.risk_fn(args, ctx)
    else:
        action = {"read": "read", "low": "write", "confirm": "run"}.get(s.risk, "run")
    verdict = policy.decide(action, target, provenance)
    if cls and cls in policy.HIGH_RISK:
        verdict = "CONFIRM" if verdict != "DENY" else verdict
    return verdict, cls


def gate(s: Skill, ctx: Ctx, args: dict[str, Any], *, approved: bool, approval_id: str | None,
         dry_run: bool) -> dict[str, Any] | None:
    """None to proceed; otherwise a receipt (denied / confirm card) that performed no effect."""
    import time

    from app.fabric import policy

    verdict, cls = _decision(s, args, ctx.provenance, ctx)
    if verdict == "DENY":
        return R.fail("denied", f"Policy denies {s.name}.", provenance=ctx.provenance, skill=s.name)
    needs_card = verdict == "CONFIRM"
    if not needs_card and not dry_run:
        return None
    plan = s.plan(ctx, args) if s.plan is not None else {}
    if plan.get("error"):
        return R.fail(plan["error"].get("code", "precondition_failed"), plan["error"].get("message", "plan failed"),
                      fix=plan["error"].get("fix"), skill=s.name, provenance=ctx.provenance,
                      evidence=plan.get("evidence"))
    pid = plan_id(s.name, args, plan.get("steps"))
    physical = bool(cls and policy.physical_confirm_required(cls)) or bool(plan.get("physical"))
    if dry_run or (needs_card and not (approved and approval_id == pid and _issued.get(pid, 0) > time.time())):
        card = dict(plan.get("confirm") or {})
        card.setdefault("title", s.summary)
        card.setdefault("rows", [])
        card.update(risk="physical" if physical else "confirm", physical=physical, plan_id=pid,
                    expires_s=int(PLAN_TTL_S))
        if needs_card:
            _issued[pid] = time.time() + PLAN_TTL_S
        if dry_run:
            return R.receipt(True, verified=False, skill=s.name, plan=plan.get("steps") or [],
                             evidence={"dry_run": True, **(plan.get("evidence") or {})},
                             needs_confirm=needs_card, confirm=card if needs_card else None,
                             provenance=ctx.provenance,
                             say="Here is what I would do." if plan.get("steps") else None)
        return R.receipt(False, skill=s.name, plan=plan.get("steps") or [], needs_confirm=True,
                         confirm=card, provenance=ctx.provenance,
                         err=R.error("confirm_required", "This needs your confirmation first.", retryable=True),
                         say="I need your confirmation first.")
    _issued.pop(pid, None)
    return None


def run(name: str, ctx: Ctx, args: dict[str, Any] | None = None, *, dry_run: bool = False,
        approved: bool = False, approval_id: str | None = None) -> dict[str, Any]:
    s = REGISTRY.get(name)
    args = dict(args or {})
    if s is None:
        return R.fail("not_found", f"There is no skill named {name!r}.", skill=name,
                      provenance=ctx.provenance)
    problem = validate_args(s, args)
    if problem:
        return R.fail("invalid_args", problem, skill=name, provenance=ctx.provenance)
    held = gate(s, ctx, args, approved=approved, approval_id=approval_id, dry_run=dry_run)
    if held is not None:
        return held
    if s.preconditions is not None:
        checks = s.preconditions(ctx, args)
        failed = [c for c in checks if not c.get("ok") and c.get("hard", True)]
        if failed:
            f = failed[0]
            return R.fail(f.get("code", "precondition_failed"), f.get("detail") or f"{f.get('check')} failed",
                          fix=f.get("fix"), skill=name, provenance=ctx.provenance,
                          evidence={"preconditions": checks})
    try:
        out = s.run(ctx, args)
    except Exception as exc:  # a skill bug must surface as an honest receipt, not a crash
        return R.fail("internal", f"{name} failed: {type(exc).__name__}: {str(exc)[:160]}", skill=name,
                      provenance=ctx.provenance)
    out.setdefault("skill", name)
    out["provenance"] = ctx.provenance
    if s.status == "learned_unreviewed":
        out["learned"] = {"skill": name, "status": s.status, "version": s.version}
    # R6: a success claim needs a verify step; strip it otherwise
    if out.get("ok") and not out.get("verified") and out.get("say") and s.effect:
        out["say"] = None
    return out


def validate_args(s: Skill, args: dict[str, Any]) -> str | None:
    schema = s.args_schema or {}
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in args:
            return f"{s.name} needs '{req}'."
    for key, val in args.items():
        spec = props.get(key)
        if spec is None:
            continue
        typ = spec.get("type")
        ok = {"string": isinstance(val, str), "number": isinstance(val, int | float) and not isinstance(val, bool),
              "integer": isinstance(val, int) and not isinstance(val, bool), "boolean": isinstance(val, bool),
              "array": isinstance(val, list), "object": isinstance(val, dict), None: True}.get(typ, True)
        if not ok:
            return f"'{key}' must be {typ}."
        if "enum" in spec and val not in spec["enum"]:
            return f"'{key}' must be one of {spec['enum']}."
    return None
