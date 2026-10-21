"""General task agent: any single goal in, a verified run out.

Shared-agent note: this module is the deterministic local executor other
lanes call (chat single-step, agent intake fallback, agent_task tool).
Improvements welcome, but keep every step kind offline-runnable and keep
run_task's receipt shape ({ok, executed, verified, spoken}) — callers
branch on it. Do not route steps through the cloud brain here; the brain
lives in app/agent + app/gateway.


The owner's ask is never matched against one static command. A goal is
decomposed into steps over the surfaces Evie already owns (files, apps,
GUI, web, shell), the owner approves the plan once, and the loop then
acts, observes each result, and stops honestly on the first failure.
Static parsers are step generators inside this loop, not gatekeepers:
when they resolve a clause deterministically that step is used verbatim,
and when they cannot, the clause still becomes a GUI/shell step instead
of a dead end. Offline the planner is deterministic and says so through
``degraded=True`` on the receipt; a brain may reorder or enrich steps
but never invents paths or file bodies.
"""

from __future__ import annotations

import re
from typing import Any

MAX_TASK_STEPS = 12

_READ_ACTIONS = frozenset({"search", "list", "open", "read", "summarize", "reveal"})

_STEP_CLAUSE_RE = re.compile(
    r"\s+(?:and\s+)?then\s+|;\s*"
    # A second imperative after "and" is a second step ("create X and put Y").
    # Bare "and" alone never splits ("fish and chips" stays one clause).
    r"|\s+and\s+(?=(?:put|write|create|make|delete|remove|run|open|copy|move|rename|append|list|show|find)\b)",
    re.I,
)


def _clauses(goal: str) -> list[str]:
    parts = [part.strip(" .,") for part in _STEP_CLAUSE_RE.split(goal or "")]
    return [part for part in parts if part]


def _step(kind: str, label: str, *, mutating: bool, **payload: Any) -> dict[str, Any]:
    return {"kind": kind, "label": label, "mutating": mutating, **payload}


_KNOWN_APPS = (
    "System Settings",
    "Finder",
    "Safari",
    "Google Chrome",
    "Chrome",
    "TextEdit",
    "Notes",
    "Mail",
    "Calendar",
    "Music",
    "Terminal",
    "Preview",
    "Photos",
)


def _infer_app(clause: str, target_app: str | None) -> str:
    if target_app:
        return target_app
    lowered = (clause or "").lower()
    for app in _KNOWN_APPS:
        if app.lower() in lowered:
            return app
    return ""


def _candidate_steps(clause: str, target_app: str | None) -> list[dict[str, Any]]:
    """Every surface that claims a clause, in deterministic claim order."""
    from app.ev.computer_strategy import (
        looks_like_web_research,
        resolve_generic_computer_goal,
        resolve_in_app_computer_goal,
        web_search_query_from_text,
    )
    from app.ev.laptop_files import resolve_file_computer_goal
    from app.ev.mac_host import looks_like_mac_command

    candidates: list[dict[str, Any]] = []
    from app.ev.terminal_continuity import looks_like_terminal_resume

    if looks_like_terminal_resume(clause):
        # The resume handler owns the whole clause: OCR the screenshot,
        # extract the commands, launch Ghostty, type them. A plain
        # open_app claim would launch Ghostty and stop there.
        candidates.append(
            _step(
                "terminal",
                clause,
                mutating=True,
                capability="terminal_resume",
                arguments={"goal": clause},
            )
        )
        return candidates
    shell_command = _shell_command_from_clause(clause)
    if shell_command:
        candidates.append(
            _step(
                "shell",
                clause,
                mutating=True,
                capability="shell_exec",
                arguments={"goal": clause, "command": shell_command},
            )
        )
        return candidates
    file_goal = resolve_file_computer_goal(clause, target_app)
    if file_goal is not None:
        capability, args = file_goal
        action = str(args.get("action") or "")
        candidates.append(
            _step(
                "file_op",
                clause,
                mutating=action not in _READ_ACTIONS,
                capability=capability,
                arguments=dict(args),
            )
        )
    computer_goal = resolve_in_app_computer_goal(clause, target_app) or (
        resolve_generic_computer_goal(clause, target_app)
    )
    if computer_goal is not None:
        capability, args = computer_goal
        candidates.append(
            _step(
                "computer",
                clause,
                mutating=capability not in {"list_apps", "computer_status", "screen_look"},
                capability=capability,
                arguments=dict(args),
            )
        )
    if looks_like_web_research(clause):
        candidates.append(
            _step(
                "web_search",
                clause,
                mutating=False,
                capability="search_web",
                arguments={"query": web_search_query_from_text(clause) or clause, "limit": 5},
            )
        )
    if looks_like_mac_command(clause):
        candidates.append(
            _step(
                "shell",
                clause,
                mutating=True,
                capability="mac_command",
                arguments={"goal": clause},
            )
        )
    candidates.append(
        _step(
            "gui",
            clause,
            mutating=True,
            capability="ui_action",
            arguments={"goal": clause, "app": _infer_app(clause, target_app)},
        )
    )
    return candidates


