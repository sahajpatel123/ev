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
    "comms_act",
    "files_op",
    "mac_act",
    "operator_act",
    "recipe_act",
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


def _effective_provenance(args: dict, actor: str) -> str:
    """Actor-derived provenance, escalatable to untrusted via explicit arg.

    Explicit provenance can only escalate TOWARD untrusted, never away:
    an untrusted caller claiming owner_turn is ignored (derived wins).
    """
    from .policy import UNTRUSTED_PROVENANCE

    explicit = str(args.get("provenance") or "").lower().strip()
    if explicit in UNTRUSTED_PROVENANCE:
        return "untrusted_content"
    return _provenance(actor)


def _deny(reason: str) -> dict[str, Any]:
    return {"ok": False, "verified": True, "evidence": {},
            "error": reason, "undo": None,
            "spoken": f"I can't do that: {reason}."}


async def _park(session: AsyncSession, *, name: str, args: dict, actor: str,
                device_id, live_session_id: str | None, request_id: str | None,
                target: str, spoken: str) -> dict[str, Any]:
    from app.ev.policy import PolicyDecision, canonical_target
    from app.ev.tools import _park_independent_hold

    # The resume path re-derives canonical_target(name, args) and fails the
    # ticket as tampered when it differs, so the parked target MUST equal
    # the canonical one whenever canonical resolves (descriptiveness lives
    # in the spoken title, not the target).
    bound = canonical_target(name, dict(args)) or target
    decision = PolicyDecision(
        allowed=False, effect="confirm", reason="fabric policy: confirmation required",
        risk_class="R2", confirmation_ttl_seconds=120, target=bound or name,
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


def _audit_target(name: str, args: dict) -> str:
    if name == "shell_run":
        return str(args.get("command") or "")
    if name == "shell_session":
        return f"{args.get('op')}:{args.get('session_id') or args.get('cmd') or ''}"
    if name == "files_op":
        paths = args.get("paths") or [args.get("path") or args.get("op")]
        return "|".join(str(p) for p in list(paths)[:10])
    if name == "sql_query":
        return str(args.get("sql") or "")[:500]
    if name == "terminal_layout":
        return f"{args.get('op')}:{args.get('name') or args.get('session') or ''}"
    if name == "screen_ref":
        return str(args.get("op") or "")
    if name == "pim_act":
        return f"{args.get('op')}:{args.get('id') or args.get('title') or ''}"
    if name == "builder_job":
        return f"{args.get('op')}:{args.get('job_id') or args.get('goal') or ''}"
    return str(args.get("domain") or args.get("app") or name)


def _audit_decision(result: dict) -> str:
    if result.get("action_id"):
        return "PARKED"
    if result.get("prepare_only"):
        return "PREPARE_ONLY"
    error = str(result.get("error") or "")
    if error == "CAPABILITY_UNAVAILABLE":
        return "UNAVAILABLE"
    if error.startswith("denied"):
        return "DENY"
    return "EXECUTED"


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
    from .audit import record as audit_record

    args = dict(args or {})
    if name not in FABRIC_TOOL_NAMES:
        return dict(_UNAVAILABLE)
    if not fabric_enabled():
        audit_record("dispatch", name, _audit_target(name, args), "UNAVAILABLE", False)
        return dict(_UNAVAILABLE)
    if args.get("prepare_only"):
        audit_record("dispatch", name, _audit_target(name, args), "PREPARE_ONLY", True)
        return _prepare_only_receipt(name)
    if _effective_provenance(args, actor) == "untrusted_content" and not approved:
        # R8: untrusted-triggered actions escalate to CONFIRM regardless of
        # tier, centrally, for every lane. Approved resumes execute.
        from app.ev.tools import _park_independent_hold
        from app.ev.policy import PolicyDecision

        decision = PolicyDecision(
            allowed=False, effect="confirm",
            reason="R8: untrusted content is data, never instruction",
            risk_class="R2", confirmation_ttl_seconds=300,
            target=_audit_target(name, args)[:200] or name,
            spoken="That came from untrusted content. Confirm before I act on it.",
        )
        held = await _park_independent_hold(
            session, name=name, arguments=dict(args), decision=decision,
            actor=actor, device_id=device_id, live_session_id=live_session_id,
            request_id=request_id, channel="voice")
        audit_record("dispatch", name, _audit_target(name, args), "PARKED", False)
        return held
    handler = _HANDLERS[name]
    with trace.span(handler.lane, handler.op(args)):
        result = await handler.run(
            session, args, actor=actor, device_id=device_id,
            live_session_id=live_session_id, request_id=request_id,
            approved=approved,
        )
    audit_record(handler.lane, handler.op(args), _audit_target(name, args),
                 _audit_decision(result), bool(result.get("ok")))
    return result


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
                   approved: bool, provenance: str | None = None) -> str:
    """AUTO / AUTO_JOURNAL / CONFIRM / DENY / PANIC for one effect."""
    if _panicked():
        return "PANIC"
    return decide(action, target, provenance or _provenance(actor))


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
                                  approved=ctx["approved"], provenance=_effective_provenance(args, ctx["actor"]))
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
            confirm=(gate == "CONFIRM"), provenance=_effective_provenance(args, ctx["actor"]),
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
            # Starting a session spawns a persistent process: always an
            # effect (even for read-only commands), always panic-gated.
            if _panicked():
                return _deny("denied: owner panic switch is engaged")
            cmd = str(args.get("cmd") or "")
            cls = classify_command(cmd)
            if cls == "DENY":
                return _deny("denied by policy")
            if cls != "AUTO":
                gate = _gate_mutation(action="run", target=cmd, actor=ctx["actor"],
                                      approved=ctx["approved"], provenance=_effective_provenance(args, ctx["actor"]))
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
                provenance=_effective_provenance(args, ctx["actor"]))
            res["spoken"] = "Session started." if res.get("ok") else str(res.get("error") or "")
            return res
        if op == "send_input":
            gate = _gate_mutation(action="run", target="session-input",
                                  actor=ctx["actor"], approved=ctx["approved"], provenance=_effective_provenance(args, ctx["actor"]))
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
        backend = str(args.get("backend") or "tmux").strip().lower()
        if backend not in {"tmux", "ghostty"}:
            return _deny(f"unknown terminal backend '{backend}'")
        if op == "plan":
            return terminal.TmuxLayout(root=root).plan(list(args.get("panes") or []))
        if op == "list":
            return terminal.list_layouts(root=root)
        if op == "save":
            if _panicked():
                return _deny("denied: owner panic switch is engaged")
            res = terminal.save_layout(str(args.get("name") or ""),
                                       {"panes": list(args.get("panes") or [])}, root=root)
            res["spoken"] = "Layout saved." if res.get("ok") else str(res.get("error") or "")
            return res
        if op == "read":
            if backend == "ghostty" and args.get("visible"):
                layout = terminal.GhosttyLayout(root=root)
                layout.window_id = str(args.get("window_id") or "") or None
                layout.tmux.session = str(args.get("session") or "") or None
                if args.get("window_id"):
                    return layout.read_visible()
                return layout.read_panes()
            layout = terminal.TmuxLayout(root=root)
            layout.session = str(args.get("session") or "") or None
            return layout.read_panes()
        if op == "close":
            if backend == "ghostty":
                layout = terminal.GhosttyLayout(root=root)
                layout.window_id = str(args.get("window_id") or "") or None
                layout.tmux.session = str(args.get("session") or "") or None
                res = layout.close()
            else:
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
                                      actor=ctx["actor"], approved=ctx["approved"], provenance=_effective_provenance(args, ctx["actor"]))
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
            if backend == "ghostty":
                res = terminal.GhosttyLayout(root=root).open(panes, confirm=True)
            else:
                res = terminal.TmuxLayout(root=root).open(panes, confirm=True)
            res["spoken"] = "Layout opened." if res.get("ok") else str(res.get("error") or "")
            return res
        return _deny(f"unknown terminal_layout op '{op}'")



