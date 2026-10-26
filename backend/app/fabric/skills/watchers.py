"""Watchers: standing, scoped, expiring tasks with a CPU budget (SKILLS_API §5).

A watcher is condition + action + scope + expiry + cap. They cost nothing while
idle: ad skipping installs ONE in-page observer (MutationObserver + a slow
fallback timer) that clicks the instant a skip control is ready, so the host
only wakes every ~10 s to re-arm and collect events; file/process/tab watchers
poll with adaptive back-off. A watcher may take only LOW-risk actions on its
own; anything else becomes a `needs_confirm` event. No model call is made per
tick.
"""

from __future__ import annotations

import contextlib
import json
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import receipts as R
from . import registry
from .packs.browsers import BrowserError
from .packs.common import browsers as browsers_of
from .packs.common import state_dir
from .registry import Ctx

KINDS = ("skip_ads", "file_done", "process_exit", "tab_state", "custom")

WATCH_SKIP_BODY = r"""
var NS='__evHandsWatch';var old=window[NS];if(old&&old.stop)old.stop();
__PRELUDE__
var st=window[NS]={armed:true,clicks:0,log:[],deadline:Date.now()+__MAX_MS__,max:__MAX_CLICKS__,last:0,armedAt:Date.now(),checks:0};
function check(){st.checks++;if(Date.now()>st.deadline||st.clicks>=st.max){st.stop();return;}
 var v=primaryVideo();if(!v)return;var s=skipState(playerBox(v));
 if(s.state==='ready'&&s.enabled&&Date.now()-st.last>1500){st.last=Date.now();try{s.el.click();}catch(e){}st.clicks++;st.log.push({t:st.last,label:s.label});}}
var timer=null;function sched(){if(timer)return;timer=setTimeout(function(){timer=null;check();},40);}
var mo=new MutationObserver(sched);mo.observe(document.documentElement,{subtree:true,childList:true,attributes:true,characterData:true});
var iv=setInterval(check,400);
st.stop=function(){st.armed=false;try{mo.disconnect();}catch(e){}clearInterval(iv);if(timer)clearTimeout(timer);};
check();return {armed:true,armedAt:st.armedAt};
"""

WATCH_STATE_BODY = r"""
var st=window['__evHandsWatch'];if(!st)return {armed:false,clicks:0,log:[],checks:0};
return {armed:!!st.armed,clicks:st.clicks,log:st.log,checks:st.checks};
"""

WATCH_STOP_BODY = r"""var st=window['__evHandsWatch'];if(st&&st.stop)st.stop();return {armed:false};"""


class Watcher:
    def __init__(self, wid: str, spec: dict[str, Any], now: float, ttl: float):
        self.id = wid
        self.spec = spec
        self.kind = spec["kind"]
        self.created = now
        self.expires = now + ttl
        self.state = "active"
        self.next_at = now
        self.actions = 0
        self.busy_s = 0.0
        self.interval = 5.0
        self.mem: dict[str, Any] = {}
        self.note = ""

    def to_dict(self, now: float) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "spec": self.spec, "state": self.state,
                "created_at": self.created, "expires_at": self.expires,
                "expires_in_s": max(0, int(self.expires - now)), "actions": self.actions,
                "busy_s": round(self.busy_s, 3), "note": self.note}