def _shell_command_from_clause(clause: str) -> str:
    """Explicit `run the shell command X` / backtick-quoted commands."""
    backticked = re.search(r"`([^`]+)`", clause or "")
    if backticked and re.search(r"\b(?:run|execut)\w*\b", clause, re.I):
        return backticked.group(1).strip()
    match = re.search(
        r"\b(?:run|execut\w*)\s+(?:the\s+)?(?:shell\s+command|terminal\s+command|in\s+(?:the\s+)?terminal|command)\s*[::]?\s*(.+)$",
        clause or "",
        re.I,
    )
    if match:
        return match.group(1).strip().strip("`\"'")
    return ""


def _plan_clause(clause: str, target_app: str | None) -> dict[str, Any]:
    """One clause -> one step. Existing resolvers get first claim."""
    return _candidate_steps(clause, target_app)[0]


def plan_task(goal: str, target_app: str | None = None) -> dict[str, Any]:
    """Decompose a goal into an ordered, approvable plan."""
    clauses = _clauses(goal)[:MAX_TASK_STEPS]
    steps = [_plan_clause(clause, target_app) for clause in clauses]
    return {
        "goal": goal,
        "steps": steps,
        "mutating": any(step["mutating"] for step in steps),
        "planner": "deterministic",
        "degraded": True,
    }


async def plan_task_async(
    goal: str, target_app: str | None = None
) -> dict[str, Any]:
    """Deterministic plan, with the brain arbitrating only contested clauses.

    The brain chooses among deterministic candidates (surface per clause,
    or the app for an app-less GUI clause). It never invents paths, file
    bodies, or new capabilities: an unusable answer keeps the
    deterministic plan with ``degraded=True``.
    """
    plan = plan_task(goal, target_app)
    clauses = _clauses(goal)[:MAX_TASK_STEPS]
    contested: list[tuple[int, list[dict[str, Any]]]] = []
    for index, clause in enumerate(clauses):
        candidates = _candidate_steps(clause, target_app)
        claims = [step for step in candidates if step["kind"] != "gui"]
        gui_only = len(claims) == 0
        app_less = gui_only and not candidates[-1]["arguments"].get("app")
        if len(claims) > 1 or app_less:
            contested.append((index, candidates))
    if not contested:
        return plan
    try:
        from app.gateway.roles import text_role_available

        if not text_role_available():
            return plan
        from app.gateway.roles import DecisionQuestion, answer_choice, decide_via_role

        questions: dict[str, Any] = {}
        options: dict[str, list[dict[str, Any]]] = {}
        for index, candidates in contested:
            claims = [step for step in candidates if step["kind"] != "gui"]
            if claims:
                pool = claims + [candidates[-1]]
                criteria = {
                    f"{step['kind']}:{n}": (
                        f"Run this clause on the {step['kind']} surface "
                        f"({step.get('capability')}) with the given arguments."
                    )
                    for n, step in enumerate(pool)
                }
            else:
                pool = [
                    _step(
                        "gui",
                        plan["steps"][index]["label"],
                        mutating=True,
                        capability="ui_action",
                        arguments={"goal": plan["steps"][index]["label"], "app": app},
                    )
                    for app in _KNOWN_APPS
                ]
                criteria = {
                    f"app:{app}": f"Drive this clause in {app}."
                    for app in _KNOWN_APPS
                }
            qid = f"clause{index}"
            options[qid] = pool
            questions[qid] = DecisionQuestion(
                type="choice",
                instructions=(
                    "Pick the surface that best matches this clause of the "
                    "owner's request. Never invent paths or file content."
                ),
                criteria=criteria,
            )
        call = await decide_via_role(
            {"request": goal[:2000]},
            questions,
            actor="task_agent",
        )
    except Exception:
        return plan
    if getattr(call, "status", None) != "ok":
        return plan
    steps = list(plan["steps"])
    changed = False
    for index, _candidates in contested:
        qid = f"clause{index}"
        choice = answer_choice(call, qid)
        if not choice or ":" not in choice:
            continue
        pool = options[qid]
        try:
            head, tail = str(choice).split(":", 1)
            if head == "app":
                picked = pool[_KNOWN_APPS.index(tail)]
            else:
                picked = pool[int(tail)]
                if str(picked.get("kind") or "") != head:
                    continue
        except (IndexError, ValueError):
            continue
        steps[index] = picked
        changed = True
    if not changed:
        return plan
    return {
        **plan,
        "steps": steps,
        "mutating": any(step["mutating"] for step in steps),
        "planner": "brain_arbitrated",
        "degraded": False,
    }


def describe_plan(plan: dict[str, Any]) -> str:
    labels = [str(step.get("label") or step.get("kind") or "") for step in plan.get("steps") or []]
    if not labels:
        return "I couldn't turn that into steps I can run."
    if len(labels) == 1:
        return f"Here's my plan: {labels[0]}. Go ahead?"
    joined = "; ".join(f"{index + 1}. {label}" for index, label in enumerate(labels))
    return f"Here's my plan: {joined}. Go ahead?"


