"""Deterministic task intake: decide, before any model call, whether a turn is
a general task, a status question, a cancel, or an approval answer.

This is the routing layer that stops the old failure mode where a static
pre-router acknowledged a complicated goal and no runner ever executed it.
Order inside the kernel is deliberate:

1. status / cancel / approval answers are handled here first (truthful,
   ledger-backed, no model);
2. general task goals are submitted to the durable runner here, before the
   static explain/file/code routers get a chance to hijack them;
3. only goals that this layer declines continue to the legacy paths.

The heuristic is intentionally conservative: single known capabilities
(weather, timers, messages, opening things) and pure questions return ``None``
and keep their existing fast paths.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from app.agent import ledger, runner
from app.agent.brain import build_agent_provider
from app.config import settings
from app.contracts import ChatMessage

logger = logging.getLogger("ev.agent.intake")

_STATUS_RE = re.compile(
    r"^\s*(?:evie[, ]+)?(?:"
    r"status|progress|update|"
    r"what are you (?:doing|working on)|what(?:'s| is) (?:happening|going on)|"
    r"how(?:'s| is) (?:it|the task|that) going|any (?:updates?|progress)|"
    r"what have you (?:done|finished)|show (?:me )?(?:the )?tasks?|task list|"
    r"are you (?:still )?(?:working|busy)|where are you"
    r")\b",
    re.IGNORECASE,
)

_CANCEL_RE = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"cancel (?:that|it|this|the task|the job)|"
    r"stop (?:that|it|this|the task|the job|working on it)|"
    r"abort (?:that|it|this|the task)|kill (?:that|it|the task)"
    r")\b",
    re.IGNORECASE,
)

_TASK_VERBS = re.compile(
    r"\b(?:"
    r"create|make|write|save|move|copy|rename|delete|remove|clean(?:\s*up)?|"
    r"organi[sz]e|sort|download|upload|install|unzip|extract|convert|resize|"
    r"merge|split|run|execute|build|compile|fix|repair|refactor|update|upgrade|"
    r"back\s?up|sync|scrape|batch|generate|set\s?up|prepare|collect|gather|"
    r"find all|find every|search for|look through|scan|compare|dedupe|archive|"
    r"compress|send all|email all"
    r")\b",
    re.IGNORECASE,
)

_OPERATIONAL_VERBS = re.compile(
    r"\b(?:"
    r"move|copy|rename|delete|remove|clean(?:\s*up)?|organi[sz]e|sort|"
    r"download|upload|install|unzip|extract|convert|resize|merge|split|run|"
    r"execute|build|compile|fix|repair|refactor|update|upgrade|back\s?up|sync|"
    r"scrape|batch|collect|gather|audit|find all|find every|search for|"
    r"look through|scan|compare|dedupe|archive|compress|send all|email all"
    r")\b",
    re.IGNORECASE,
)

_SYSTEM_NOUNS = re.compile(
    r"\b(?:"
    r"files?|folders?|director(?:y|ies)|desktop|downloads?|documents?|pictures?|"
    r"photos?|videos?|music|invoices?|receipts?|pdfs?|csvs?|spreadsheets?|"
    r"projects?|repos?|repositor(?:y|ies)|git|branches?|commits?|backups?|"
    r"scripts?|terminal|shell|commands?|logs?|archives?|zip|code|tests?|"
    r"website|web\s?site|spreadsheet|database|server|docker|homebrew|brew|"
    r"disk|storage|laptop|mac(?:book)?|computer"
    r")\b",
    re.IGNORECASE,
)

_MULTI_RE = re.compile(
    r"\b(?:then|and then|after that|finally|also)\b|;\s*|,\s*and\b|(?:^|\s)\d+[.)]\s",
    re.IGNORECASE,
)

_SIMPLE_CAP = re.compile(
    r"\b(?:"
    r"weather|forecast|temperature|remind|reminder|timer|alarm|"
    r"message|send (?:a )?(?:text|message)|whatsapp|email|mail|call|dial|"
    r"screenshot|screen|look at|what do you see|play|pause|skip|volume|"
    r"open (?:the )?(?:app|safari|chrome|finder|settings|notes|mail|calendar)|"
    r"launch|close (?:the )?app|switch to|remember that|recall|who is|what is"
    r")\b",
    re.IGNORECASE,
)

# Explicit studio/overnight language keeps the legacy coding lane, which owns
# multi-shift scaffolds and steering. Everything else compound goes to the
# durable agent, so an ack can never be the end of the story.
_STUDIO_HINT = re.compile(
    r"\b(?:"
    r"overnight|while i (?:sleep|am away)|coding job|code studio|"
    r"continue (?:the )?(?:coding )?(?:job|studio|goal)|steer (?:the )?(?:job|studio)|"
    r"background (?:coding )?(?:job|task)"
    r")\b",
    re.IGNORECASE,
)

_SIMPLE_START = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"weather|forecast|remind|reminder|set (?:a )?(?:timer|alarm)|timer|alarm|"
    r"message|text|whatsapp|email|mail|call|dial|screenshot|screen|look|"
    r"play|pause|skip|volume|open|launch|close|switch to|remember|recall|"
    r"who|what|when|where|why|how"
    r")\b",
    re.IGNORECASE,
)

_QUESTION_START = re.compile(
    r"^\s*(?:what|who|when|where|why|how|which|is|are|was|were|do|does|did|"
    r"can you tell|explain|tell me about|summarize|summarise)\b",
    re.IGNORECASE,
)


def looks_actionable_for_retry(text: str) -> bool:
    """A goal with a real action verb that is not a known simple capability.

    Used only by the post-turn guard: if the model claims deferred work and no
    task exists, this decides whether the goal should be forced into the task
    lane instead of being spoken as a promise.
    """
    raw = (text or "").strip()
    if not raw or looks_like_status(raw) or looks_like_cancel(raw):
        return False
    from app.cognitive.local_route import looks_like_local_work

    if looks_like_local_work(raw):
        return True
    if _SIMPLE_START.search(raw) or _QUESTION_START.match(raw):
        return False
    return bool(_TASK_VERBS.search(raw) or _OPERATIONAL_VERBS.search(raw))


def looks_like_status(text: str) -> bool:
    return bool(_STATUS_RE.search(text or ""))


def looks_like_cancel(text: str) -> bool:
    return bool(_CANCEL_RE.search(text or ""))


def looks_like_general_task(text: str) -> bool:
    """True only for goals that need the durable agent, not a fast path."""
    raw = (text or "").strip()
    if not raw or len(raw) > 2000:
        return False
    if looks_like_status(raw) or looks_like_cancel(raw):
        return False
    # Explicit studio/overnight work keeps its dedicated lane.
    if _STUDIO_HINT.search(raw):
        return False
    words = raw.split()
    if len(words) < 3:
        return False
    # Local machine work (files, terminal, apps, screen) belongs to the agent
    # loop when it is compound, a state question, or not a single parsed file
    # operation. Simple file creates/reads stay on the deterministic file lane,
    # which is faster and already tested; compound work gets real hands.
    from app.cognitive.local_route import looks_like_local_work, needs_full_agent

    if looks_like_local_work(raw):
        from app.ev.laptop_files import looks_like_file_task, parse_file_goal

        parsed_file = looks_like_file_task(raw) and parse_file_goal(raw) is not None
        compound_hint = bool(
            _MULTI_RE.search(raw)
            or re.search(
                r"\b(?:ghostty|ghosty|tmux|terminal|panes?|screenshot)\b"
                r"|\b(?:two|three|four|five|\d+)\s+(?:files?|folders?)\b"
                r"|\b(?:organi[sz]e|sort|dedupe|archive|clean\s*up)\b"
                r"|\b(?:find every|find all|search (?:for|all))\b"
                r"|\bfolders?\b[^.]{0,80}\b(?:write|note|file)\b",
                raw,
                re.I,
            )
        )
        parsed = parse_file_goal(raw) if parsed_file else None
        action = str((parsed or {}).get("action") or "")
        simple_write = action in {"write", "append", "edit", "create"} and not compound_hint
        if parsed_file and simple_write:
            # One create/write/append of one file. The deterministic file lane
            # owns it and is faster than the agent. Listing, searching, and
            # opening a folder are hands work: the kernel's file fast path
            # dead-ends on those, so they continue to the agent below.
            return False
        if parsed_file and action in {"list", "retrieve", "ask_which"}:
            return True
        if needs_full_agent(raw) or _QUESTION_START.match(raw):
            return True
        return not _SIMPLE_START.match(raw)
    multi = bool(_MULTI_RE.search(raw))
    if _QUESTION_START.search(raw) and not (_TASK_VERBS.search(raw) and multi):
        return False
    if _SIMPLE_CAP.search(raw) and not multi:
        return False
    if _SIMPLE_START.search(raw):
        return False
    verbs = {match.group(0).lower() for match in _TASK_VERBS.finditer(raw)}
    verb = bool(verbs)
    operational = bool(_OPERATIONAL_VERBS.search(raw))
    noun = bool(_SYSTEM_NOUNS.search(raw))
    # Two different actions in one sentence ("create X and write Y") are a
    # multi-step goal even without "then" or a comma.
    multi_actions = len(verbs) >= 2
    if verb and (multi or multi_actions):
        return True
    if operational and noun:
        return True
    return bool(multi and re.search(r"\bmy (?:laptop|mac|computer)\b", raw, re.IGNORECASE))


# --------------------------------------------------------------------------- #
# Model-first routing: the brain routes what the static regexes would drop
# --------------------------------------------------------------------------- #

_ROUTE_SYSTEM = """You route one owner turn for Evie, a personal AI on the owner's own Mac.
Reply with exactly one JSON object and nothing else: {"route": "task"} or {"route": "chat"}.
Choose "task" when the owner wants something DONE: act on this Mac (files,
folders, terminal, apps, screen), use their personal data (mail, messages,
calendar, contacts), fetch or change anything on the network, or any
multi-step goal — no matter how it is phrased.
Choose "chat" for greetings, thanks, small talk, and questions answerable
from general knowledge without touching the Mac or their data.
Status, cancel, and yes/no approval turns are handled before you; if you see
one, reply {"route": "chat"}.
When unsure, choose "task": the agent answers conversational turns directly
and only uses tools when the answer depends on the Mac."""


def _parse_route(text: str) -> str | None:
    """Extract a task|chat verdict from a router reply. None means unreadable."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        payload = json.loads(cleaned)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    route = str(payload.get("route") or "").strip().lower()
    return route if route in {"task", "chat"} else None


