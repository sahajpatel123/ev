"""HANDS public interface (docs/contracts/SKILLS_API.md, version 1.0.0).

    from app.fabric.skills.api import Hands
    hands = Hands()
    hands.skill_run("web_media.skip_ad")

Blocking calls; from async code use `await hands.aio.skill_run(...)`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import receipts as R
from . import registry, screenref
from .learning import Learned, Trace
from .packs import load_all
from .receipts import API_VERSION
from .registry import Ctx
from .runtime import Runtime, default_runtime
from .ui import UI
from .watchers import WatcherManager
from .world import World


def hands_api_version() -> str:
    return API_VERSION


class Hands:
    def __init__(self, rt: Runtime | None = None, *, home: Path | None = None, state_dir: Path | None = None,
                 ocr_fn: Callable[..., dict[str, Any]] | None = None,
                 vision_fn: Callable[..., dict[str, Any]] | None = None, ax: Any = None,
                 browsers: list[Any] | None = None, ghostty: Any = None,
                 window_ids: dict[str, list[str]] | None = None, extra: dict[str, Any] | None = None):
        load_all()
        self.rt: Runtime = rt or default_runtime()
        self._base = dict(home=home, state_dir=state_dir, ocr_fn=ocr_fn, vision_fn=vision_fn, ax=ax,
                          browsers=browsers, ghostty=ghostty, window_ids=window_ids, extra=extra or {})
        self._ctx = self._make_ctx("owner")
        self.world = World(self._ctx)
        self.ui = UI(self._ctx)
        self.watchers = WatcherManager(self._ctx)
        self.learned = Learned(self._ctx)
        self._trace: Trace | None = None
        try:
            self.learned.load_all()
        except OSError:
            pass

    def _make_ctx(self, provenance: str) -> Ctx:
        b = self._base
        ctx = Ctx(rt=self.rt, provenance=provenance, home=b["home"], state_dir=b["state_dir"], ghostty=b["ghostty"],
                  browsers=b["browsers"], ax=b["ax"], ocr_fn=b["ocr_fn"], vision_fn=b["vision_fn"],
                  window_ids=b["window_ids"], extra=b["extra"])
        return ctx

    # -- world ------------------------------------------------------------------------------
    def world_query(self, select: str, where: dict[str, Any] | None = None, *, refresh: Any = "auto",
                    max_age_s: float | None = None, limit: int = 50) -> dict[str, Any]:
        return self.world.query(select, where, refresh=refresh, max_age_s=max_age_s, limit=limit)

    def world_snapshot(self, *, refresh: Any = "auto") -> dict[str, Any]:
        return self.world.snapshot(refresh=refresh)

    # -- ui ----------------------------------------------------------------------------------
    def ui_find(self, selector: dict[str, Any], *, channels: list[str] | None = None, limit: int = 10) -> dict[str, Any]:
        return self.ui.find(selector, channels=channels, limit=limit)

    def ui_act(self, target: Any, action: str, args: dict[str, Any] | None = None, *,
               verify: dict[str, Any] | None = None, restore_focus: bool = True, provenance: str = "owner",
               approved: bool = False) -> dict[str, Any]:
        from app.fabric import policy

        verdict = policy.decide("write", f"ui:{action}", provenance)
        if verdict == "DENY":
            return R.fail("denied", "Policy denies that action.", provenance=provenance)
        if verdict == "CONFIRM" and not approved:
            return R.receipt(False, needs_confirm=True, provenance=provenance, skill=f"ui.{action}",
                             err=R.error("confirm_required", "That needs your confirmation first.", retryable=True),
                             confirm={"title": f"UI action: {action}", "rows": [{"label": str(target)[:120]}],
                                      "risk": "confirm", "physical": False,
                                      "plan_id": registry.plan_id(f"ui.{action}", {"t": str(target), "a": args}, None),
                                      "expires_s": 300}, say="I need your confirmation first.")
        self.ui.ctx.provenance = provenance
        out = self.ui.act(target, action, args, verify=verify, restore_focus=restore_focus)
        if self._trace is not None and out["ok"] and isinstance(target, dict):
            self._trace.act(target, action, args, self._ordinal_hint(target))
        return out

    def _ordinal_hint(self, target: dict[str, Any]) -> dict[str, Any] | None:
        """Where this element sat among the page's elements of its role (for later repair)."""
        role, name = str(target.get("role") or "").lower(), str(target.get("name") or "").lower()
        if not role or not (target.get("browser") or target.get("tab")):
            return None
        scope = {k: v for k, v in target.items() if k in ("browser", "tab", "app")}
        obs = self.ui.observe({**scope, "limit": 200})
        els = [e for e in (obs.get("evidence", {}).get("elements") or []) if str(e.get("role", "")).lower() == role]
        for i, e in enumerate(els):
            if name and name in str(e.get("name", "")).lower():
                return {"role": role, "ordinal": i, "count": len(els)}
        return None

    def ui_wait_until(self, predicate: dict[str, Any], *, timeout_s: float = 10.0, poll_s: Any = "adaptive") -> dict[str, Any]:
        out = self.ui.wait_until(predicate, timeout_s=timeout_s, poll_s=poll_s)
        if self._trace is not None and out["ok"]:
            self._trace.wait(predicate, timeout_s)
        return out

    def ui_observe(self, scope: dict[str, Any], *, include_image: bool = False) -> dict[str, Any]:
        return self.ui.observe(scope, include_image=include_image)

    def ui_verify(self, postcondition: dict[str, Any]) -> dict[str, Any]:
        out = self.ui.verify(postcondition)
        if self._trace is not None and out["ok"]:
            self._trace.verified_with(postcondition)
        return out

    # -- skills ------------------------------------------------------------------------------------
    def skill_list(self) -> dict[str, Any]:
        rows = [s.manifest() for s in sorted(registry.REGISTRY.values(), key=lambda s: s.name)]
        return R.receipt(True, verified=True, skill="skill.list",
                         evidence={"api_version": API_VERSION, "skills": rows, "n": len(rows)})

    def skill_find(self, intent: str, *, context: dict[str, Any] | None = None, limit: int = 5) -> dict[str, Any]:
        rows = registry.find(intent, self._ctx, limit=limit, context=context)
        return R.receipt(True, verified=True, skill="skill.find", evidence={"skills": rows, "intent": intent},
                         say=None)

    def skill_run(self, name: str, args: dict[str, Any] | None = None, *, dry_run: bool = False,
                  provenance: str = "owner", approved: bool = False, approval_id: str | None = None,
                  deadline_s: float | None = None) -> dict[str, Any]:
        ctx = self._make_ctx(provenance)
        ctx.extra = self._base["extra"]
        ctx.ax, ctx.ghostty, ctx.browsers = self._ctx.ax, self._ctx.ghostty, self._ctx.browsers
        ctx.session_index = self._ctx.session_index
        out = registry.run(name, ctx, args, dry_run=dry_run, approved=approved, approval_id=approval_id)
        self._ctx.ax, self._ctx.ghostty, self._ctx.browsers = ctx.ax, ctx.ghostty, ctx.browsers
        self._ctx.session_index = ctx.session_index
        return out

    # -- watchers ----------------------------------------------------------------------------------
    def watcher_create(self, spec: dict[str, Any], *, provenance: str = "owner", approved: bool = False) -> dict[str, Any]:
        return self.watchers.create(spec, provenance=provenance, approved=approved)

    def watcher_list(self, *, include_finished: bool = False) -> dict[str, Any]:
        return self.watchers.list(include_finished)

    def watcher_cancel(self, watcher_id: str) -> dict[str, Any]:
        return self.watchers.cancel(watcher_id)

    def watcher_events(self, since: float | None = None, watcher_id: str | None = None) -> dict[str, Any]:
        return self.watchers.get_events(since, watcher_id)

    def set_event_sink(self, fn: Callable[[dict[str, Any]], None] | None) -> None:
        self.watchers.sink = fn

    # -- screenshot as reference ------------------------------------------------------------------------
    def screen_reference(self, image: str, target: str, *, hint: str | None = None,
                         provenance: str = "screenshot") -> dict[str, Any]:
        ctx = self._make_ctx("screenshot")
        ctx.ax, ctx.ghostty, ctx.browsers = self._ctx.ax, self._ctx.ghostty, self._ctx.browsers
        ctx.session_index = self._ctx.session_index
        return screenref.reference(ctx, image, target, hint=hint)

    def screen_apply(self, plan_id: str, *, approved: bool = False, approval_id: str | None = None) -> dict[str, Any]:
        ctx = self._make_ctx("screenshot")
        ctx.ax, ctx.ghostty, ctx.browsers = self._ctx.ax, self._ctx.ghostty, self._ctx.browsers
        ctx.session_index = self._ctx.session_index
        return screenref.apply(ctx, plan_id, approved=approved, approval_id=approval_id)

    # -- learning ---------------------------------------------------------------------------------------------
    def trace_begin(self, task: str, *, provenance: str = "owner", params: dict[str, Any] | None = None) -> None:
        self._trace = Trace(task, provenance, params)

    def trace_end(self, *, replay_setup: Callable[[], None] | None = None,
                  params_for_replay: dict[str, Any] | None = None) -> dict[str, Any]:
        trace, self._trace = self._trace, None
        if trace is None:
            return R.fail("precondition_failed", "No task was being recorded.")
        return self.learned.learn(trace, replay_setup=replay_setup, params_for_replay=params_for_replay)

    # -- diagnostics ---------------------------------------------------------------------------------------------
    def permissions(self) -> dict[str, Any]:
        from .diag import permissions

        return permissions(self._ctx)

    def selftest(self, pack: str | None = None) -> dict[str, Any]:
        from .diag import selftest

        return selftest(self._ctx, pack)

    @property
    def aio(self) -> _Async:
        return _Async(self)


class _Async:
    """Same method names as `Hands`, each run in a worker thread."""

    def __init__(self, hands: Hands):
        self._h = hands

    def __getattr__(self, name: str):
        fn = getattr(self._h, name)

        async def call(*a: Any, **k: Any) -> Any:
            return await asyncio.to_thread(fn, *a, **k)

        return call