async def _default_run_step(step: dict[str, Any]) -> dict[str, Any]:
    kind = str(step.get("kind") or "")
    args = dict(step.get("arguments") or {})
    if kind == "file_op":
        from app.ev.laptop_files import execute_file_op

        return await execute_file_op(args)
    if kind == "web_search":
        from app.search.providers import get_search_provider

        provider = get_search_provider()
        if provider is None:
            return {
                "ok": False,
                "executed": False,
                "verified": False,
                "error": "search_unavailable",
                "spoken": "Web search isn't available right now.",
            }
        results = await provider.search(
            str(args.get("query") or ""), limit=int(args.get("limit") or 5)
        )
        return {
            "ok": True,
            "executed": False,
            "verified": True,
            "results": [getattr(item, "title", str(item)) for item in results],
            "spoken": f"Found {len(results)} results.",
        }
    if kind == "shell":
        command = str(args.get("command") or "").strip()
        if command:
            from pathlib import Path

            from app.fabric.shell_sessions import ShellSessions
            from app.fabric.trace import span as _trace_span

            # Same evidence as the conversational shell lane: agent-first
            # turns execute here, and the fabric turn ring must show it.
            with _trace_span("shell", "run"):
                receipt = ShellSessions().run(
                    command, cwd=str(Path.home()), confirm=True, provenance="agent_task"
                )
            evidence = receipt.get("evidence") or {}
            out = str(evidence.get("out") or "").strip()
            if receipt.get("ok"):
                return {
                    "ok": True,
                    "executed": True,
                    "verified": True,
                    "spoken": f"Ran it. {out[:400]}".strip(),
                    "output": out,
                    "exit_code": evidence.get("exit_code"),
                }
            return {
                "ok": False,
                "executed": True,
                "verified": True,
                "error": str(receipt.get("error") or "shell_failed"),
                "spoken": f"That command failed: {str(receipt.get('error') or '')[:200]}".strip(),
            }
        from app.ev.mac_host import run_owner_mac_goal

        return run_owner_mac_goal(str(args.get("goal") or ""))
    if kind == "terminal":
        from app.ev.terminal_continuity import (
            extract_resume_commands,
            find_terminal_screenshots,
            resume_from_screenshot,
            resume_named_sessions,
        )

        goal_text = str(args.get("goal") or "")
        direct = extract_resume_commands(goal_text)
        if direct:
            return await resume_named_sessions(direct)
        shots = find_terminal_screenshots()
        if not shots:
            return {
                "ok": False,
                "executed": False,
                "verified": False,
                "error": "no_screenshot",
                "spoken": "I don't see a terminal screenshot on your Desktop.",
            }
        return await resume_from_screenshot(shots[0])
    if kind == "gui":
        from app.fabric.operator import drive

        app = str(args.get("app") or "")
        if not app:
            return {
                "ok": False,
                "executed": False,
                "verified": False,
                "error": "no_target_app",
                "spoken": "Which app should I use for that?",
            }
        return drive(str(args.get("goal") or ""), app, approved=True)
    return {
        "ok": False,
        "executed": False,
        "verified": False,
        "error": "needs_session_runner",
        "spoken": "That step needs the live session runner.",
    }


async def run_task(
    goal: str,
    *,
    target_app: str | None = None,
    approved: bool = False,
    runner: Any = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan, gate on one approval, then act/observe/verify step by step."""
    plan = plan if plan is not None else await plan_task_async(goal, target_app)
    steps = plan.get("steps") or []
    if not steps:
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "error": "empty_plan",
            "spoken": "I couldn't turn that into steps I can run.",
            "plan": plan,
        }
    if plan.get("mutating") and not approved:
        return {
            "ok": False,
            "executed": False,
            "verified": False,
            "needs_confirm": True,
            "confirm": True,
            "error": "confirm_required",
            "spoken": describe_plan(plan),
            "plan": plan,
        }
    run_step = runner or _default_run_step
    receipts: list[dict[str, Any]] = []
    for step in steps:
        result = await run_step(step)
        receipts.append({"step": step.get("label"), "kind": step.get("kind"), "result": result})
        if not result.get("ok"):
            return {
                "ok": False,
                "executed": any(r["result"].get("executed") for r in receipts),
                "verified": False,
                "error": str(result.get("error") or "step_failed"),
                "spoken": str(result.get("spoken") or f"Stopped at: {step.get('label')}."),
                "plan": plan,
                "receipts": receipts,
            }
    last = receipts[-1]["result"] if receipts else {}
    return {
        "ok": True,
        "executed": any(r["result"].get("executed") for r in receipts),
        "verified": all(r["result"].get("verified", True) for r in receipts),
        "spoken": str(last.get("spoken") or "Done."),
        "plan": plan,
        "receipts": receipts,
    }
