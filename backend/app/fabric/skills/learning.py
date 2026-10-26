"""Skill learning and self-healing (SKILLS_API §4 `learned.*`, task F).

When the brain finishes a novel UI task through the primitives and the result
VERIFIED, the recorded trace compiles into a parameterised skill: semantic
selectors only (no coordinates), explicit waits, and the postcondition that
verified. A candidate is validated by replay on a fresh instance before it is
stored as `learned_unreviewed`; every receipt from it says so.

Self-healing: when a step's selector no longer matches (a site changed), the
replay perceives the page again, repairs that step's selector with the closest
element of the same role, re-verifies the postcondition, and saves the repaired
version with its history. A repair that does not re-verify is discarded.
Tasks triggered by untrusted content are never learned.
"""

from __future__ import annotations

import difflib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from . import receipts as R
from . import registry
from .packs.common import state_dir
from .receipts import Recorder
from .registry import Ctx, Skill
from .ui import UI

UNTRUSTED = frozenset({"untrusted_content", "untrusted", "screenshot", "web", "email", "message", "ocr", "audio",
                       "terminal_output"})
FORBIDDEN_KEYS = ("x", "y", "frame", "coords", "pos")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "task"


def _strip_coords(sel: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in sel.items() if k not in FORBIDDEN_KEYS and k != "locator"}


class Trace:
    """What the brain did through the primitives, in order."""

    def __init__(self, task: str, provenance: str = "owner", params: dict[str, Any] | None = None):
        self.task = task
        self.provenance = provenance
        self.params = dict(params or {})
        self.steps: list[dict[str, Any]] = []
        self.postcondition: dict[str, Any] | None = None
        self.verified = False

    def act(self, selector: dict[str, Any], action: str, args: dict[str, Any] | None = None) -> None:
        self.steps.append({"op": "act", "selector": _strip_coords(selector), "action": action, "args": dict(args or {})})

    def wait(self, predicate: dict[str, Any], timeout_s: float = 10.0) -> None:
        self.steps.append({"op": "wait", "predicate": predicate, "timeout_s": timeout_s})

    def verified_with(self, postcondition: dict[str, Any]) -> None:
        self.postcondition, self.verified = postcondition, True


def compile_trace(trace: Trace) -> dict[str, Any]:
    """A parameterised spec from a verified, owner-triggered trace; or {"error": ...}."""
    if trace.provenance in UNTRUSTED:
        return {"error": "tasks triggered by untrusted content are never learned"}
    if not trace.verified or not trace.postcondition:
        return {"error": "only verified tasks are learned"}
    if not any(s["op"] == "act" for s in trace.steps):
        return {"error": "nothing to learn: no actions"}
    inv = {str(v): k for k, v in trace.params.items() if isinstance(v, str) and len(v) >= 2}

    def parametrise(value: Any) -> Any:
        if isinstance(value, str):
            for lit, name in inv.items():
                value = value.replace(lit, "{" + name + "}")
            return value
        if isinstance(value, dict):
            return {k: parametrise(v) for k, v in value.items()}
        if isinstance(value, list):
            return [parametrise(v) for v in value]
        return value

    steps = []
    for s in trace.steps:
        steps.append(parametrise(s))
    spec = {"name": f"learned.{slug(trace.task)}", "task": trace.task, "version": 1,
            "params": {k: type(v).__name__ for k, v in trace.params.items()},
            "steps": steps, "postcondition": parametrise(trace.postcondition),
            "status": "learned_unreviewed", "history": [], "created_id": uuid.uuid4().hex[:8]}
    blob = json.dumps(spec)
    if re.search(r'"(frame|coords|pos)"\s*:', blob):
        return {"error": "a compiled skill may not contain raw coordinates"}
    return spec