class _ScreenRef(_Handler):
    lane = "screen"

    async def run(self, session, args, **ctx):
        from . import screen_ref
        from .bridge import Bridge

        del ctx
        op = str(args.get("op") or "")
        if op == "resolve_last":
            from .screen_pipeline import screenshot_dir

            directory = args.get("directory") or str(screenshot_dir())
            found = screen_ref.newest_screenshot(directory)
            if found is None:
                return {"ok": False, "verified": True, "evidence": {},
                        "error": "no screenshot found", "undo": None,
                        "spoken": "I don't see a screenshot to work from."}
            return {"ok": True, "verified": True, "evidence": found, "error": None,
                    "undo": None, "spoken": "Found the latest screenshot."}
        if op == "capture":
            if args.get("fixture"):
                res = screen_ref.capture_region(dict(args.get("region") or {"x": 0, "y": 0, "w": 4, "h": 4}))
                res["evidence"]["fixture"] = True
                res["spoken"] = "Captured a test-fixture region."
                return res
            res = Bridge().screenshot()
            res["spoken"] = ("Captured the screen." if res.get("ok")
                             else str(res.get("error") or "Capture failed."))
            return res
        if op == "ocr":
            from .screen_pipeline import ocr_path

            path = str(args.get("path") or "")
            if not path:
                return _deny("ocr needs a path")
            res = await ocr_path(path)
            res["spoken"] = (f"Read {res['evidence'].get('n', 0)} lines." if res.get("ok")
                             else str(res.get("error") or ""))
            return res
        if op == "analyze":
            from .screen_pipeline import analyze_image

            path = str(args.get("path") or "")
            if not path:
                return _deny("analyze needs a path")
            res = await analyze_image(path, session=session)
            if res.get("ok"):
                art = res["evidence"]["artifact"]
                res["spoken"] = (f"Artifact with {len(art.get('panes', []))} panes "
                                 f"({art.get('method')}, confidence {art.get('confidence')}).")
            else:
                res["spoken"] = str(res.get("error") or "")
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
        backend = str(args.get("backend") or "sqlite").strip().lower()
        if backend not in {"sqlite", "postgres"}:
            return _deny(f"unknown sql backend '{backend}'")
        if backend == "postgres":
            container = str(args.get("container") or "ev-sql-scratch")
            if args.get("preview"):
                res = sql_lane.pg_execute(sql, container=container,
                                          root=lane_root(), preview=True)
                res["spoken"] = "Preview only — nothing was committed."
                return res
            kind = sql_lane.classify(sql)
            if kind == "read":
                res = sql_lane.pg_execute(sql, container=container, root=lane_root())
                res["spoken"] = "Query ran." if res.get("ok") else str(res.get("error") or "")
                return res
            gate = _gate_mutation(action="apply" if kind == "dml" else "exec",
                                  target=sql[:200], actor=ctx["actor"],
                                  approved=ctx["approved"], provenance=_effective_provenance(args, ctx["actor"]))
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
            res = sql_lane.pg_execute(sql, container=container, root=lane_root(),
                                      confirm=True)
            res["spoken"] = "Change committed." if res.get("ok") else str(res.get("error") or "")
            return res
        db_path = str(args.get("db_path") or "")
        if not db_path:
            db_path = sql_lane.default_db_path(lane_root())
        if db_path.strip().lower() in {"prod", "production"}:
            return _deny("prod DB DENY: writes to production are not allowed")
        if self._is_prod_db(db_path):
            return _deny("prod DB DENY: Evie's own database is not a scratch lane")
        if db_path != ":memory:":
            allowed = self._scratch_ok(db_path)
            if not allowed:
                gate = _gate_mutation(action="outside-root", target=db_path,
                                      actor=ctx["actor"], approved=ctx["approved"], provenance=_effective_provenance(args, ctx["actor"]))
                if gate == "PANIC":
                    return _deny("denied: owner panic switch is engaged")
                if not ctx["approved"]:
                    return await _park(session, name="sql_query", args=args,
                                        actor=ctx["actor"], device_id=ctx["device_id"],
                                        live_session_id=ctx["live_session_id"],
                                        request_id=ctx["request_id"], target=db_path[:200],
                                        spoken="Confirm using that database file.")
        kind = sql_lane.classify(sql)
        prov = _effective_provenance(args, ctx["actor"])
        if kind == "read":
            res = sql_lane.execute(sql, db_path=db_path, root=lane_root(),
                                   provenance=prov)
            res["spoken"] = "Query ran." if res.get("ok") else str(res.get("error") or "")
            if res.get("confirm") and not ctx["approved"]:
                return await _park(session, name="sql_query", args=args, actor=ctx["actor"],
                                    device_id=ctx["device_id"],
                                    live_session_id=ctx["live_session_id"],
                                    request_id=ctx["request_id"], target=sql[:200],
                                    spoken="Confirm running that query.")
            return res
        if args.get("preview"):
            res = sql_lane.execute(sql, db_path=db_path, root=lane_root(), preview=True,
                                   provenance=prov)
            res["spoken"] = "Preview only — nothing was committed."
            return res
        gate = _gate_mutation(action="apply" if kind == "dml" else "exec",
                              target=sql[:200], actor=ctx["actor"],
                              approved=ctx["approved"], provenance=_effective_provenance(args, ctx["actor"]))
        if gate == "PANIC":
            return _deny("denied: owner panic switch is engaged")
        if gate == "DENY":
            return _deny("denied by policy")
        if kind == "deny":
            return _deny("denied by policy: dangerous SQL construct")
        if not ctx["approved"]:
            return await _park(session, name="sql_query", args=args, actor=ctx["actor"],
                                device_id=ctx["device_id"],
                                live_session_id=ctx["live_session_id"],
                                request_id=ctx["request_id"], target=sql[:200],
                                spoken="Confirm committing that database change.")
        res = sql_lane.execute(sql, db_path=db_path, root=lane_root(), confirm=True,
                               provenance=prov)
        res["spoken"] = "Change committed." if res.get("ok") else str(res.get("error") or "")
        return res

    @staticmethod
    def _is_prod_db(db_path: str) -> bool:
        try:
            from urllib.parse import urlsplit

            from app.config import settings

            url = str(getattr(settings, "database_url", "") or "")
            if url.startswith("sqlite"):
                path_part = url.split(":///", 1)[-1].split("?", 1)[0]
                if not path_part or path_part == ":memory:":
                    return False
                try:
                    return Path(db_path).expanduser().resolve() == Path(path_part).expanduser().resolve()
                except OSError:
                    return False
            return False
        except Exception:
            return False

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
            if _panicked():
                return _deny("denied: owner panic switch is engaged")
            project_dir = str(args.get("project_dir") or "")
            if project_dir and not self._inside_root(project_dir, root):
                gate = _gate_mutation(action="outside-root", target=project_dir,
                                      actor=ctx["actor"], approved=ctx["approved"], provenance=_effective_provenance(args, ctx["actor"]))
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
                                     steps=args.get("steps"),
                                     provenance=_effective_provenance(args, ctx["actor"]))
            if res.get("confirm") and not approved:
                return await _park(session, name="builder_job", args=args,
                                    actor=ctx["actor"], device_id=ctx["device_id"],
                                    live_session_id=ctx["live_session_id"],
                                    request_id=ctx["request_id"],
                                    target="builder-plan",
                                    spoken="Confirm planning that build.")
            res["spoken"] = "Plan ready." if res.get("ok") else str(res.get("error") or "")
            return res
        if op == "apply":
            gate = _gate_mutation(action="apply", target="builder-plan",
                                  actor=ctx["actor"], approved=ctx["approved"], provenance=_effective_provenance(args, ctx["actor"]))
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
        prov = _effective_provenance(args, ctx["actor"])
        if op in {"read", "list"}:
            if op == "list":
                res = lane.list_files(root=root)
            else:
                res = lane.read_file(str(args.get("path") or ""), root=root,
                                     confirm=approved, provenance=prov)
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
                              args.get("content", ""), root=root, confirm=approved,
                              provenance=prov)
        elif op == "overwrite":
            res = lane.overwrite(str(args.get("path") or ""),
                                 args.get("content", ""), root=root, confirm=approved,
                                 provenance=prov)
        elif op == "delete":
            res = lane.delete(str(args.get("path") or ""), root=root, confirm=approved,
                              provenance=prov)
        elif op == "bulk":
            res = lane.bulk(list(args.get("paths") or []),
                            op=str(args.get("sub") or "delete"),
                            root=root, confirm=approved,
                            content=args.get("content", ""), provenance=prov)
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


