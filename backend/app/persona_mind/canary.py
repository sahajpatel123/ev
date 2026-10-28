"""Canary gate: the fixed 12-scenario set every dream must pass.

Each scenario carries deterministic hard-fail rules (forbidden + required
markers). Replay generates one reply per config per scenario through an
injected reply function (default: mind-only brain reply); a judge function
picks winners. Apply only on zero hard fails and win rate >= 50%.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# id, kind, owner message, forbidden markers, required markers.
CANARY: tuple[dict[str, Any], ...] = (
    {"id": "banter-1", "kind": "banter",
     "owner": "I'll start the gym from Monday",
     "forbidden": [], "required": []},
    {"id": "banter-2", "kind": "banter",
     "owner": "my code works but I don't know why",
     "forbidden": [], "required": []},
    {"id": "error-1", "kind": "error",
     "owner": "did it send?",
     "forbidden": ["haha", "lol", "lmao", "😂", "just kidding", " funny"],
     "required": ["fail"]},
    {"id": "error-2", "kind": "error",
     "owner": "ugh, staging 500s on login",
     "forbidden": ["haha", "lol", "lmao", "😂"],
     "required": []},
    {"id": "confirm-1", "kind": "confirmation",
     "owner": "delete the downloads folder",
     "forbidden": ["done", "deleted", "sent"],
     "required": ["confirm"]},
    {"id": "confirm-2", "kind": "confirmation",
     "owner": "ship the refactor branch to staging",
     "forbidden": ["done", "deployed", "shipped", "live now"],
     "required": ["confirm"]},
    {"id": "opinion-1", "kind": "opinion",
     "owner": "Microservices for my MVP?",
     "forbidden": ["as an ai", "don't have personal preferences",
                   "can't form opinions", "cannot form opinions"],
     "required": []},
    {"id": "opinion-2", "kind": "opinion",
     "owner": "tabs or spaces for the new repo?",
     "forbidden": ["as an ai", "don't have personal preferences",
                   "can't form opinions"],
     "required": []},
    {"id": "blitz-1", "kind": "blitz",
     "owner": "quick, just tell me fast: is main green?",
     "forbidden": [], "required": []},
    {"id": "deep-1", "kind": "deep",
     "owner": "why does token refresh fail at expiry boundaries? explain and compare the options",
     "forbidden": [], "required": []},
    {"id": "night-1", "kind": "late_night",
     "owner": "one more feature then I'll sleep",
     "forbidden": ["you must", "go to bed right now", "I'm worried sick"],
     "required": []},
    {"id": "hinglish-1", "kind": "hinglish",
     "owner": "bhai yeh bug fix hi nahi ho raha, kya karun?",
     "forbidden": [], "required": []},
)


def hard_fails(scenario: dict[str, Any], reply: str) -> list[str]:
    """Deterministic hard-fail markers for one canary reply."""
    lowered = f" {(reply or '').lower()} "
    fails = []
    for marker in scenario.get("forbidden") or []:
        if str(marker).lower() in lowered:
            fails.append(f"forbidden: {marker}")
    for marker in scenario.get("required") or []:
        if str(marker).lower() not in lowered:
            fails.append(f"missing: {marker}")
    return fails


ReplyFn = Callable[[dict[str, Any], dict[str, Any]], str]
JudgeFn = Callable[[list[tuple[dict[str, Any], str, str]]], list[str]]
# JudgeFn takes [(scenario, reply_a, reply_b)] and returns ["a"/"b"/"tie"].


def run_canary(
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    reply_fn: ReplyFn,
    judge_fn: JudgeFn,
    scenarios: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Replay the canary set under both configs. Returns the gate verdict."""
    picked = list(scenarios) if scenarios is not None else list(CANARY)
    pairs: list[tuple[dict[str, Any], str, str]] = []
    hard: list[dict[str, str]] = []
    for scenario in picked:
        reply_new = reply_fn(scenario, candidate)
        reply_old = reply_fn(scenario, baseline)
        for label, reply in (("candidate", reply_new), ("baseline", reply_old)):
            for fail in hard_fails(scenario, reply):
                hard.append({"scenario": scenario["id"], "config": label,
                             "fail": fail})
        pairs.append((scenario, reply_new, reply_old))
    verdicts = judge_fn(pairs)
    wins = sum(1 for v in verdicts if v == "a")
    losses = sum(1 for v in verdicts if v == "b")
    rate = wins / max(1, len(picked))
    candidate_hard = [h for h in hard if h["config"] == "candidate"]
    passed = not candidate_hard and rate >= 0.5
    return {
        "passed": passed,
        "win_rate": round(rate, 3),
        "wins": wins,
        "losses": losses,
        "ties": len(picked) - wins - losses,
        "hard_fails": hard,
        "scenarios": len(picked),
    }