async def route_goal(text: str, *, source: str = "chat", provider: Any = None) -> str:
    """Route one turn to "task" (durable agent lane) or "chat" (fast paths).

    ``EV_AGENT_ROUTING`` selects the mode: ``model_backstop`` (default) sends
    regex-missed turns to the brain and keeps instant routing for known task
    shapes; ``model_first`` asks the brain about every turn; ``static`` keeps
    the legacy regex verdict only. Control turns never reach here — the
    caller runs ``handle_control`` first.

    The legacy classifier is the offline fallback, never the decider when a
    brain answers: no usable brain, a privacy-boundary refusal, a timeout, a
    crash, or an unreadable reply all fall back to it, so a dark brain
    degrades to yesterday's routing instead of dropping the turn or
    inventing an answer.
    """
    raw = (text or "").strip()
    if not raw or len(raw) > 2000:
        return "chat"
    mode = str(getattr(settings, "agent_routing", "model_backstop") or "model_backstop")
    mode = mode.strip().lower()
    legacy_task = looks_like_general_task(raw)
    if mode == "static":
        return "task" if legacy_task else "chat"
    if mode != "model_first" and legacy_task:
        return "task"
    if provider is None:
        import os

        if os.environ.get("PYTEST_CURRENT_TEST"):
            # Hermetic suites never route through ambient keys; tests inject
            # an explicit provider to exercise the model path.
            return "task" if legacy_task else "chat"
        try:
            from app.agent import service

            choice = service.resolve_agent_brain()
        except Exception:
            choice = None
        if choice is None or not choice.available or choice.degraded:
            return "task" if legacy_task else "chat"
        try:
            provider = build_agent_provider(choice)
        except Exception:
            logger.debug("router provider build failed; static fallback", exc_info=True)
            return "task" if legacy_task else "chat"
    try:
        from app.security.boundary import ModelBoundaryViolation, guard_model_payload

        messages = [
            ChatMessage(role="system", content=_ROUTE_SYSTEM),
            ChatMessage(role="user", content=raw),
        ]
        try:
            safe = guard_model_payload(messages, None)
        except ModelBoundaryViolation:
            return "task" if legacy_task else "chat"
        if source in {"voice", "phone"}:
            timeout = float(getattr(settings, "agent_route_voice_timeout_s", 4.0) or 4.0)
        else:
            timeout = float(getattr(settings, "agent_route_timeout_s", 8.0) or 8.0)
        # Routing is a two-token decision; low effort keeps it from adding a
        # high-reasoning delay before the real turn.
        if hasattr(provider, "reasoning_effort"):
            provider.reasoning_effort = "low"
        reply = await asyncio.wait_for(
            provider.chat(safe, temperature=0.0), timeout=max(1.0, min(timeout, 30.0))
        )
        route = _parse_route(str(getattr(reply, "text", "") or ""))
    except Exception:
        logger.debug("model routing failed; static fallback", exc_info=True)
        return "task" if legacy_task else "chat"
    if route is None:
        return "task" if legacy_task else "chat"
    return route