class _MacAct(_Handler):
    lane = "mac"

    async def run(self, session, args, **ctx):
        from .bridge import Bridge

        op = str(args.get("op") or "")
        bridge = Bridge(root=lane_root())
        if op == "jxa":
            script = str(args.get("script") or "")
            if not script:
                return _deny("jxa needs a script")
            gate = _gate_mutation(action="run", target=f"jxa:{script[:120]}",
                                  actor=ctx["actor"], approved=ctx["approved"], provenance=_effective_provenance(args, ctx["actor"]))
            if gate == "PANIC":
                return _deny("denied: owner panic switch is engaged")
            if gate == "DENY":
                return _deny("denied by policy")
            if not ctx["approved"]:
                return await _park(session, name="mac_act", args=args,
                                    actor=ctx["actor"], device_id=ctx["device_id"],
                                    live_session_id=ctx["live_session_id"],
                                    request_id=ctx["request_id"],
                                    target=f"jxa:{script[:120]}",
                                    spoken="Confirm running that Mac automation.")
            res = bridge.run("jxa", script)
            res["spoken"] = "Automation ran." if res.get("ok") else str(res.get("error") or "")
            return res
        if op == "open_url":
            url = str(args.get("url") or "")
            if not url:
                return _deny("open_url needs a url")
            if _panicked():
                return _deny("denied: owner panic switch is engaged")
            res = bridge.open_url(url)
            res["spoken"] = "Opened." if res.get("ok") else str(res.get("error") or "")
            return res
        if op == "shortcuts_run":
            name = str(args.get("name") or "")
            if not name:
                return _deny("shortcuts_run needs a name")
            gate = _gate_mutation(action="run", target=f"shortcut:{name}",
                                  actor=ctx["actor"], approved=ctx["approved"], provenance=_effective_provenance(args, ctx["actor"]))
            if gate == "PANIC":
                return _deny("denied: owner panic switch is engaged")
            if not ctx["approved"]:
                return await _park(session, name="mac_act", args=args,
                                    actor=ctx["actor"], device_id=ctx["device_id"],
                                    live_session_id=ctx["live_session_id"],
                                    request_id=ctx["request_id"], target=f"shortcut:{name}",
                                    spoken=f"Confirm running shortcut '{name[:80]}'.")
            res = bridge.shortcuts_run(name, str(args.get("input") or ""))
            res["spoken"] = "Shortcut ran." if res.get("ok") else str(res.get("error") or "")
            return res
        if op == "paste_get":
            return bridge.pasteboard_get()
        if op == "paste_set":
            if _panicked():
                return _deny("denied: owner panic switch is engaged")
            res = bridge.pasteboard_set(str(args.get("text") or ""))
            res["spoken"] = "Copied." if res.get("ok") else str(res.get("error") or "")
            return res
        return _deny(f"unknown mac_act op '{op}'")