def _fill(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return re.sub(r"\{(\w+)\}", lambda m: str(params.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: _fill(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, params) for v in value]
    return value


class Learned:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def _dir(self) -> Path:
        d = state_dir(self.ctx) / "learned"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- storing / loading -------------------------------------------------------------
    def save(self, spec: dict[str, Any]) -> None:
        (self._dir() / f"{spec['name']}.json").write_text(json.dumps(spec))

    def load(self, name: str) -> dict[str, Any] | None:
        try:
            return json.loads((self._dir() / f"{name}.json").read_text())
        except (OSError, ValueError):
            return None

    def load_all(self) -> int:
        n = 0
        for p in self._dir().glob("learned.*.json"):
            try:
                spec = json.loads(p.read_text())
            except ValueError:
                continue
            self.register(spec)
            n += 1
        return n

    def register(self, spec: dict[str, Any]) -> None:
        name = spec["name"]
        params = {k: {"type": {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}.get(t, "string")}
                  for k, t in spec.get("params", {}).items()}
        registry.register(Skill(
            name=name, summary=f"(learned) {spec['task']}", triggers=[spec["task"]],
            run=lambda ctx, args, _n=name: Learned(ctx).replay(_n, args),
            risk="confirm", status="learned_unreviewed", version=int(spec.get("version", 1)),
            args_schema={"type": "object", "properties": params}, channels=["dom", "ax"],
            plan=lambda ctx, args, _n=name: {
                "steps": [{"op": s["op"], "action": s.get("action"), "target": (s.get("selector") or {}).get("name")}
                          for s in (Learned(ctx).load(_n) or {}).get("steps", [])],
                "confirm": {"title": f"Run learned skill: {spec['task']}", "rows": [
                    {"label": f"step {i + 1}", "command": f"{s.get('action', s['op'])} {(s.get('selector') or {}).get('name', '')}".strip(),
                     "will_run": True, "warnings": ["learned, not yet reviewed"], "untrusted_source": False}
                    for i, s in enumerate(spec["steps"])]}}))

    # -- learning -------------------------------------------------------------------------
    def learn(self, trace: Trace, *, replay_setup: Any = None, params_for_replay: dict[str, Any] | None = None
              ) -> dict[str, Any]:
        """Compile, validate by replay on a fresh instance (`replay_setup()`), then store."""
        try:
            from app.config import settings

            if not settings.hands_learn_enabled:
                return R.fail("denied", "Learning new skills is turned off.")
        except Exception:
            pass
        spec = compile_trace(trace)
        if "error" in spec:
            return R.fail("precondition_failed", spec["error"], skill="learning.learn")
        if replay_setup is not None:
            replay_setup()
        self.register(spec)
        self.save(spec)
        out = self.replay(spec["name"], params_for_replay or dict(trace.params), _validating=True)
        if not (out["ok"] and out["verified"]):
            registry.REGISTRY.pop(spec["name"], None)
            p = self._dir() / f"{spec['name']}.json"
            if p.exists():
                p.unlink()
            return R.fail("unverified", "The compiled skill did not replay successfully, so I didn't keep it.",
                          skill="learning.learn", evidence={"replay": out.get("evidence")})
        return R.receipt(True, verified=True, skill="learning.learn",
                         evidence={"name": spec["name"], "steps": len(spec["steps"])},
                         learned={"skill": spec["name"], "status": "learned_unreviewed", "version": 1},
                         say=f"I learned “{spec['task']}” and replayed it successfully. It is marked unreviewed.")

    # -- replay + self-healing ----------------------------------------------------------------
    def replay(self, name: str, params: dict[str, Any] | None = None, *, _validating: bool = False) -> dict[str, Any]:
        spec = self.load(name)
        if spec is None:
            return R.fail("not_found", f"I don't have a learned skill called {name}.")
        ui = UI(self.ctx)
        rec = Recorder(name, clock=self.ctx.rt.monotonic)
        params = params or {}
        steps = _fill(spec["steps"], params)
        repairs: list[dict[str, Any]] = []
        for i, s in enumerate(steps):
            if s["op"] == "wait":
                w = ui.wait_until(s["predicate"], timeout_s=float(s.get("timeout_s", 10)))
                rec.add("wait", "predicate", w["ok"], evidence=w.get("evidence"))
                if not w["ok"]:
                    return rec.finish(False, err=R.error("timeout", f"step {i + 1}: the wait did not finish"),
                                      evidence={"step": i + 1})
                continue
            res = ui.act(s["selector"], s["action"], s.get("args"), restore_focus=False)
            if not res["ok"] and res.get("error", {}).get("code") in ("not_found", "stale_ref"):
                fixed = self._repair(ui, s, i)
                if fixed:
                    res = ui.act(fixed, s["action"], s.get("args"), restore_focus=False)
                    if res["ok"]:
                        repairs.append({"step": i, "old": s["selector"], "new": fixed})
                        s["selector"] = fixed
            rec.add("act", f"{s['action']} {(s['selector'] or {}).get('name', '')}".strip(), res["ok"],
                    channel=res.get("channel"), evidence={"repaired": bool(repairs and repairs[-1]['step'] == i)})
            if not res["ok"]:
                return rec.finish(False, err=res.get("error") or R.error("not_found", f"step {i + 1} failed"),
                                  evidence={"step": i + 1}, say=f"A step failed: {res.get('say') or res.get('error', {}).get('message', '')}")
        post = _fill(spec["postcondition"], params)
        v = ui.wait_until(post, timeout_s=6.0)
        rec.add("verify", "postcondition", v["ok"], channel=v.get("channel"), evidence=v.get("evidence"))
        learned = {"skill": name, "status": spec["status"], "version": spec["version"]}
        if not v["ok"]:
            return rec.finish(False, err=R.error("unverified", "the postcondition did not hold afterwards"),
                              learned=learned, evidence={"repairs": repairs})
        if repairs and not _validating:
            spec["steps"] = self._apply_repairs(spec["steps"], repairs)
            spec["version"] = int(spec["version"]) + 1
            spec["history"].append({"version": spec["version"], "repairs": repairs})
            self.save(spec)
            registry.REGISTRY[name].version = spec["version"]
            learned["version"] = spec["version"]
        out = rec.finish(True, verified=True, learned=learned, evidence={"repairs": repairs, "postcondition": post},
                         say=f"Done (learned skill{', repaired' if repairs else ''}).")
        return out

    @staticmethod
    def _apply_repairs(steps: list[dict[str, Any]], repairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = json.loads(json.dumps(steps))
        for r in repairs:
            out[r["step"]]["selector"] = r["new"]
        return out

    def _repair(self, ui: UI, step: dict[str, Any], index: int) -> dict[str, Any] | None:
        """Perceive again and pick the closest element of the same role; None if nothing is close."""
        sel = dict(step["selector"])
        want = str(sel.get("name") or sel.get("text") or "").lower()
        role = sel.get("role")
        scope = {k: v for k, v in sel.items() if k in ("app", "browser", "tab", "window")}
        obs = ui.observe(scope)
        if not obs["ok"]:
            return None
        ev = obs["evidence"]
        cands: list[dict[str, Any]] = []
        if ev.get("kind") == "page":
            cands = [{"role": e["role"], "name": e["name"]} for e in ev.get("elements", [])]
        elif ev.get("kind") == "ax_tree":
            def walk(n: dict[str, Any]) -> None:
                cands.append({"role": n.get("role"), "name": n.get("title") or n.get("desc") or ""})
                for c in n.get("children", []):
                    walk(c)

            for w in ev.get("windows", []):
                walk(w)
        pool = [c for c in cands if c["name"] and (not role or str(c["role"]).lower() == str(role).lower()
                                                   or role in ("button", "link") and c["role"] in ("button", "link"))]
        best, score = None, 0.0
        for c in pool:
            nm = c["name"].lower()
            r = difflib.SequenceMatcher(None, want, nm).ratio()
            if want and (want in nm or nm in want):
                r = max(r, 0.75)
            if r > score:
                best, score = c, r
        if best is None or score < 0.55:
            return None
        fixed = {**sel, "name": best["name"], "role": best["role"] if role else sel.get("role")}
        fixed.pop("exact", None)
        fixed.pop("nth", None)
        return _strip_coords(fixed)