_APPROVE_WORDS = re.compile(
    r"^\s*(?:please\s+)?(?:approve|approved|approve it|proceed|continue|go on|carry on)\b",
    re.IGNORECASE,
)
_REJECT_WORDS = re.compile(
    r"^\s*(?:do not|don'?t|stop it|stop that|abort|reject|deny)\b",
    re.IGNORECASE,
)


def _affirmative(text: str) -> bool:
    from app.ev.continuity import is_affirmative_reply

    return is_affirmative_reply(text) or bool(_APPROVE_WORDS.match(text or ""))


def _negative(text: str) -> bool:
    from app.ev.continuity import is_negative_reply

    return is_negative_reply(text) or bool(_REJECT_WORDS.match(text or ""))


# --------------------------------------------------------------------------- #
# Control turns (status / cancel / approval) — no model, ledger-backed
# --------------------------------------------------------------------------- #


#: Sources whose "yes" is heard, not pressed. R7: a spoken yes may approve
#: only low-risk actions. A send, call, SQL write or permanent delete needs a
#: physical confirmation: the HUD card, the paired phone, or a typed reply.
_SPOKEN_SOURCES = frozenset({"voice", "hands_free", "talk", "live", "wake", "ears"})

_PHYSICAL_HINT = (
    "That one needs a physical confirm. Press Return on the confirm card, "
    "or approve it on your phone."
)