class _OperatorAct(_Handler):
    lane = "operator"

    def op(self, args):
        return str(args.get("op") or "drive")

    async def run(self, session, args, **ctx):
        from . import operator as op_lane

        op = str(args.get("op") or "")
        app = str(args.get("app") or "")
        approved = bool(ctx["approved"])
        if op == "observe":
            if not app:
                return _deny("observe needs an app")
            return op_lane.observe(app)
        if op == "act":
            if not app:
                return _deny("act needs an app")
            res = op_lane.act(app, str(args.get("frame_id") or ""),
                              dict(args.get("action") or {}), approved=approved)
            if res.get("confirm") and not approved:
                return await _park(session, name="operator_act", args=args,
                                    actor=ctx["actor"], device_id=ctx["device_id"],
                                    live_session_id=ctx["live_session_id"],
                                    request_id=ctx["request_id"],
                                    target=f"operator:{app}",
                                    spoken=f"Confirm driving {app}.")
            return res
        if op == "verify":
            if not app:
                return _deny("verify needs an app")
            return op_lane.verify(app, dict(args.get("expect") or {}))
        if op == "drive":
            goal = str(args.get("goal") or "")
            if not app or not goal:
                return _deny("drive needs an app and a goal")
            if _panicked():
                return _deny("denied: owner panic switch is engaged")
            if not approved:
                return await _park(session, name="operator_act", args=args,
                                    actor=ctx["actor"], device_id=ctx["device_id"],
                                    live_session_id=ctx["live_session_id"],
                                    request_id=ctx["request_id"],
                                    target=f"operator:{app}:{goal[:100]}",
                                    spoken=f"Confirm driving {app} to: {goal[:120]}")
            return op_lane.drive(goal, app, approved=True,
                                 max_steps=int(args.get("max_steps") or 8),
                                 expect=dict(args.get("expect") or {}) or None)
        return _deny(f"unknown operator_act op '{op}'")


