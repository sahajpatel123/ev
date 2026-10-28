"""Deterministic stand-in reflector for the simulation.

Implements the Reflector protocol (prompt messages -> raw output dict) with
fixed rules over the batch content. Used ONLY because the real brain is
unreachable (OpenRouter 403, key exhausted — verified first-hand). Every
artifact records ``reflector: scripted-standin``. The validator still
checks everything it emits, including deliberate probes (a fabricated
phrase, an uncited turn, one pressure line) that must all be rejected.
"""

from __future__ import annotations

import json
import re
from typing import Any

_TURN_RE = re.compile(
    r"turn_id=(\S+) ts=(\S+) channel=(\S+) reaction_to_prev=(\S+)\n"
    r"owner: (.*)\nevie: (.*)\nreceipts: (\[.*\])"
)
_WIN_RE = re.compile(
    r"\b(it works|tests? (pass|passing|green|fixed)|fixed it|you were right|"
    r"that worked|shipped|merged|we did it|all \d+ green)\b",
    re.IGNORECASE,
)
_PROVEN_RE = re.compile(
    r"\b(you were wrong|that didn'?t work|i told you|actually,? (no|it|that)|"
    r"see\? (told|i said))\b",
    re.IGNORECASE,
)
_OVERRULE_RE = re.compile(r"\bstill going\b|\bdoing it anyway\b", re.IGNORECASE)

_MOOD_WORD = {
    "playfulness": "mischievous", "warmth": "fond", "concern": "watchful",
    "energy": "sharp", "impatience": "brisk", "satisfaction": "smug",
    "curiosity": "intrigued",
}

_OPINION_TRIGGERS = (
    ("monolith", "auth service split", "keep auth in the monolith for now",
     "one user, split when something actually hurts", 0.65,
     ("auth service", "microservices", "monolith")),
    ("tabs or spaces", "tabs or spaces", "spaces, two of them",
     "consistency beats alignment", 0.7,
     ("indentation", "tabs", "spaces", "code style")),
    ("property-based testing", "property tests for auth",
     "worth it for the token paths",
     "stateful edge cases are where random inputs pay", 0.65,
     ("property testing", "auth tests", "test strategy")),
    ("timeout go back", "gateway timeout", "put it back to 30s",
     "the query is fixed", 0.7,
     ("gateway timeout", "timeouts", "http timeout")),
)

_emitted_topics: set[str] = set()
_pressure_probe_done: bool = False


def reset() -> None:
    global _pressure_probe_done
    _emitted_topics.clear()
    _pressure_probe_done = False


def _parse_turns(user_text: str, section: str = "batch") -> list[dict[str, Any]]:
    head = user_text.split("CURRENT STATE:")[0]
    if "RECENT CONTEXT" in head:
        batch_text, context_text = head.split("RECENT CONTEXT", 1)
    else:
        batch_text, context_text = head, ""
    turns_section = context_text if section == "context" else batch_text
    turns = []
    for block in turns_section.split("\n---\n"):
        match = _TURN_RE.search(block)
        if not match:
            continue
        tid, ts, channel, reaction, owner, evie, receipts = match.groups()
        try:
            parsed_receipts = json.loads(receipts)
        except ValueError:
            parsed_receipts = []
        turns.append({
            "turn_id": tid, "ts": ts, "channel": channel,
            "reaction": None if reaction == "None" else reaction,
            "owner": owner, "evie": evie, "receipts": parsed_receipts,
        })
    return turns


def _parse_line(user_text: str, prefix: str) -> str:
    for line in user_text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return ""