class WatcherManager:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.rt = ctx.rt
        self.watchers: dict[str, Watcher] = {}
        self.events: list[dict[str, Any]] = []
        self.sink: Callable[[dict[str, Any]], None] | None = None
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- settings -------------------------------------------------------------------
    @staticmethod
    def _limits() -> dict[str, float]:
        try:
            from app.config import settings

            return {"max": settings.hands_watcher_max, "ttl": settings.hands_watcher_max_ttl_s,
                    "poll_min": settings.hands_watcher_poll_min_s, "poll_max": settings.hands_watcher_poll_max_s}
        except Exception:
            return {"max": 8, "ttl": 14400.0, "poll_min": 0.4, "poll_max": 5.0}

    # -- create / list / cancel ----------------------------------------------------------
    def create(self, spec: dict[str, Any], *, provenance: str = "owner", approved: bool = False) -> dict[str, Any]:
        from app.fabric import policy

        if not isinstance(spec, dict) or spec.get("kind") not in KINDS:
            return R.fail("invalid_args", f"A watcher needs a kind: one of {', '.join(KINDS)}.")
        lim = self._limits()
        with self._lock:
            live = [w for w in self.watchers.values() if w.state == "active"]
            if len(live) >= lim["max"]:
                return R.fail("precondition_failed", f"I'm already running {len(live)} watchers; cancel one first.")
        verdict = policy.decide("write", "hands:watcher", provenance)
        if verdict == "CONFIRM" and not approved:
            return R.receipt(False, needs_confirm=True, provenance=provenance, skill="watcher.create",
                             err=R.error("confirm_required", "Starting a watcher from this source needs your confirmation.", retryable=True),
                             confirm={"title": f"Start a {spec['kind']} watcher", "rows": [{"label": json.dumps(spec)[:160]}],
                                      "risk": "confirm", "physical": False,
                                      "plan_id": registry.plan_id("watcher.create", spec, None), "expires_s": 300},
                             say="I need your confirmation to start that watcher.")
        if verdict == "DENY":
            return R.fail("denied", "Policy denies that watcher.")
        problems = self._validate(spec)
        if problems:
            return R.fail("invalid_args", problems)
        ttl = min(float(spec.get("expires_in_s") or 3600), lim["ttl"])
        now = self.rt.now()
        w = Watcher(f"w-{uuid.uuid4().hex[:8]}", dict(spec), now, ttl)
        w.interval = lim["poll_max"] if spec["kind"] != "skip_ads" else 10.0
        action = spec.get("action") or {}
        if action.get("skill"):
            sk = registry.REGISTRY.get(action["skill"])
            if sk is None:
                return R.fail("not_found", f"There is no skill {action['skill']!r}.")
            if sk.risk not in ("read", "low"):
                w.note = "its action needs your confirmation each time, so it will ask instead of acting"
        with self._lock:
            self.watchers[w.id] = w
        self._persist()
        first = self._run_one(w)  # arm immediately
        return R.receipt(True, verified=bool(first.get("armed", True)), skill="watcher.create", provenance=provenance,
                         evidence={"watcher": w.to_dict(now), "armed": first},
                         say=f"Watching: {self._describe(w)} for {self._fmt_dur(ttl)}.",
                         undo={"skill": "watcher.cancel", "args": {"watcher_id": w.id},
                               "description": "stop that watcher"})

    @staticmethod
    def _fmt_dur(s: float) -> str:
        return f"{int(s // 3600)} h" if s >= 3600 and s % 3600 == 0 else (f"{int(s // 60)} min" if s >= 60 else f"{int(s)} s")

    @staticmethod
    def _describe(w: Watcher) -> str:
        sc = w.spec.get("scope") or {}
        if w.kind == "skip_ads":
            return "skipping ads" + (f" on {sc['tab_url_contains']}" if sc.get("tab_url_contains") else " in your browsers")
        if w.kind == "file_done":
            return f"waiting for {Path(str(w.spec.get('path', ''))).name} to finish writing"
        if w.kind == "process_exit":
            return f"waiting for {w.spec.get('name') or w.spec.get('pid')} to exit"
        return w.kind.replace("_", " ")

    def _validate(self, spec: dict[str, Any]) -> str | None:
        k = spec["kind"]
        if k == "file_done" and not spec.get("path"):
            return "file_done needs a path."
        if k == "process_exit" and not (spec.get("pid") or spec.get("name")):
            return "process_exit needs a pid or name."
        if k == "custom" and not (spec.get("condition") and spec.get("action", {}).get("skill")):
            return "custom needs a condition and an action.skill."
        return None

    def list(self, include_finished: bool = False) -> dict[str, Any]:
        now = self.rt.now()
        rows = [w.to_dict(now) for w in self.watchers.values() if include_finished or w.state == "active"]
        return R.receipt(True, verified=True, skill="watcher.list", evidence={"watchers": rows, "n": len(rows)},
                         say=("No watchers are running." if not rows else
                              "Running: " + "; ".join(self._describe(self.watchers[r["id"]]) for r in rows) + "."))

    def cancel(self, watcher_id: str) -> dict[str, Any]:
        w = self.watchers.get(watcher_id)
        if w is None:
            return R.fail("not_found", "I don't have a watcher with that id.")
        if w.kind == "skip_ads" and w.state == "active":
            with contextlib.suppress(Exception):
                self._skip_ads(w)  # collect what it did since the last look before it goes away
        self._teardown(w)
        w.state = "cancelled"
        tail = f" It skipped {w.actions} ad{'s' if w.actions != 1 else ''}." if w.kind == "skip_ads" and w.actions else ""
        self._emit(w, "cancelled", say=f"Stopped {self._describe(w)}.{tail}")
        self._persist()
        gone = w.state == "cancelled"
        return R.receipt(gone, verified=gone, skill="watcher.cancel", evidence={"id": watcher_id, "actions": w.actions},
                         say=f"Stopped {self._describe(w)}.{tail}")

    def cancel_all(self) -> dict[str, Any]:
        n = 0
        for w in list(self.watchers.values()):
            if w.state == "active":
                self.cancel(w.id)
                n += 1
        return R.receipt(True, verified=True, skill="watcher.cancel", evidence={"cancelled": n},
                         say=f"Stopped {n} watcher{'s' if n != 1 else ''}.")

    def get_events(self, since: float | None = None, watcher_id: str | None = None) -> dict[str, Any]:
        rows = [e for e in self.events if (since is None or e["t"] > since) and (not watcher_id or e["watcher_id"] == watcher_id)]
        return R.receipt(True, verified=True, skill="watcher.events", evidence={"events": rows, "n": len(rows)})

    # -- running ---------------------------------------------------------------------------------
    def tick(self) -> int:
        """Run every due watcher once. Returns how many ran."""
        n = 0
        now = self.rt.now()
        for w in list(self.watchers.values()):
            if w.state != "active":
                continue
            if now >= w.expires:
                self._teardown(w)
                w.state = "expired"
                self._emit(w, "expired", say=f"Stopped {self._describe(w)}: time's up.")
                self._persist()
                continue
            if now >= w.next_at:
                t0 = self.rt.monotonic()
                try:
                    self._run_one(w)
                except Exception as exc:
                    self._emit(w, "error", say=f"A watcher hit a problem: {type(exc).__name__}.")
                    w.next_at = now + 30
                w.busy_s += max(0.0, self.rt.monotonic() - t0)
                n += 1
        return n

    def next_due(self) -> float | None:
        due = [w.next_at for w in self.watchers.values() if w.state == "active"]
        return min(due) if due else None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                self.tick()
                due = self.next_due()
                wait = 1.0 if due is None else max(0.2, min(5.0, due - self.rt.now()))
                self._stop.wait(wait)

        self._thread = threading.Thread(target=loop, name="hands-watchers", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for w in list(self.watchers.values()):
            if w.state == "active":
                self._teardown(w)

    def _run_one(self, w: Watcher) -> dict[str, Any]:
        lim = self._limits()
        fn = {"skip_ads": self._skip_ads, "file_done": self._file_done, "process_exit": self._process_exit,
              "tab_state": self._tab_state, "custom": self._custom}[w.kind]
        out = fn(w)
        w.next_at = self.rt.now() + max(lim["poll_min"], float(out.get("next_s", w.interval)))
        return out

    # -- kinds ------------------------------------------------------------------------------------
    def _scope_tabs(self, w: Watcher):
        sc = w.spec.get("scope") or {}
        for b in browsers_of(self.ctx):
            try:
                if not b.running():
                    continue
                for t in b.tabs(window_ids=(self.ctx.window_ids or {}).get(b.kind)):
                    if sc.get("tab_url_contains") and sc["tab_url_contains"].lower() not in t.url.lower():
                        continue
                    yield b, t
            except BrowserError:
                continue

    def _skip_ads(self, w: Watcher) -> dict[str, Any]:
        from .packs.web_media import PRELUDE

        limit = int(w.spec.get("max_actions") or 200)
        seen = w.mem.setdefault("armed_tabs", {})
        counted = w.mem.setdefault("counted", {})
        active_ad = False
        armed = 0
        for b, t in self._scope_tabs(w):
            key = f"{t.browser}:{t.window_id}:{t.tab_id}"
            try:
                st = b.js_json(t, WATCH_STATE_BODY, timeout=8)
                clicks = int(st.get("clicks", 0))
                known = counted.get(key, 0) if clicks >= counted.get(key, 0) else 0  # page reloaded: counter restarted
                if clicks > known:
                    new = clicks - known
                    w.actions += new
                    self._emit(w, "fired", say="Skipped an ad." if new == 1 else f"Skipped {new} ads.",
                               receipt={"tab": t.to_dict(), "clicks": new, "log": st.get("log", [])[-new:]})
                    active_ad = True
                counted[key] = clicks
                if w.actions >= limit:
                    self._teardown(w)
                    w.state = "finished"
                    self._emit(w, "expired", say="Reached the limit of ads to skip, so I stopped.")
                    self._persist()
                    return {"armed": False, "next_s": w.interval}
                if not st.get("armed"):
                    body = (WATCH_SKIP_BODY.replace("__PRELUDE__", PRELUDE)
                            .replace("__MAX_MS__", str(int(max(1.0, w.expires - self.rt.now()) * 1000)))
                            .replace("__MAX_CLICKS__", str(max(1, limit - w.actions))))
                    b.js_json(t, body, timeout=10)
                    seen[key] = self.rt.now()
                    counted[key] = 0
                armed += 1
            except BrowserError as e:
                if e.code in ("js_disabled", "permission_missing"):
                    self._emit(w, "error", say=f"{e.message}. {e.fix or ''}".strip())
                    w.state = "error"
                    return {"armed": False, "next_s": 60}
        return {"armed": armed > 0, "tabs": armed, "next_s": 2.0 if active_ad else w.interval}

    def _file_done(self, w: Watcher) -> dict[str, Any]:
        p = Path(str(w.spec["path"])).expanduser()
        quiet = float(w.spec.get("quiet_s") or 3.0)
        size = p.stat().st_size if p.exists() else None
        last = w.mem.get("size")
        now = self.rt.now()
        if size is not None and size == last and size >= int(w.spec.get("min_bytes") or 1):
            if now - w.mem.get("since", now) >= quiet:
                self._finish(w, f"{p.name} finished writing.", {"path": str(p), "bytes": size})
                return {"done": True}
        else:
            w.mem["since"] = now
        w.mem["size"] = size
        return {"size": size, "next_s": 1.0 if size is not None else w.interval}

    def _process_exit(self, w: Watcher) -> dict[str, Any]:
        ps = self.rt.run(["ps", "-axo", "pid=,comm="], timeout=8).out or ""
        alive = False
        for line in ps.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and ((w.spec.get("pid") and parts[0] == str(w.spec["pid"]))
                                    or (w.spec.get("name") and parts[1].rsplit("/", 1)[-1] == w.spec["name"])):
                alive = True
                break
        if w.mem.get("seen") and not alive:
            self._finish(w, f"{w.spec.get('name') or w.spec.get('pid')} has exited.", {})
            return {"done": True}
        if alive:
            w.mem["seen"] = True
        elif not w.mem.get("seen") and self.rt.now() - w.created > 30:
            w.state = "finished"
            self._emit(w, "error", say="That process was never running, so there was nothing to wait for.")
        return {"alive": alive, "next_s": 2.0}

    def _tab_state(self, w: Watcher) -> dict[str, Any]:
        want = w.spec.get("condition") or {}
        for _, t in self._scope_tabs(w):
            if want.get("title_contains") and want["title_contains"].lower() in t.title.lower():
                self._finish(w, f"“{t.title[:50]}” matched.", {"tab": t.to_dict()})
                return {"done": True}
            if want.get("url_contains") and want["url_contains"].lower() in t.url.lower():
                self._finish(w, "That page is open.", {"tab": t.to_dict()})
                return {"done": True}
        return {"next_s": w.interval}

    def _custom(self, w: Watcher) -> dict[str, Any]:
        from .ui import UI

        ok, ev, _ = UI(self.ctx)._check(w.spec["condition"])
        if ok:
            self._finish(w, "The condition you asked me to watch for is true.", ev)
            return {"done": True}
        return {"next_s": w.interval}

    def _finish(self, w: Watcher, say: str, evidence: dict[str, Any]) -> None:
        action = w.spec.get("action") or {}
        receipt: dict[str, Any] | None = None
        if action.get("skill"):
            sk = registry.REGISTRY.get(action["skill"])
            if sk and sk.risk in ("read", "low"):
                receipt = registry.run(action["skill"], self.ctx, action.get("args") or {})
                say = say + (" " + receipt["say"] if receipt.get("say") else "")
            else:
                self._emit(w, "needs_confirm", say=say + f" Do you want me to run {action['skill']}?",
                           receipt={"action": action})
                w.state = "finished"
                self._persist()
                return
        w.actions += 1
        w.state = "finished"
        self._emit(w, "fired", say=say, receipt={"evidence": evidence, "action": receipt})
        self._persist()

    def _teardown(self, w: Watcher) -> None:
        if w.kind != "skip_ads":
            return
        for b, t in self._scope_tabs(w):
            try:
                b.js_json(t, WATCH_STOP_BODY, timeout=6)
            except BrowserError:
                continue

    # -- events / persistence ---------------------------------------------------------------------
    def _emit(self, w: Watcher, kind: str, *, say: str | None = None, receipt: dict[str, Any] | None = None) -> None:
        ev = {"id": f"e-{uuid.uuid4().hex[:8]}", "watcher_id": w.id, "t": self.rt.now(), "type": kind,
              "say": say, "receipt": receipt}
        self.events.append(ev)
        del self.events[:-500]
        if self.sink:
            try:
                self.sink(ev)
            except Exception:
                pass

    def _persist(self) -> None:
        try:
            path = state_dir(self.ctx) / "watchers.json"
            path.write_text(json.dumps({"watchers": [w.to_dict(self.rt.now()) for w in self.watchers.values()
                                                     if w.state == "active"]}))
        except OSError:
            pass