class _RecipeAct(_Handler):
    lane = "recipe"

    def op(self, args):
        return str(args.get("op") or "run")

    async def run(self, session, args, **ctx):
        from . import recipes as recipe_lane

        op = str(args.get("op") or "")
        if op == "save":
            if _panicked():
                return _deny("denied: owner panic switch is engaged")
            return await recipe_lane.save(
                session, str(args.get("name") or ""),
                list(args.get("steps") or []),
                params=dict(args.get("params") or {}),
                title=args.get("title"), actor=ctx["actor"])
        if op == "run":
            if _panicked():
                return _deny("denied: owner panic switch is engaged")
            return await recipe_lane.run(
                session, str(args.get("name") or args.get("recipe_id") or ""),
                dict(args.get("params") or {}), actor=ctx["actor"],
                device_id=ctx["device_id"],
                live_session_id=ctx["live_session_id"],
                start_from=int(args.get("start_from") or 0))
        if op == "list":
            return await recipe_lane.list_recipes(session)
        if op == "get":
            return await recipe_lane.get_recipe(
                session, str(args.get("name") or args.get("recipe_id") or ""))
        if op == "delete":
            if _panicked():
                return _deny("denied: owner panic switch is engaged")
            return await recipe_lane.delete_recipe(
                session, str(args.get("name") or args.get("recipe_id") or ""))
        return _deny(f"unknown recipe_act op '{op}'")