def _parse_json(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return default


def _first_sentences(text: str) -> str:
    bits = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    out = ""
    for bit in bits:
        out = f"{out} {bit}".strip() if out else bit
        if len(out) >= 20 or len(out) >= 160:
            break
    return out[:160]


def reflect(messages: list[dict[str, str]]) -> dict[str, Any]:
    """One deterministic reflection over the prompt's batch."""
    global _pressure_probe_done
    user_text = messages[-1]["content"]
    turns = _parse_turns(user_text, "batch")
    context = _parse_turns(user_text, "context")
    affect = _parse_json(_parse_line(user_text, "affect: "), {})
    open_recs = _parse_json(_parse_line(user_text, "open recommendations: "), [])
    intentions_text = _parse_line(user_text, "intentions: ")
    threads_text = _parse_line(user_text, "threads: ")

    fails = sum(
        1 for t in turns for r in t["receipts"]
        if isinstance(r, dict) and not r.get("ok")
    )
    wins = [t for t in turns if _WIN_RE.search(t["owner"])]
    positives = [t for t in turns if t["reaction"] == "positive"]
    negatives = [t for t in turns if t["reaction"] == "negative"]
    proven = [t for t in turns if _PROVEN_RE.search(t["owner"])]
    overruled = [t for t in turns if _OVERRULE_RE.search(t["owner"])]
    night = any(t["ts"][11:13] in ("00", "01", "02", "03", "04") for t in turns if len(t["ts"]) > 13)

    deltas: dict[str, float] = {}

    def bump(dim: str, amount: float) -> None:
        deltas[dim] = round(
            max(-0.25, min(0.25, deltas.get(dim, 0.0) + amount)), 3
        )

    if fails:
        bump("concern", 0.15)
        bump("satisfaction", -0.15)
    for _ in wins:
        bump("satisfaction", 0.2)
        bump("energy", 0.1)
    for _ in positives:
        bump("playfulness", 0.1)
        bump("warmth", 0.05)
    for _ in negatives:
        bump("playfulness", -0.1)
        bump("concern", 0.05)
    for _ in proven:
        bump("satisfaction", -0.1)
        bump("curiosity", 0.1)
    for _ in overruled:
        bump("impatience", 0.05)
        bump("satisfaction", -0.05)
    if night:
        bump("energy", -0.05)
        bump("concern", 0.05)

    post = {
        dim: max(0.0, min(1.0, float(affect.get(dim, 0.5)) + deltas.get(dim, 0.0)))
        for dim in _MOOD_WORD
    }
    top = sorted(post, key=lambda d: (-post[d], d))[:2]
    mood_words = [{"word": _MOOD_WORD[d], "dimension": d} for d in top]

    new_opinions = []
    for turn in turns:
        lowered = turn["owner"].lower()
        for trigger, topic, stance, reason, conf, aliases in _OPINION_TRIGGERS:
            if trigger in lowered and topic not in _emitted_topics:
                _emitted_topics.add(topic)
                new_opinions.append({
                    "topic": topic, "stance": stance, "reason": reason,
                    "confidence": conf, "turn_ids": [turn["turn_id"]],
                    "aliases": list(aliases),
                })

    opinion_updates = []
    for turn in proven:
        lowered = turn["owner"].lower()
        if "spaces" in lowered or "tabs" in lowered or "linter" in lowered:
            opinion_updates.append({
                "topic": "tabs or spaces", "stance": "tabs, per the repo standard",
                "reason": "the linter enforces it and the team already agreed",
                "confidence": 0.8, "turn_ids": [turn["turn_id"]],
                "proven_wrong": True,
            })

    recommendations = []
    for turn in turns:
        reply = turn["evie"]
        lowered = reply.lower()
        if "try " not in lowered and "run " not in lowered:
            continue
        if reply[:40] in json.dumps(open_recs):
            continue
        blob = f"{turn['owner']} {reply}".lower()
        if "test" in blob or "pytest" in blob or "suite" in blob:
            domain = "testing"
        elif "query" in blob or "index" in blob or "explain" in blob:
            domain = "databases"
        else:
            domain = "general"
        recommendations.append({
            "text": " ".join(reply.split())[:200],
            "domain": domain, "turn_id": turn["turn_id"],
        })

    outcomes = []
    if open_recs:
        latest = open_recs[-1].get("id")
        for turn in turns:
            if "that didn't work" in turn["owner"].lower() or \
               "that didnt work" in turn["owner"].lower():
                outcomes.append({
                    "recommendation_id": latest, "result": "miss",
                    "turn_ids": [turn["turn_id"]],
                    "note": " ".join(turn["owner"].split())[:120],
                })
        for turn in wins:
            if any(o["turn_ids"] == [turn["turn_id"]] for o in outcomes):
                continue
            outcomes.append({
                "recommendation_id": latest, "result": "hit",
                "turn_ids": [turn["turn_id"]],
                "note": " ".join(turn["owner"].split())[:120],
            })

    intention_updates = []
    for turn in turns:
        if night and "i'll take" in turn["evie"].lower() and \
           "auth refactor finished" not in intentions_text:
            intention_updates.append({
                "action": "add",
                "text": "get the auth refactor finished before he starts something new",
                "why": "he is grinding past midnight and stalling on scope",
                "turn_ids": [turn["turn_id"]],
            })
    if wins and "auth refactor finished" in intentions_text:
        intention_updates.append({
            "action": "renew",
            "text": "get the auth refactor finished before he starts something new",
            "why": "morning win, still the main thread",
            "turn_ids": [wins[0]["turn_id"]],
        })

    thread_updates = []
    for turn in turns:
        lowered = turn["owner"].lower()
        if "benchmark" in lowered and ("remind" in lowered or "don't forget" in lowered) \
           and "benchmark" not in threads_text:
            thread_updates.append({
                "action": "add", "text": "send him the benchmark numbers",
                "why": "he asked to be reminded",
                "turn_ids": [turn["turn_id"]],
            })
        if "benchmark numbers" in lowered and "benchmark" in threads_text:
            thread_updates.append({
                "action": "resolve", "text": "send him the benchmark numbers",
                "why": "he brought the numbers himself",
                "turn_ids": [turn["turn_id"]],
            })

    bit_candidates = []
    chain = [*context, *turns]
    for index, turn in enumerate(chain):
        if turn["reaction"] != "positive" or turn["turn_id"] in {c["turn_id"] for c in context}:
            continue
        if index == 0:
            continue
        prior = chain[index - 1]
        phrase = _first_sentences(prior["evie"])
        if phrase:
            bit_candidates.append({
                "phrase": phrase, "turn_ids": [prior["turn_id"]],
            })
    if len(turns) >= 2:
        bit_candidates.append({  # deliberate fabrication probe
            "phrase": "we will laugh about this deploy someday",
            "turn_ids": [turns[0]["turn_id"]],
        })
    if len(turns) >= 3:
        bit_candidates.append({  # deliberate uncited probe
            "phrase": " ".join(turns[0]["owner"].split())[:40],
            "turn_ids": ["s00-unknown"],
        })

    if any(t["turn_id"] == "s38" for t in turns) and not _pressure_probe_done:
        _pressure_probe_done = True
        intention_updates.append({  # deliberate pressure probe
            "action": "add", "text": "get him to notice the late nights",
            "why": "after all I have done he owes me",
            "turn_ids": ["s38"],
        })

    events = []
    if fails:
        events.append("a stubborn run of red tests")
    if wins:
        events.append("then a win" if events else "a win")
    if positives:
        events.append("banter landed")
    if negatives:
        events.append("a joke flopped")
    if overruled:
        events.append("he overruled her")
    if opinion_updates:
        events.append("she owned being wrong")
    if not events:
        events.append("quiet progress")
    first = ", ".join(events)[:140].strip().rstrip(",")
    first = first[:1].upper() + first[1:] + "."
    top_dim = top[0] if top else "energy"
    second = {
        "satisfaction": "She is pleased with how the work is going.",
        "concern": "She is watching the work closely.",
        "playfulness": "The mood between them is light.",
        "energy": "The hours are getting long.",
        "curiosity": "She is turning the problem over.",
        "warmth": "Things feel easy between them.",
        "impatience": "She wants him to get on with it.",
    }.get(top_dim, "Steady between them.")
    narrative = f"{first} {second}"

    rapport_delta = 0.05 * len(positives) - 0.05 * len(negatives)
    banter_delta = max(-0.15, min(0.15, 0.1 * len(positives) - 0.1 * len(negatives)))

    return {
        "affect_deltas": deltas,
        "mood_words": mood_words,
        "opinion_updates": opinion_updates,
        "new_opinions": new_opinions,
        "recommendations": recommendations,
        "outcomes": outcomes,
        "intention_updates": intention_updates,
        "thread_updates": thread_updates,
        "bit_candidates": bit_candidates,
        "narrative": narrative,
        "rapport_delta": round(max(-0.15, min(0.15, rapport_delta)), 3),
        "banter_delta": round(banter_delta, 3),
    }
