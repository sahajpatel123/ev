"""Local agent CLI: ``python -m app.agent "goal"``.

Runs the agent in-process — no API server required. Approval is interactive on
a TTY (approve this call / approve the whole run / deny); in a pipe it denies
unless ``--yes`` is passed. Exit code 0 only when the run completed and every
executed step succeeded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.agent.brain import brain_status, resolve_agent_brain
from app.agent.loop import (
    AgentLimits,
    AgentResult,
    ApprovalGate,
    ApprovalRequest,
    ApproveGate,
    CallbackGate,
    CancelToken,
    DenyGate,
    run_agent,
)


def _interactive_gate(reader=input) -> CallbackGate:
    def ask(request: ApprovalRequest) -> str:
        print(f"\nApproval needed: {request.reason}")
        print(f"  tool: {request.tool}")
        print(f"  args: {json.dumps(request.arguments, default=str)[:400]}")
        while True:
            try:
                answer = reader("  [a]pprove call / [r]un everything / [d]eny > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return "deny"
            if answer in {"a", "approve", "y", "yes"}:
                return "approve"
            if answer in {"r", "run"}:
                return "approve_run"
            if answer in {"d", "deny", "n", "no", ""}:
                return "deny"
            print("  please answer a, r, or d")

    return CallbackGate(ask)


def _printer(event: dict[str, Any]) -> None:
    kind = str(event.get("kind") or "")
    if kind == "run_started":
        brain = event.get("brain") or {}
        print(
            f"brain: {brain.get('provider')}/{brain.get('model')} "
            f"({'degraded' if brain.get('degraded') else 'live'})  "
            f"root: {event.get('root')}"
        )
    elif kind == "brain_turn" and event.get("text"):
        print(f"  ... {str(event['text'])[:200]}")
    elif kind == "tool_result":
        state = "ok" if event.get("ok") else "FAILED"
        approval = event.get("approval")
        suffix = f" approval={approval}" if approval and approval != "n/a" else ""
        error = f" — {event.get('error')}" if event.get("error") else ""
        print(f"  [{event.get('policy')}] {event.get('tool')}: {state}{suffix}{error}")


def _summarize(result: AgentResult) -> None:
    print()
    print(result.summary or "(no summary)")
    print()
    print(f"status: {result.status}   ok: {result.ok}   steps: {len(result.steps)}")
    if result.error:
        print(f"error: {result.error}")
    if result.needs_input and result.question:
        print(f"question: {result.question}")
    for step in result.steps:
        detail = f" — {step.error}" if step.error else ""
        print(f"  {step.index:>2}. {step.tool} [{step.policy}/{step.approval}]"
              f" {'ok' if step.ok else 'FAILED'}{detail}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.agent",
        description="Run one goal with Evie's local agent (shell, files, web, apps).",
    )
    parser.add_argument("goal", nargs="*", help="the goal; reads stdin when omitted")
    parser.add_argument("--root", default=None, help="workspace root (default: EV_AGENT_ROOT or ~)")
    parser.add_argument("--yes", "-y", action="store_true", help="approve every mutating action")
    parser.add_argument(
        "--provider",
        default=None,
        help="auto | deepseek | openrouter | xai | deepseek-direct | openai | mimo | local | echo",
    )
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--max-seconds", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="print the full run receipt as JSON")
    parser.add_argument("--brains", action="store_true", help="print brain availability and exit")
    return parser


async def _run(args: argparse.Namespace) -> int:
    goal = " ".join(args.goal).strip()
    if not goal and not sys.stdin.isatty():
        goal = sys.stdin.read().strip()
    if not goal:
        print("error: no goal given", file=sys.stderr)
        return 2

    limits = AgentLimits.from_settings()
    if args.max_steps is not None:
        limits.max_steps = max(1, min(int(args.max_steps), 64))
    if args.max_seconds is not None:
        limits.max_seconds = max(5, min(int(args.max_seconds), 3600))

    gate: ApprovalGate | None
    if args.yes:
        gate = ApproveGate()
    elif sys.stdin.isatty():
        gate = _interactive_gate()
    else:
        gate = DenyGate()

    result = await run_agent(
        goal,
        root=args.root,
        approved=True if args.yes else None,
        limits=limits,
        gate=gate,
        cancel=CancelToken(),
        on_event=None if args.json else _printer,
    )
    if args.json:
        print(json.dumps(result.to_dict(), default=str, indent=2, ensure_ascii=False))
    else:
        _summarize(result)
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    from app.agent.env import prime_agent_env

    prime_agent_env()
    args = _build_parser().parse_args(argv)
    if args.brains:
        print(json.dumps(brain_status(), indent=2))
        return 0
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130


__all__ = ["main", "resolve_agent_brain"]