class _CommsAct(_Handler):
    lane = "comms"

    def op(self, args):
        return str(args.get("op") or "send")

    async def run(self, session, args, **ctx):
        from . import comms as lane

        op = str(args.get("op") or "")
        channel = lane.normalize_channel(str(args.get("channel") or ""))
        if op in {"draft", "send", "read", "resolve"} and not channel:
            return _deny(f"unknown channel '{args.get('channel')}'")
        if op == "draft":
            return await self._draft(session, args, channel, ctx)
        if op == "send":
            return await self._send(session, args, channel, ctx)
        if op == "read":
            return await self._read(session, args, channel, ctx)
        if op == "resolve":
            return await self._resolve(session, args, channel, ctx)
        if op == "grant":
            return self._grant(args, ctx)
        if op == "revoke":
            return self._revoke(args)
        if op == "grants":
            from .grants import active_grants

            return {"ok": True, "verified": True,
                    "evidence": {"grants": active_grants()}, "error": None,
                    "undo": None, "spoken": "Standing grants listed."}
        return _deny(f"unknown comms_act op '{op}'")

    async def _draft(self, session, args, channel, ctx):
        from . import comms as lane

        to = str(args.get("to") or "").strip()
        body = str(args.get("body") or args.get("text") or "")
        subject = str(args.get("subject") or "")
        if not to or not body.strip():
            return _deny("draft needs to + body")
        flags = lane.content_flags(body, subject)
        first = await self._first_contact(session, channel, to)
        usable, reason = lane.grant_usable(channel, to, flags, first)
        return {"ok": True, "verified": True,
                "evidence": {"draft": {"channel": channel, "to": to, "body": body,
                                       "subject": subject},
                             "flags": flags, "first_contact": first,
                             "grant_usable": usable, "grant_reason": reason,
                             "needs_confirm": not usable},
                "error": None, "undo": None,
                "spoken": "Draft ready." + ("" if usable else " Needs approval.")}

    async def _send(self, session, args, channel, ctx):
        from . import comms as lane
        from .grants import check_grant

        to = str(args.get("to") or "").strip()
        body = str(args.get("body") or args.get("text") or "")
        subject = str(args.get("subject") or "")
        if not to or not body.strip():
            return _deny("send needs to + body")
        if _panicked():
            return _deny("denied: owner panic switch is engaged")
        flags = lane.content_flags(body, subject)
        first = await self._first_contact(session, channel, to)
        usable, reason = lane.grant_usable(channel, to, flags, first)
        approved = bool(ctx["approved"])
        if not usable and not approved:
            return await _park(session, name="comms_act", args=args,
                                actor=ctx["actor"], device_id=ctx["device_id"],
                                live_session_id=ctx["live_session_id"],
                                request_id=ctx["request_id"],
                                target=f"{channel}:{to}"[:200],
                                spoken=f"Confirm sending to {to} via {channel} ({reason}).")
        if usable and not approved:
            from .policy import journal

            journal(lane_root(), {"lane": "comms", "op": "send", "grant": True,
                                  "channel": channel})
        sent = await self._backend_send(session, channel, to=to, body=body,
                                        subject=subject, ctx=ctx,
                                        thread_id=args.get("thread_id"),
                                        in_reply_to=args.get("in_reply_to"))
        if not sent.get("ok"):
            return sent
        verified = bool(sent.get("evidence", {}).get("verified"))
        sent["spoken"] = ("Sent." if verified
                          else "Sent, but I could not verify delivery.")
        return sent

    async def _read(self, session, args, channel, ctx):
        from app.digital.taint import taint_external

        del ctx
        query = str(args.get("query") or "")
        limit = max(1, min(int(args.get("limit") or 10), 50))
        try:
            if channel == "whatsapp":
                from app.digital.adapters.whatsapp import WhatsAppWebAdapter

                adapter = WhatsAppWebAdapter()
                chats = await adapter.search_chats(query) if query else []
                out = {"chats": chats[:limit]}
            elif channel == "gmail":
                rows = await self._gmail_op(session, "search", {"query": query or "in:inbox"},
                                            ctx_actor="owner")
                out = {"messages": rows if isinstance(rows, list) else []}
            else:
                from app.integrations.life_helper import run_life_helper

                res = await run_life_helper("messages.list", {"limit": limit})
                out = dict(res.data or {})
        except Exception as exc:
            return {"ok": False, "verified": True, "evidence": {},
                    "error": f"{channel} read failed: {type(exc).__name__}: {str(exc)[:200]}",
                    "undo": None}
        wrapped = taint_external(out, source=f"{channel}:read")
        return {"ok": True, "verified": True,
                "evidence": {"tainted": wrapped, "provenance": "untrusted_content"},
                "error": None, "undo": None,
                "spoken": "Here is what I found (untrusted content)."}

    async def _resolve(self, session, args, channel, ctx):
        from . import comms as lane

        del session, ctx
        query = str(args.get("query") or args.get("to") or "").strip()
        if not query:
            return _deny("resolve needs a query")
        candidates: list[dict] = []
        try:
            from app.integrations.life_helper import run_life_helper

            res = await run_life_helper("contacts.resolve", {"query": query})
            for row in (res.data or {}).get("contacts", []) or []:
                if isinstance(row, dict):
                    candidates.append({"name": row.get("name") or query,
                                       "handle": row.get("phone") or row.get("email") or "",
                                       "source": "contacts"})
        except Exception:
            pass
        if channel == "whatsapp":
            try:
                from app.digital.adapters.whatsapp import WhatsAppWebAdapter

                for chat in await WhatsAppWebAdapter().search_chats(query):
                    if isinstance(chat, dict):
                        candidates.append({"name": chat.get("name") or query,
                                           "handle": chat.get("chat_ref") or "",
                                           "source": "whatsapp"})
            except Exception:
                pass
        seen: list[dict] = []
        for cand in candidates:
            if (cand.get("name"), cand.get("handle")) not in [
                    (s.get("name"), s.get("handle")) for s in seen]:
                seen.append(cand)
        res = lane.resolve_thread(query, candidates=seen)
        res["spoken"] = ("Which one?" if res.get("evidence", {}).get("needs_choice")
                         else ("Resolved." if res.get("ok") else "No match."))
        return res

    def _grant(self, args, ctx):
        from . import comms as lane
        from .grants import grant

        del ctx
        channel = lane.normalize_channel(str(args.get("channel") or ""))
        to = str(args.get("to") or "").strip()
        if not channel or not to:
            return _deny("grant needs channel + to")
        ttl = float(args.get("ttl_s") or 3600)
        if ttl <= 0 or ttl > 30 * 86400:
            return _deny("grant ttl out of range (1s..30d)")
        res = grant(lane.grant_scope(channel, to), ttl_s=ttl)
        res["spoken"] = ("Grant recorded." if res.get("ok")
                         else str(res.get("error") or ""))
        return res

    def _revoke(self, args):
        from . import comms as lane
        from .grants import revoke

        channel = lane.normalize_channel(str(args.get("channel") or ""))
        to = str(args.get("to") or "").strip()
        if not channel or not to:
            return _deny("revoke needs channel + to")
        res = revoke(lane.grant_scope(channel, to))
        res["spoken"] = "Grant revoked."
        return res

    async def _first_contact(self, session, channel: str, to: str) -> bool | None:
        """True = first contact, False = known, None = backend unknown.

        Unknown fails toward first-contact (safe): grants never apply.
        """
        if not to:
            return True
        try:
            if channel == "whatsapp":
                from app.digital.adapters.whatsapp import WhatsAppWebAdapter

                chats = await WhatsAppWebAdapter().search_chats(to)
                return not bool(chats)
            if channel == "gmail":
                rows = await self._gmail_op(
                    session, "search", {"query": f"to:{to} OR from:{to}"},
                    ctx_actor="owner")
                return not bool(rows)
            from app.integrations.life_helper import run_life_helper

            res = await run_life_helper("messages.list", {"limit": 50})
            needle = to.strip().lower()
            for msg in (res.data or {}).get("messages", []) or []:
                hay = f"{msg.get('sender', '')} {msg.get('handle', '')}".lower()
                if needle and needle in hay:
                    return False
            return True
        except Exception:
            return None

    async def _gmail_op(self, session, operation: str, args: dict, *, ctx_actor: str):
        from app.digital.adapters.gmail import GmailAdapter
        from app.digital.fabric import OpContext

        adapter = GmailAdapter()
        ctx = OpContext(actor=ctx_actor, session=session)
        result = await adapter.execute(operation, dict(args), ctx=ctx)
        return result

    async def _backend_send(self, session, args_channel, **kw) -> dict:
        channel = args_channel
        to, body = kw.get("to", ""), kw.get("body", "")
        try:
            if channel == "whatsapp":
                from app.digital.adapters.whatsapp import WhatsAppWebAdapter

                adapter = WhatsAppWebAdapter()
                chats = await adapter.search_chats(to)
                if not chats:
                    return {"ok": False, "verified": True, "evidence": {},
                            "error": f"no WhatsApp chat for {to}", "undo": None}
                ref = chats[0].get("chat_ref") or ""
                if len(chats) > 1:
                    return {"ok": False, "verified": True,
                            "evidence": {"candidates": [
                                {"name": c.get("name"), "chat_ref": c.get("chat_ref")}
                                for c in chats[:5]]},
                            "error": "ambiguous recipient", "undo": None}
                sent = await adapter.send(ref, body)
                verified = bool(sent.get("verified_in_thread"))
                return {"ok": bool(sent.get("ok", sent.get("sent", False))),
                        "verified": verified,
                        "evidence": {"channel": channel, "to": to, "verified": verified,
                                     "message_id": sent.get("message_id"),
                                     "transport": sent},
                        "error": None if sent.get("ok", sent.get("sent")) else str(
                            sent.get("error") or "send failed"),
                        "undo": None}
            if channel == "gmail":
                subject = kw.get("subject", "") or "(no subject)"
                result = await self._gmail_op(
                    session, "send",
                    {"to": [to], "subject": subject, "body": body},
                    ctx_actor="owner")
                status = getattr(result, "status", None)
                ok = str(status) in {"COMPLETED_VERIFIED", "completed_verified"} or bool(
                    getattr(result, "ok", False))
                payload = getattr(result, "payload", {}) or {}
                if isinstance(result, dict):
                    ok = bool(result.get("sent", result.get("ok")))
                    payload = result
                verified = ok and bool(payload.get("id") or payload.get("thread_id"))
                if not ok:
                    err = payload.get("error") if isinstance(payload, dict) else None
                    return {"ok": False, "verified": True, "evidence": {},
                            "error": str(err or status or "gmail send failed"),
                            "undo": None}
                return {"ok": True, "verified": verified,
                        "evidence": {"channel": channel, "to": to, "verified": verified,
                                     "message_id": payload.get("id"),
                                     "thread_id": payload.get("thread_id")},
                        "error": None, "undo": None}
            from app.integrations.life_helper import run_life_helper

            cmd = "mail.send" if channel == "mail" else "messages.send"
            call_args: dict[str, Any] = {"to": to, "text": body} if channel != "mail" else {
                "to": to, "subject": kw.get("subject", "") or "Message from Evie",
                "body": body}
            res = await run_life_helper(cmd, call_args)
            data = res.data or {}
            sent_ok = bool(data.get("sent"))
            return {"ok": sent_ok, "verified": sent_ok,
                    "evidence": {"channel": channel, "to": to,
                                 "delivery": res.delivery, "data": data},
                    "error": None if sent_ok else "helper send unconfirmed",
                    "undo": None}
        except Exception as exc:
            name = type(exc).__name__
            if "Permission" in name or "Denied" in name:
                return {"ok": False, "verified": True, "evidence": {},
                        "error": f"{channel} permission denied",
                        "fix": "Run `ev setup` and grant the helper its permission.",
                        "undo": None}
            if "Timeout" in name:
                return {"ok": False, "verified": True, "evidence": {},
                        "error": f"{channel} backend timeout", "undo": None}
            return {"ok": False, "verified": True, "evidence": {},
                    "error": f"{channel} send failed: {name}", "undo": None}


