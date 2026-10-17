"""ev.tools.dispatch backends for the 8 fabric tools.

Single implementation shared by direct dispatch callers (routines,
``/v1/tools/execute``) and by the kernel (semantic tools route here via
``executor._backed_existing``). Every effect passes the fabric policy
engine first (DENY fails closed, CONFIRM parks an ``ApprovedAction``
ticket through the existing confirm machinery, AUTO/AUTO_JOURNAL run),
and every lane call emits a fabric span.

Model-controlled values are never trusted for containment: lane roots
come from D1 + settings (never from arguments), and approval is the
dispatch-level ``approved`` flag (a caller confirmation ``authorize``
accepted — i.e. an ApprovedAction resume), never a boolean argument.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from . import trace
from .facade import fabric_enabled
from .policy import decide
from .roots import lane_root

FABRIC_TOOL_NAMES = frozenset({
    "files_op",
    "shell_run",
    "shell_session",
    "terminal_layout",
    "screen_ref",
    "sql_query",
    "pim_act",
    "builder_job",
    "atlas_discover",
})

_UNAVAILABLE = {"ok": False, "verified": True, "evidence": {},
                "error": "CAPABILITY_UNAVAILABLE", "undo": None}


def _provenance(actor: str) -> str:
    return "scheduled" if (actor or "") == "automation" else "owner_turn"


def _deny(reason: str) -> dict[str, Any]:
    return {"ok": False, "verified": True, "evidence": {},
            "error": reason, "undo": None,
            "spoken": f"I can't do that: {reason}."}


async def _park(session: AsyncSession, *, name: str, args: dict, actor: str,
                device_id, live_session_id: str | None, request_id: str | None,
                target: str, spoken: str) -> dict[str, Any]:
    from app.ev.policy import PolicyDecision
    from app.ev.tools import _park_independent_hold

    decision = PolicyDecision(
        allowed=False, effect="confirm", reason="fabric policy: confirmation required",
        risk_class="R2", confirmation_ttl_seconds=120, target=target or name,
        spoken=spoken,
    )
    return await _park_independent_hold(
        session, name=name, arguments=dict(args), decision=decision, actor=actor,
        device_id=device_id, live_session_id=live_session_id, request_id=request_id,
        channel="voice",
    )


def _prepare_only_receipt(name: str) -> dict[str, Any]:
    return {"ok": True, "verified": True, "prepare_only": True,
            "evidence": {"tool": name}, "error": None, "undo": None,
            "spoken": "Prepare-only: I will not run that effect."}


async def run_fabric_tool(
    session: AsyncSession,
    name: str,
    args: dict[str, Any],
    *,
    actor: str = "master",
    device_id=None,
    live_session_id: str | None = None,
    request_id: str | None = None,
    approved: bool = False,
) -> dict[str, Any]:
    """Run one fabric dispatch tool. Called from ``ev.tools._handle``."""
    args = dict(args or {})
    if name not in FABRIC_TOOL_NAMES:
        return dict(_UNAVAILABLE)
    if not fabric_enabled():
        return dict(_UNAVAILABLE)
    if args.get("prepare_only"):
        return _prepare_only_receipt(name)
    handler = _HANDLERS[name]
    with trace.span(handler.lane, handler.op(args)):
        return await handler.run(
            session, args, actor=actor, device_id=device_id,
            live_session_id=live_session_id, request_id=request_id,
            approved=approved,
        )


class _Handler:
    lane = ""

    def op(self, args: dict) -> str:  # span label
        return str(args.get("op") or "run")

    async def run(self, session, args, **ctx) -> dict:
        raise NotImplementedError


def _panicked() -> bool:
    from .panic import panicked

    return panicked()


def _gate_mutation(*, action: str, target: str, actor: str,
                   approved: bool) -> str:
    """AUTO / AUTO_JOURNAL / CONFIRM / DENY / PANIC for one effect."""
    if _panicked():
        return "PANIC"
    return decide(action, target, _provenance(actor))


class _ShellRun(_Handler):
    lane = "shell"

    def op(self, args):
        return "run"

    async def run(self, session, args, **ctx):
        from .shell_sessions import ShellSessions, classify_command

        cmd = str(args.get("command") or "")
        cls = classify_command(cmd)
        if cls == "AUTO":
            # AUTO reads stay available even under panic; effects do not.
            gate = "AUTO"
        else:
            action = {"AUTO_JOURNAL": "write", "CONFIRM": "run", "DENY": "run"}[cls]
            gate = _gate_mutation(action=action, target=cmd, actor=ctx["actor"],
                                  approved=ctx["approved"])
            if cls == "DENY":
                gate = "DENY"
        if gate == "PANIC":
            return _deny("denied: owner panic switch is engaged")
        if gate == "DENY":
            return _deny("denied by policy")
        if gate == "CONFIRM" and not ctx["approved"]:
            return await _park(session, name="shell_run", args=args, actor=ctx["actor"],
                                device_id=ctx["device_id"],
                                live_session_id=ctx["live_session_id"],
                                request_id=ctx["request_id"], target=cmd[:200],
                                spoken=f"Confirm running: {cmd[:160]}")
        root = lane_root()
        res = ShellSessions(root=root).run(
            cmd, timeout=float(args.get("timeout_s") or 30),
            confirm=(gate == "CONFIRM"), provenance=_provenance(ctx["actor"]),
            cwd=args.get("cwd") or str(root),
        )
        res["spoken"] = _speak_shell(cmd, res)
        return res


def _speak_shell(cmd: str, res: dict) -> str:
    if not res.get("ok"):
        return f"That command failed: {str(res.get('error') or '')[:200]}"
    ev = res.get("evidence", {})
    out = str(ev.get("out") or "").strip().splitlines()
    first = out[0][:200] if out else f"exit {ev.get('exit_code', 0)}"
    return f"Ran {cmd[:80]}: {first}"[:500]


class _ShellSession(_Handler):
    lane = "shell"

    async def run(self, session, args, **ctx):
        from .shell_sessions import ShellSessions, classify_command

        op = str(args.get("op") or "")
        root = lane_root()
        if op in {"stream", "tail", "list", "exit_code", "reattach"}:
            res = self._read(ShellSessions(root=root), op, args)
            res["spoken"] = "Here is the session output." if res.get("ok") else str(res.get("error") or "")
            return res
        if op == "cancel":
            # Teardown is always allowed (the stop path); panic also wants stops.
            res = ShellSessions(root=root).cancel(str(args.get("session_id") or ""))
            res["spoken"] = "Session stopped." if res.get("ok") else str(res.get("error") or "")
            return res
        if op == "start":
            cmd = str(args.get("cmd") or "")
            cls = classify_command(cmd)
            if cls == "DENY":
                return _deny("denied by policy")
            if cls != "AUTO":
                gate = _gate_mutation(action="run", target=cmd, actor=ctx["actor"],
                                      approved=ctx["approved"])
                if gate == "PANIC":
                    return _deny("denied: owner panic switch is engaged")
                if gate == "CONFIRM" and not ctx["approved"]:
                    return await _park(session, name="shell_session", args=args,
                                        actor=ctx["actor"], device_id=ctx["device_id"],
                                        live_session_id=ctx["live_session_id"],
                                        request_id=ctx["request_id"], target=cmd[:200],
                                        spoken=f"Confirm starting: {cmd[:160]}")
                lane_confirm = True
            else:
                # AUTO stays available under panic (reads only).
                lane_confirm = False
            res = ShellSessions(root=root).start(
                cmd, cwd=args.get("cwd") or str(root), confirm=lane_confirm,
                provenance=_provenance(ctx["actor"]))
            res["spoken"] = "Session started." if res.get("ok") else str(res.get("error") or "")
            return res
        if op == "send_input":
            gate = _gate_mutation(action="run", target="session-input",
                                  actor=ctx["actor"], approved=ctx["approved"])
            if gate == "PANIC":
                return _deny("denied: owner panic switch is engaged")
            if not ctx["approved"]:
                return await _park(session, name="shell_session", args=args,
                                    actor=ctx["actor"], device_id=ctx["device_id"],
                                    live_session_id=ctx["live_session_id"],
                                    request_id=ctx["request_id"], target="session-input",
                                    spoken="Confirm sending input to that session.")
            res = ShellSessions(root=root).send_input(
                str(args.get("session_id") or ""), str(args.get("data") or ""))
            res["spoken"] = "Input sent." if res.get("ok") else str(res.get("error") or "")
            return res
        return _deny(f"unknown shell_session op '{op}'")

    @staticmethod
    def _read(sessions, op: str, args: dict) -> dict:
        sid = str(args.get("session_id") or "")
        if op == "stream":
            return sessions.stream(sid)
        if op == "tail":
            return sessions.tail(sid, int(args.get("n") or 50))
        if op == "list":
            return sessions.list()
        if op == "exit_code":
            return sessions.exit_code(sid)
        return sessions.reattach(sid)


class _TerminalLayout(_Handler):
    lane = "terminal"

    async def run(self, session, args, **ctx):
        from . import terminal
        from .shell_sessions import classify_command

        op = str(args.get("op") or "")
        root = lane_root()
        if op == "plan":
            return terminal.TmuxLayout(root=root).plan(list(args.get("panes") or []))
        if op == "list":
            return terminal.list_layouts(root=root)
        if op == "save":
            res = terminal.save_layout(str(args.get("name") or ""),
                                       {"panes": list(args.get("panes") or [])}, root=root)
            res["spoken"] = "Layout saved." if res.get("ok") else str(res.get("error") or "")
            return res
        if op == "read":
            layout = terminal.TmuxLayout(root=root)
            layout.session = str(args.get("session") or "") or None
            return layout.read_panes()
        if op == "close":
            layout = terminal.TmuxLayout(root=root)
            layout.session = str(args.get("session") or "") or None
            res = layout.close()
            res["spoken"] = "Layout closed." if res.get("ok") else str(res.get("error") or "")
            return res
        if op in {"open", "restore"}:
            if op == "restore":
                loaded = terminal.load_layout(str(args.get("name") or ""), root=root)
                if not loaded.get("ok"):
                    return loaded
                panes = loaded["spec"]["panes"]
            else:
                panes = list(args.get("panes") or [])
            classes = [classify_command(str(p.get("command") or "")) for p in panes
                        if str(p.get("command") or "").strip()]
            if "DENY" in classes:
                return _deny("denied by policy: a pane command is denied")
            if "CONFIRM" in classes or "AUTO_JOURNAL" in classes:
                gate = _gate_mutation(action="run", target="terminal-layout",
                                      actor=ctx["actor"], approved=ctx["approved"])
                if gate == "PANIC":
                    return _deny("denied: owner panic switch is engaged")
                if not ctx["approved"]:
                    return await _park(session, name="terminal_layout", args=args,
                                        actor=ctx["actor"], device_id=ctx["device_id"],
                                        live_session_id=ctx["live_session_id"],
                                        request_id=ctx["request_id"], target="terminal-layout",
                                        spoken="Confirm opening that terminal layout.")
            elif _panicked():
                return _deny("denied: owner panic switch is engaged")
            res = terminal.TmuxLayout(root=root).open(panes, confirm=True)
            res["spoken"] = "Layout opened." if res.get("ok") else str(res.get("error") or "")
            return res
        return _deny(f"unknown terminal_layout op '{op}'")


def _screenshot_dir() -> Path:
    import subprocess

    try:
        p = subprocess.run(["defaults", "read", "com.apple.screencapture", "location"],
                           capture_output=True, text=True, timeout=5)
        loc = (p.stdout or "").strip()
        if loc:
            return Path(loc).expanduser()
    except Exception:
        pass
    return Path.home() / "Desktop"


class _ScreenRef(_Handler):
    lane = "screen"

    async def run(self, session, args, **ctx):
        from . import screen_ref

        del session, ctx
        op = str(args.get("op") or "")
        if op == "resolve_last":
            directory = args.get("directory") or str(_screenshot_dir())
            found = screen_ref.newest_screenshot(directory)
            if found is None:
                return {"ok": False, "verified": True, "evidence": {},
                        "error": "no screenshot found", "undo": None,
                        "spoken": "I don't see a screenshot to work from."}
            return {"ok": True, "verified": True, "evidence": found, "error": None,
                    "undo": None, "spoken": "Found the latest screenshot."}
        if op == "capture":
            res = screen_ref.capture_region(dict(args.get("region") or {"x": 0, "y": 0, "w": 4, "h": 4}))
            res["evidence"]["fixture"] = True
            res["spoken"] = "Captured a test-fixture region (real capture lands in WP5)."
            return res
        if op == "infer":
            panes = screen_ref.infer_panes(list(args.get("ocr") or []))
            return {"ok": True, "verified": True,
                    "evidence": {"panes": panes, "n": len(panes)}, "error": None,
                    "undo": None, "spoken": f"Inferred {len(panes)} panes."}
        return _deny(f"unknown screen_ref op '{op}'")


class _SqlQuery(_Handler):
    lane = "sql"

    def op(self, args):
        return "query"

    async def run(self, session, args, **ctx):
        from . import sql_lane

        sql = str(args.get("sql") or "")
        db_path = str(args.get("db_path") or ":memory:")
        if db_path.strip().lower() in {"prod", "production"}:
            return _deny("prod DB DENY: writes to production are not allowed")
        if db_path != ":memory:":
            allowed = self._scratch_ok(db_path)
            if not allowed:
                gate = _gate_mutation(action="outside-root", target=db_path,
                                      actor=ctx["actor"], approved=ctx["approved"])
                if gate == "PANIC":
                    return _deny("denied: owner panic switch is engaged")
                if not ctx["approved"]:
                    return await _park(session, name="sql_query", args=args,
                                        actor=ctx["actor"], device_id=ctx["device_id"],
                                        live_session_id=ctx["live_session_id"],
                                        request_id=ctx["request_id"], target=db_path[:200],
                                        spoken="Confirm using that database file.")
        kind = sql_lane.classify(sql)
        if kind == "read":
            res = sql_lane.execute(sql, db_path=db_path, root=lane_root())
            res["spoken"] = "Query ran." if res.get("ok") else str(res.get("error") or "")
            return res
        if args.get("preview"):
            res = sql_lane.execute(sql, db_path=db_path, root=lane_root(), preview=True)
            res["spoken"] = "Preview only — nothing was committed."
            return res
        gate = _gate_mutation(action="apply" if kind == "dml" else "exec",
                              target=sql[:200], actor=ctx["actor"],
                              approved=ctx["approved"])
        if gate == "PANIC":
            return _deny("denied: owner panic switch is engaged")
        if gate == "DENY":
            return _deny("denied by policy")
        if not ctx["approved"]:
            return await _park(session, name="sql_query", args=args, actor=ctx["actor"],
                                device_id=ctx["device_id"],
                                live_session_id=ctx["live_session_id"],
                                request_id=ctx["request_id"], target=sql[:200],
                                spoken="Confirm committing that database change.")
        res = sql_lane.execute(sql, db_path=db_path, root=lane_root(), confirm=True)
        res["spoken"] = "Change committed." if res.get("ok") else str(res.get("error") or "")
        return res

    @staticmethod
    def _scratch_ok(db_path: str) -> bool:
        try:
            candidate = Path(db_path).expanduser().resolve()
        except OSError:
            return False
        try:
            tmp = Path(tempfile.gettempdir()).resolve()
            if tmp in candidate.parents or candidate == tmp:
                return True
        except OSError:
            pass
        root = lane_root()
        return root in candidate.parents or candidate == root


class _PimAct(_Handler):
    lane = "pim"

    async def run(self, session, args, **ctx):
        from .bridge import Bridge

        del session
        op = str(args.get("op") or "")
        bridge = Bridge(root=lane_root())
        if op == "reminder_list":
            res = bridge.reminder_list(args.get("list_name"))
            res["spoken"] = f"{res['evidence'].get('n', 0)} reminders."
            return res
        if op in {"reminder_create", "reminder_complete", "reminder_delete"}:
            if _panicked():
                return _deny("denied: owner panic switch is engaged")
            if op == "reminder_create":
                res = bridge.reminder_create(str(args.get("title") or ""),
                                             str(args.get("list_name") or "default"),
                                             str(args.get("notes") or ""))
            elif op == "reminder_complete":
                res = bridge.reminder_complete(str(args.get("id") or ""))
            else:
                res = bridge.reminder_delete(str(args.get("id") or ""))
            res["spoken"] = ("Done." if res.get("ok") else str(res.get("error") or ""))
            return res
        return _deny(f"unknown pim_act op '{op}'")


class _BuilderJob(_Handler):
    lane = "builder"

    async def run(self, session, args, **ctx):
        from . import builder

        op = str(args.get("op") or "")
        root = lane_root()
        if op == "plan":
            project_dir = str(args.get("project_dir") or "")
            if project_dir and not self._inside_root(project_dir, root):
                gate = _gate_mutation(action="outside-root", target=project_dir,
                                      actor=ctx["actor"], approved=ctx["approved"])
                if gate == "PANIC":
                    return _deny("denied: owner panic switch is engaged")
                if not ctx["approved"]:
                    return await _park(session, name="builder_job", args=args,
                                        actor=ctx["actor"], device_id=ctx["device_id"],
                                        live_session_id=ctx["live_session_id"],
                                        request_id=ctx["request_id"],
                                        target=project_dir[:200],
                                        spoken="Confirm building outside the workspace.")
            res = builder.build_plan(str(args.get("goal") or ""), root=root,
                                     project_dir=project_dir,
                                     steps=args.get("steps"))
            res["spoken"] = "Plan ready." if res.get("ok") else str(res.get("error") or "")
            return res
        if op == "apply":
            gate = _gate_mutation(action="apply", target="builder-plan",
                                  actor=ctx["actor"], approved=ctx["approved"])
            if gate == "PANIC":
                return _deny("denied: owner panic switch is engaged")
            if not ctx["approved"]:
                return await _park(session, name="builder_job", args=args,
                                    actor=ctx["actor"], device_id=ctx["device_id"],
                                    live_session_id=ctx["live_session_id"],
                                    request_id=ctx["request_id"], target="builder-plan",
                                    spoken="Confirm running that build plan.")
            res = builder.apply_plan({"plan": args.get("plan") or {},
                                      "job_id": args.get("job_id")}, root=root)
            res["spoken"] = ("Build finished." if res.get("ok")
                             else str(res.get("error") or "Build failed."))
            return res
        if op in {"resume", "status"}:
            res = builder.resume_job(str(args.get("job_id") or ""), root=root)
            res["spoken"] = str(res.get("evidence", {}).get("state") or res.get("error") or "")
            return res
        return _deny(f"unknown builder_job op '{op}'")

    @staticmethod
    def _inside_root(project_dir: str, root: Path) -> bool:
        try:
            candidate = (root / project_dir).resolve() if not Path(project_dir).is_absolute() \
                else Path(project_dir).resolve()
        except OSError:
            return False
        return candidate == root or root in candidate.parents


class _FilesOp(_Handler):
    lane = "files"

    async def run(self, session, args, **ctx):
        from . import files as lane

        op = str(args.get("op") or "")
        root = lane_root()
        approved = bool(ctx["approved"])
        if op in {"read", "list"}:
            if op == "list":
                res = lane.list_files(root=root)
            else:
                res = lane.read_file(str(args.get("path") or ""), root=root,
                                     confirm=approved)
            if res.get("confirm") and not approved:
                return await _park(session, name="files_op", args=args,
                                    actor=ctx["actor"], device_id=ctx["device_id"],
                                    live_session_id=ctx["live_session_id"],
                                    request_id=ctx["request_id"],
                                    target=str(args.get("path") or op)[:200],
                                    spoken="Confirm reading outside the workspace.")
            res["spoken"] = "Here it is." if res.get("ok") else str(res.get("error") or "")
            return res
        if op not in {"create", "overwrite", "delete", "bulk", "undo"}:
            return _deny(f"unknown files_op op '{op}'")
        if _panicked():
            return _deny("denied: owner panic switch is engaged")
        if op == "create":
            res = lane.create(str(args.get("path") or ""),
                              args.get("content", ""), root=root, confirm=approved)
        elif op == "overwrite":
            res = lane.overwrite(str(args.get("path") or ""),
                                 args.get("content", ""), root=root, confirm=approved)
        elif op == "delete":
            res = lane.delete(str(args.get("path") or ""), root=root, confirm=approved)
        elif op == "bulk":
            res = lane.bulk(list(args.get("paths") or []),
                            op=str(args.get("sub") or "delete"),
                            root=root, confirm=approved,
                            content=args.get("content", ""))
        else:
            res = lane.undo(dict(args.get("undo") or {}), root=root)
        if res.get("confirm") and not approved:
            return await _park(session, name="files_op", args=args,
                                actor=ctx["actor"], device_id=ctx["device_id"],
                                live_session_id=ctx["live_session_id"],
                                request_id=ctx["request_id"],
                                target=str(args.get("path") or op)[:200],
                                spoken="Confirm that file change.")
        if res.get("ok"):
            res["spoken"] = {
                "create": "Saved.", "overwrite": "Saved.", "delete": "Moved to Trash.",
                "bulk": "Bulk change done.", "undo": "Undone.",
            }.get(op, "Done.")
        else:
            res["spoken"] = str(res.get("error") or "")
        return res


class _AtlasDiscover(_Handler):
    lane = "atlas"

    def op(self, args):
        return "discover"

    async def run(self, session, args, **ctx):
        from .gaps import closest_alternative, record_gap

        del session, ctx
        domain = str(args.get("domain") or args.get("app") or "").strip()
        descriptors: list[dict[str, Any]] = []
        try:
            from app.digital.fabric import all_descriptors

            needle = domain.lower()
            for item in all_descriptors():
                public = item.as_public()
                hay = f"{public.get('service', '')} {public.get('operation', '')}".lower()
                if not needle or needle in hay:
                    descriptors.append({
                        "name": f"{public.get('service')}.{public.get('operation')}",
                        "availability": public.get("availability"),
                        "risk": public.get("risk"),
                    })
        except Exception:
            descriptors = []
        if descriptors:
            return {"ok": True, "verified": True,
                    "evidence": {"descriptors": descriptors[:20], "n": len(descriptors)},
                    "error": None, "undo": None,
                    "spoken": f"Found {len(descriptors)} matching capabilities."}
        gap = record_gap(f"atlas:{domain or 'all'}")
        return {"ok": True, "verified": True,
                "evidence": {"descriptors": [],
                             "gap": gap.get("evidence", {}),
                             "alternative": closest_alternative(domain)},
                "error": None, "undo": None,
                "spoken": "No matching app capability yet; I recorded the gap."}


_HANDLERS: dict[str, _Handler] = {
    "files_op": _FilesOp(),
    "shell_run": _ShellRun(),
    "shell_session": _ShellSession(),
    "terminal_layout": _TerminalLayout(),
    "screen_ref": _ScreenRef(),
    "sql_query": _SqlQuery(),
    "pim_act": _PimAct(),
    "builder_job": _BuilderJob(),
    "atlas_discover": _AtlasDiscover(),
}
