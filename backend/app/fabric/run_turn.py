"""One-brain facade: every channel calls run_turn(); ops dispatch to lanes.

Each op is {"lane": ..., "op": ..., "args": {...}}. The turn runs at most
max_steps ops, checks the cancel token between steps, and emits ack/progress
events. Unknown lanes produce a capability-gap record, never a phantom success.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from .policy import _receipt

MAX_STEPS = 16


def _cancelled(cancel) -> bool:
    return bool(cancel is not None and getattr(cancel, "cancelled", False))


def _dispatch(lane: str, op: str, args: dict, root) -> dict:
    lane = (lane or "").lower()
    args = dict(args or {})
    args.setdefault("root", root)
    if lane == "files":
        from . import files as f

        table: dict[str, Callable[..., dict]] = {
            "create": f.create, "overwrite": f.overwrite, "delete": f.delete,
            "bulk": f.bulk, "undo": f.undo, "read": f.read_file,
            "list": f.list_files,
        }
        fn = table.get(op)
        if fn is None:
            from .gaps import record_gap
            return record_gap(f"files.{op}", root=root)
        if op == "bulk":
            return fn(args.get("paths", []), op=args.get("sub", "delete"), root=root)
        call = {k: v for k, v in args.items() if k != "root"}
        call["root"] = root
        return fn(**call)
    if lane == "shell":
        from .shell_sessions import ShellSessions

        shells = ShellSessions(root=root)
        if op == "run":
            return shells.run(args.get("cmd", ""), confirm=args.get("confirm", False),
                              provenance=args.get("provenance", "owner_turn"))
        if op == "start":
            return shells.start(args.get("cmd", ""))
        if op in ("stream", "tail", "send_input", "reattach", "list", "cancel", "exit_code"):
            fn = getattr(shells, op)
            if op == "list":
                return fn()
            if op == "send_input":
                return fn(args.get("session_id", ""), args.get("data", ""))
            if op == "tail":
                return fn(args.get("session_id", ""), args.get("n", 50))
            return fn(args.get("session_id", ""))
        from .gaps import record_gap
        return record_gap(f"shell.{op}", root=root)
    if lane == "sql":
        from . import sql_lane as q
        if op in ("query", "execute"):
            return q.execute(args.get("sql", ""), db_path=args.get("db_path", ":memory:"),
                             root=root, confirm=args.get("confirm", False),
                             preview=args.get("preview", False))
        if op == "classify":
            return {"ok": True, "verified": True, "evidence": {"kind": q.classify(args.get("sql", ""))},
                    "error": None, "undo": None}
        from .gaps import record_gap
        return record_gap(f"sql.{op}", root=root)
    if lane == "gap":
        from .gaps import record_gap
        return record_gap(args.get("desc", op), root=root)
    if lane == "grant":
        from . import grants as g
        if op == "grant":
            return g.grant(args.get("scope", ""), args.get("ttl_s", 3600))
        if op == "revoke":
            return g.revoke(args.get("scope", ""))
        if op == "check":
            return g.check_grant(args.get("scope", ""))
        from .gaps import record_gap
        return record_gap(f"grant.{op}", root=root)
    if lane == "comms":
        from . import comms as c
        if op == "draft":
            return c.draft_message(args.get("body", ""), to=args.get("to", "owner"))
        if op == "send":
            return c.send_message(args.get("body", ""), to=args.get("to", "owner"),
                                  confirmed=args.get("confirmed", False))
        if op == "resolve":
            return c.resolve_thread(args.get("query", ""), candidates=args.get("candidates"))
        from .gaps import record_gap
        return record_gap(f"comms.{op}", root=root)
    if lane == "bridge":
        from .bridge import Bridge

        br = Bridge(root=root)
        if op == "roundtrip":
            return br.roundtrip(args.get("msg", {"op": "ping"}))
        return br.run(op, args.get("script", ""), args.get("payload"))
    if lane == "screen":
        from . import screen_ref as screen

        if op == "capture":
            return screen.capture_region(args.get("region", {"x": 0, "y": 0, "w": 4, "h": 4}),
                                         screen=args.get("screen"))
        if op == "resolve_last":
            found = screen.resolve_last_screenshot(args.get("conversation"),
                                                   store=args.get("store"))
            if found is None:
                return {"ok": False, "verified": True, "evidence": {},
                        "error": "no screenshot found", "undo": None}
            return {"ok": True, "verified": True, "evidence": found, "error": None, "undo": None}
        from .gaps import record_gap
        return record_gap(f"screen.{op}", root=root)
    if lane == "builder":
        from . import builder as bld

        if op == "plan":
            return bld.build_plan(args.get("goal", ""), root=root)
        if op == "apply":
            return bld.apply_plan(args.get("plan", {}), root=root)
        if op == "resume":
            return bld.resume_job(args.get("job_id", ""), root=root)
        from .gaps import record_gap
        return record_gap(f"builder.{op}", root=root)
    from .gaps import record_gap
    r = record_gap(f"{lane}.{op}", root=root)
    r["evidence"]["unfulfilled"] = True
    try:
        from .gaps import record_central

        record_central(f"lane:{lane}.{op}")
    except Exception:
        pass
    return r


def run_turn(prompt: str, root=None, on_event: Callable | None = None,
             cancel=None, budget_ms: int | None = None,
             ops: list[dict] | None = None, max_steps: int = MAX_STEPS,
             provenance: str = "owner_turn", **kw) -> dict:
    t0 = time.time()
    timestamps = {
        "t_request": t0,
        "t_first_provider_token": None,
        "t_first_streamed_chunk_to_client": None,
        "t_first_audio_out": None,
    }
    turn_id = f"turn-{int(t0 * 1000) % 1000000:06d}"

    def emit(kind: str, **e):
        if on_event is not None:
            e.setdefault("elapsed_ms", int((time.time() - t0) * 1000))
            on_event({"kind": kind, "turn_id": turn_id, **e})

    # S18: acknowledgement is the first event, synchronously, before any work.
    emit("ack", prompt=prompt)
    if _cancelled(cancel):
        r = _receipt(ok=False, evidence={"prompt": prompt, "turn_id": turn_id},
                     error="cancelled before start")
        r["latency_ms"] = int((time.time() - t0) * 1000)
        return r
    steps: list[dict] = []
    all_ok = True
    for i, op in enumerate(ops or []):
        if _cancelled(cancel):
            steps.append({"ok": False, "verified": False, "error": "cancelled", "evidence": {}})
            all_ok = False
            emit("cancelled", at_step=i)
            break
        if i >= max_steps:
            steps.append({"ok": False, "verified": True, "error": "step budget exhausted",
                          "evidence": {"max_steps": max_steps}})
            all_ok = False
            break
        spec = op or {}
        emit("progress", step=i, lane=spec.get("lane"), op=spec.get("op"))
        try:
            res = _dispatch(spec.get("lane", ""), spec.get("op", ""),
                            spec.get("args", {}), root)
        except Exception as e:  # lane crash is a failed step, never a turn crash
            res = {"ok": False, "verified": False, "evidence": {},
                   "error": f"lane error: {e}", "undo": None}
        steps.append(res)
        if not res.get("ok") or res.get("evidence", {}).get("unfulfilled"):
            all_ok = False
            emit("step_failed", step=i, error=res.get("error") or "capability gap")
            if spec.get("stop_on_fail", True):
                break
    emit("progress", step="done", steps=len(steps))
    from .honesty import claim as claim_text
    from .stream import sentences

    spoken = claim_text({"ok": all_ok, "verified": all(o.get("verified") for o in steps) if steps else False,
                         "error": None, "evidence": {"steps": steps}})
    for piece in sentences(spoken):
        if timestamps["t_first_streamed_chunk_to_client"] is None:
            timestamps["t_first_streamed_chunk_to_client"] = time.time()
        emit("chunk", text=piece)
    r = _receipt(ok=all_ok, verified=all(o.get("verified") for o in steps) if steps else True,
                 evidence={"prompt": prompt, "turn_id": turn_id, "steps": steps,
                           "n_steps": len(steps), "provenance": provenance,
                           "spoken": spoken, "timestamps": timestamps})
    r["timestamps"] = timestamps
    r["latency_ms"] = int((time.time() - t0) * 1000)
    if not r.get("ok") or not r.get("verified"):
        import os
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            try:
                from .failures import record as record_failure
                record_failure(prompt, r, trace={"turn_id": turn_id, "n_steps": len(steps)})
            except Exception:
                pass
    return r
