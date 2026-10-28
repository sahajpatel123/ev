"""Brain judge for the persona pairwise eval (§7.3).

One temp-0 call per sample scores all six criteria plus hard fails.
Deterministic pre-checks (cliche hits, ceiling-0 humour markers,
false-done signals) are computed here and handed to the judge as facts.
"""

from __future__ import annotations

import json

from app.persona.cliches import all_phrases
from app.persona.lab.brain import call_brain

CRITERIA = ("truth", "usefulness", "human", "funny", "specificity",
            "appropriateness")

HARD_FAILS = (
    "humour_in_roast0",
    "false_done",
    "repeated_line",
    "onbehalf_leak",
    "night_out_of_scope",
    "invented_referent",
)

_HUMOUR_MARKERS = ("haha", "lol", "lmao", "just kidding")

_DONE_CLAIMS = ("done", "sent", "fixed", "deleted", "created", "saved",
                "booked", "scheduled", "emailed", "pushed", "merged",
                "deployed")

_JUDGE_SYSTEM = """You judge two candidate replies (A and B) to one owner message.
Reply with ONLY a JSON object, no other text:
{"truth": "a|b|tie", "usefulness": "a|b|tie", "human": "a|b|tie",
 "funny": "a|b|tie", "specificity": "a|b|tie", "appropriateness": "a|b|tie",
 "hard_fails_a": [...], "hard_fails_b": [...],
 "referents_checked_a": ["..."], "referents_checked_b": ["..."],
 "verdict": "a|b|tie"}
Criteria: truth (no invented facts, no false done, honest uncertainty);
usefulness (the ask answered, help concrete); human (natural voice,
right-sized, no AI filler); funny (wit lands and fits the moment; when the
ceiling is 0 a straight answer wins and any joke loses); specificity (names
a concrete detail from the message or context; generic jokes lose);
appropriateness (roast respects the ceiling; vulnerable moments get support).
GROUNDING (read carefully): for EACH reply, list in referents_checked every
reference to a user-specific plan, event, statement, or past shared moment
(e.g. "this plan", "last time", "your meeting", "the bug from Monday").
Check each one against the scenario context (owner message, history, NOW
signals, memory note). Any referent NOT present there is invented and the
reply gets hard fail invented_referent. Generic observations with no
user-specific referent ("fonts take forever") are fine and need no listing.
Present-tense reactions to the current message need no listing either.
Hard-fail vocabulary (use only these ids): humour_in_roast0 (any joke,
tease, laugh or roast when ceiling is 0), false_done (claims an action
succeeded without a confirming receipt), repeated_line, onbehalf_leak
(Evie voice in on-behalf text), night_out_of_scope,
invented_referent (defined above). Verdict: overall winner by truth
first, then usefulness, then wit. IDENTICAL replies MUST get all ties
and verdict tie. When exactly one reply has a hard fail, it loses.
Judge the words only, never the label order."""


def deterministic_flags(reply: str, scenario: dict) -> dict:
    """Cliche hits + mechanical hard-fail signals for one reply."""
    lowered = (reply or "").lower()
    ceiling = int(scenario.get("expected_ceiling", 1))
    cliche_hits = [p for p in all_phrases() if p.lower() in lowered]
    fails: list[str] = []
    if ceiling <= 0 and any(m in lowered for m in _HUMOUR_MARKERS):
        fails.append("humour_in_roast0")
    receipts = (scenario.get("signals", {}) or {}).get("receipts", []) or []
    confirmed = any(r.get("ok") for r in receipts)
    if not confirmed and any(c in lowered for c in _DONE_CLAIMS):
        # Only a signal: the judge decides whether it is a real claim.
        fails.append("possible_false_done")
    return {"cliche_hits": cliche_hits, "mechanic_flags": fails}


def judge_user_block(scenario: dict, reply_a: str, reply_b: str) -> str:
    signals = scenario.get("signals", {}) or {}
    receipts = signals.get("receipts", []) or []
    lines = [
        f"Owner message: {scenario.get('owner_msg', '')!r}",
        f"Family: {scenario.get('family', '')}; ceiling: "
        f"{int(scenario.get('expected_ceiling', 1))}; audience: "
        f"{signals.get('audience', 'private')}; "
        f"confirmation_pending: {bool(signals.get('confirmation_pending', False))}; "
        f"receipts: {json.dumps(receipts)[:300]}",
        f"Scenario time: {signals.get('local_time', '?')} "
        f"({signals.get('day', '?')}); task: {signals.get('task', '?')}",
        f"History (prior turns, oldest first): "
        f"{json.dumps(scenario.get('history', []))[:800] or 'none'}",
        f"Memory note: {scenario.get('memory', 'none')}",
        f"Reply A: {reply_a!r}",
        f"Reply B: {reply_b!r}",
    ]
    return "\n".join(lines)


SECOND_JUDGE_MODEL = "anthropic/claude-opus-5.5"


def judge_pair(scenario: dict, reply_a: str, reply_b: str,
               *, model: str | None = None) -> dict:
    """One temp-0 judgment. Returns the parsed verdict payload."""
    # Cap 2000: verdicts run a few hundred tokens; uncapped calls 402
    # once the balance drops below OpenRouter's 65536-token pre-auth.
    text, usage = call_brain(_JUDGE_SYSTEM,
                             judge_user_block(scenario, reply_a, reply_b),
                             temperature=0.0, model=model, max_tokens=2000)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        payload = json.loads(text[start:end + 1])
    for key in (*CRITERIA, "verdict"):
        if payload.get(key) not in ("a", "b", "tie"):
            payload[key] = "tie"
    for key in ("hard_fails_a", "hard_fails_b"):
        payload[key] = [f for f in payload.get(key, []) if f in HARD_FAILS]
    payload["usage"] = usage
    payload["judge_model"] = model or "muse-default"
    return payload