def approval_channel_allows(row: dict[str, Any], *, source: str) -> bool:
    """True when this channel may approve the parked action in ``row``."""
    from app.agent.tools import REGISTRY
    from app.fabric.policy import physical_confirm_required, risk_class

    tool_name = str(row.get("approval_tool") or "")
    args = row.get("approval_args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    tool = REGISTRY.get(tool_name)
    permission = str(getattr(getattr(tool, "spec", None), "permission", "") or "")
    risk = risk_class(tool_name, permission, args if isinstance(args, dict) else {})
    if not physical_confirm_required(risk):
        return True
    return (source or "").strip().lower() not in _SPOKEN_SOURCES


async def handle_control(
    text: str,
    *,
    source: str = "chat",
    inline_wait: float | None = None,
) -> dict[str, Any] | None:
    """Handle status, cancel, and approval answers. None means not ours."""
    raw = (text or "").strip()
    if not raw:
        return None
    pending = runner.pending_approval()
    if pending is not None and (_affirmative(raw) or _negative(raw)):
        decision = "approve_run" if _affirmative(raw) else "deny"
        if decision != "deny" and not approval_channel_allows(pending, source=source):
            return {
                "spoken": _PHYSICAL_HINT,
                "kind": "agent_approval",
                "task_id": pending["id"],
                "physical_required": True,
            }
        if not pending.get("run_id") and str(pending.get("goal") or ""):
            local = await _decide_local_plan(str(pending["id"]), decision)
            if local is not None:
                return local
        if not _live(pending):
            ledger.update(
                str(pending["id"]),
                status="interrupted",
                error="the process running this task stopped",
            )
            return {
                "spoken": "That task is no longer running, so there is nothing to approve.",
                "kind": "agent_approval",
                "task_id": pending["id"],
            }
        runner.approve_task(str(pending["id"]), decision)
        if decision == "deny":
            runner.mark_consumed(str(pending["id"]))
            return {
                "spoken": "Okay, I stopped that task.",
                "kind": "agent_approval",
                "task_id": pending["id"],
            }
        runner.mark_approval_announced(str(pending["id"]))
        row = await runner.wait_task(str(pending["id"]), _inline(source, inline_wait))
        if runner.is_inline_finished(row):
            runner.mark_consumed(str(pending["id"]))
            return {
                "spoken": runner.inline_result_text(row),
                "kind": "agent_approval",
                "task_id": row["id"],
            }
        return {
            "spoken": runner.progress_text(row),
            "kind": "agent_approval",
            "task_id": row["id"],
        }

    if looks_like_cancel(raw):
        cancelled = runner.cancel_task()
        if cancelled is None:
            # No durable agent task; let the caller's own cancel reflex /
            # code-studio control handle it instead of claiming "nothing".
            return None
        stopped = str(cancelled.get("status") or "") == "cancelled"
        verb = "Stopped" if stopped else "Stopping"
        return {
            "spoken": f"{verb} “{str(cancelled.get('goal') or 'that task')[:100]}”.",
            "kind": "agent_cancel",
            "task_id": cancelled["id"],
        }

    if looks_like_status(raw):
        return {"spoken": runner.status_reply(), "kind": "agent_status"}

    return None


# --------------------------------------------------------------------------- #
# Task turns
# --------------------------------------------------------------------------- #


def _inline(source: str, override: float | None) -> float:
    if override is not None:
        return max(0.0, float(override))
    if source in {"voice", "phone"}:
        return max(0.0, float(getattr(settings, "agent_voice_inline_seconds", 1.5) or 1.5))
    return max(0.0, float(getattr(settings, "agent_chat_inline_seconds", 30.0) or 30.0))


def _live(row: dict[str, Any]) -> bool:
    """A task is controllable while its owning process is alive.

    The run handle is process-local, so a decision from the other API process
    is written to the ledger and applied by the owner's mirror.
    """
    run_id = str(row.get("run_id") or "")
    if run_id and runner.SERVICE.get(run_id) is not None:
        return True
    return ledger.pid_alive(row.get("pid"))


async def _decide_local_plan(task_id: str, decision: str) -> dict[str, Any] | None:
    """Approve/deny a parked no-brain local plan. None means not ours."""
    row = ledger.get(task_id)
    if row is None or row.get("run_id"):
        return None
    goal = str(row.get("goal") or "")
    if not goal:
        return None
    if decision == "deny":
        ledger.update(task_id, status="cancelled", error="denied by owner", notified=1)
        return {
            "spoken": "Okay, I stopped that task.",
            "kind": "agent_approval",
            "task_id": task_id,
        }
    from app.ev.task_agent import run_task as _run_deterministic_task

    ledger.update(task_id, status="running")
    result = await _run_deterministic_task(goal, approved=True)
    ledger.update(
        task_id,
        status="completed" if result.get("ok") else "failed",
        summary=str(result.get("spoken") or ""),
        error=None if result.get("ok") else str(result.get("error") or "step_failed"),
        notified=1,
    )
    return {
        "spoken": str(result.get("spoken") or "Done."),
        "kind": "agent_approval",
        "task_id": task_id,
        "ok": bool(result.get("ok")),
    }


#: Durable-run errors that mean "the brain never decided", not "the work
#: was attempted and failed". Only these may fall back to deterministic
#: local execution; anything else keeps its honest failure.
_BRAIN_DARK_ERRORS = frozenset({
    "egress_denied",
    "brain_unavailable",
    "brain_timeout",
    "brain_error",
})


async def _brain_dark_local_fallback(
    goal: str, row: dict[str, Any], *, source: str, approved: bool | None
) -> dict[str, Any] | None:
    """Execute local work deterministically after a brain-dark durable death.

    A run that failed before its first tool call because the brain was
    unreachable must not dead-end local work the Mac can do itself: retry
    it through the deterministic task agent and return that real result.
    Returns None (keeping the original honest failure) unless every guard
    holds: failed status, brain-dark error, zero tool calls (nothing acted
    yet, so nothing can double-apply), local work, and a deterministic
    run that itself succeeded or parked a real plan.
    """
    if str(row.get("status") or "") != "failed":
        return None
    if str(row.get("error") or "") not in _BRAIN_DARK_ERRORS:
        return None
    try:
        if int(row.get("tool_calls") or 0) != 0:
            return None
    except (TypeError, ValueError):
        return None
    local = await _run_local_without_brain(
        goal, source=source, approved=approved, force=True
    )
    if local is None or not (local.get("ok") or local.get("needs_confirm")):
        return None
    local["brain_fallback"] = str(row.get("error") or "")
    return local


async def _run_local_without_brain(
    raw: str, *, source: str, approved: bool | None, force: bool = False
) -> dict[str, Any] | None:
    """Local machine work with no cloud brain runs here, synchronously.

    Shared-agent note: this is the no-"sent to backend" guarantee. Do not
    remove the deterministic path or make it brain-dependent; improve the
    planner (task_agent) instead. Parked rows carry no run_id — that is
    how _decide_local_plan tells them apart from durable runs.


    Submitting a durable run that can only fail with brain_unavailable is
    the "sent to the backend and never happens" dead end. The deterministic
    task agent executes files, apps, terminal, and web steps on this Mac
    with no model calls, so local work never needs the queue to do real
    work. Returns None when the durable lane should run instead.

    ``force`` skips the availability re-check: the caller already watched a
    durable run die brain-dark, which proves unreachability that key-only
    (network-free) resolution cannot see.
    """
    from app.cognitive.local_route import looks_like_local_work

    if not looks_like_local_work(raw):
        return None
    if not force:
        # Same seam the runner submits through (service.resolve_agent_brain),
        # so a test double or provider override cannot route deterministic
        # here while the durable lane would have run — or vice versa.
        from app.agent import service

        try:
            choice = service.resolve_agent_brain()
        except Exception:
            choice = None
        if choice is not None and choice.available and not choice.degraded:
            return None
    from app.ev.task_agent import run_task as _run_deterministic_task

    if source in {"voice", "phone"} and not approved:
        from app.ev.task_agent import describe_plan, plan_task

        plan = plan_task(raw)
        if plan.get("mutating"):
            import os

            from app.agent.runner import BOOT_ID

            parked = ledger.create(
                goal=raw,
                source=source,
                pid=os.getpid(),
                boot_id=BOOT_ID,
                status="awaiting_approval",
            )
            return {
                "spoken": describe_plan(plan),
                "kind": "agent_task",
                "task_id": parked["id"],
                "ok": False,
                "needs_confirm": True,
                "confirm": True,
                "plan": plan,
            }
    # Chat runs unapproved here ON PURPOSE (do not "fix" into a park): the owner
    # is present at the keyboard and the deterministic plan is fixed and
    # visible per-step in receipts. The durable lane parks instead because a
    # model acts there with unpredictable steps. Voice/phone park above
    # because there is no screen to show the plan on.
    result = await _run_deterministic_task(raw, approved=True)
    return {
        "spoken": str(result.get("spoken") or "Done."),
        "kind": "agent_task",
        "task_id": "",
        "ok": bool(result.get("ok")),
        "local": True,
        "receipts": result.get("receipts") or [],
    }


async def handle_task(
    text: str,
    *,
    source: str = "chat",
    approved: bool | None = None,
    inline_wait: float | None = None,
    force: bool = False,
    context: str = "",
) -> dict[str, Any] | None:
    """Submit a general goal to the durable runner. None means not a task.

    ``force`` is the post-turn safety net: when the model has already claimed
    deferred work it never started, the goal is submitted even if the
    conservative classifier would have let it through to another path.
    ``context`` carries the recent exchange so a pronoun follow-up still has
    its referent in the agent's prompt.
    """
    raw = (text or "").strip()
    if not force and await route_goal(raw, source=source) != "task":
        return None
    if force and not raw:
        return None
    local = await _run_local_without_brain(raw, source=source, approved=approved)
    if local is not None:
        return local
    row = runner.submit_task(
        raw,
        source=source,
        approved=approved,
        announce_final=True,
        context=context,
    )
    if row.get("status") in ledger.FINAL_STATUSES:
        # start failure (brain unavailable, bad root) is reported immediately
        fallback = await _brain_dark_local_fallback(
            raw, row, source=source, approved=approved
        )
        runner.mark_consumed(str(row["id"]))
        if fallback is not None:
            return fallback
        return {
            "spoken": runner.inline_result_text(row),
            "kind": "agent_task",
            "task_id": row["id"],
            "ok": False,
        }
    row = await runner.wait_task(str(row["id"]), _inline(source, inline_wait))
    if runner.is_inline_finished(row):
        if str(row.get("status") or "") == "failed":
            fallback = await _brain_dark_local_fallback(
                raw, row, source=source, approved=approved
            )
            if fallback is not None:
                runner.mark_consumed(str(row["id"]))
                return fallback
        runner.mark_consumed(str(row["id"]))
        return {
            "spoken": runner.inline_result_text(row),
            "kind": "agent_task",
            "task_id": row["id"],
            "ok": row.get("status") in {"completed", "needs_input"},
        }
    if row.get("status") == "awaiting_approval":
        runner.mark_approval_announced(str(row["id"]))
    return {
        "spoken": runner.progress_text(row),
        "kind": "agent_task",
        "task_id": row["id"],
        "ok": True,
    }