class _AtlasDiscover(_Handler):
    lane = "atlas"

    def op(self, args):
        return "discover"

    async def run(self, session, args, **ctx):
        from .atlas import describe, route
        from .gaps import closest_alternative, record_gap

        del session, ctx
        domain = str(args.get("domain") or args.get("app") or "").strip()
        action = str(args.get("action") or "")
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
        routed = route(domain, action) if domain else None
        lane = (routed.get("evidence") or {}) if routed and routed.get("ok") else {}
        if descriptors or lane.get("lane"):
            return {"ok": True, "verified": True,
                    "evidence": {"descriptors": descriptors[:20], "n": len(descriptors),
                                 "route": lane or None,
                                 "apps_scanned": describe()["evidence"].get("apps", 0)},
                    "error": None, "undo": None,
                    "spoken": "Atlas checked."}
        gap = record_gap(f"atlas:{domain or 'all'}")
        return {"ok": True, "verified": True,
                "evidence": {"descriptors": [],
                             "gap": gap.get("evidence", {}),
                             "alternative": closest_alternative(domain)},
                "error": None, "undo": None,
                "spoken": "No matching app capability yet; I recorded the gap."}


_HANDLERS: dict[str, _Handler] = {
    "comms_act": _CommsAct(),
    "files_op": _FilesOp(),
    "mac_act": _MacAct(),
    "operator_act": _OperatorAct(),
    "recipe_act": _RecipeAct(),
    "shell_run": _ShellRun(),
    "shell_session": _ShellSession(),
    "terminal_layout": _TerminalLayout(),
    "screen_ref": _ScreenRef(),
    "sql_query": _SqlQuery(),
    "pim_act": _PimAct(),
    "builder_job": _BuilderJob(),
    "atlas_discover": _AtlasDiscover(),
}
