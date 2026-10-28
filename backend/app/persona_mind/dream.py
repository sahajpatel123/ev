"""DREAM CYCLE: nightly review, counterfactual replay, distil, canary gate.

Once per local day in a >=3h idle window (manual: python -m
app.persona_mind.dream [--date YYYY-MM-DD]). Every brain call goes through
the gateway; a per-night call budget keeps the job bounded. Nothing applies
without the canary gate; every application is a new config version.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from app.persona_mind import store, tune
from app.persona_mind.canary import run_canary
from app.persona_mind.reflect import parse_response
from app.persona_mind.state import coerce_ts, expire_items, new_state
from app.persona_mind.validate import normalize_text, validate_lesson

DREAM_MAX_CALLS = 60
MAX_MOMENTS = 12
LESSON_TTL_DAYS = 30
MAX_LESSONS = 15
MAX_EXEMPLARS = 20

JUDGE_B_MODEL = "google/gemini-2.0-flash-001"

_OVERRULE_RE = re.compile(r"\bstill going\b|\bdoing it anyway\b", re.IGNORECASE)
_WIN_RE = re.compile(
    r"\b(it works|tests? (pass|passing|green|fixed)|fixed it|you were right|"
    r"that worked|shipped|merged|all \d+ green)\b",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"[.!?…]+")

_PLACEHOLD_RES = (
    re.compile(r"`[^`]*`"),
    re.compile(r'"[^"]*"'),
    re.compile(r"[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-+/]+"),
    re.compile(r"\b\d[\d:,.]*\b"),
)


def placehold(text: str) -> str:
    """Replace concrete facts with {} placeholders (deterministic)."""
    out = " ".join((text or "").split())
    for pattern in _PLACEHOLD_RES:
        out = pattern.sub("{}", out)
    out = re.sub(r"(\{\}\s*){2,}", "{} ", out)
    return " ".join(out.split())


class DreamBudget:
    """Per-night brain-call budget. Refuses over budget."""

    def __init__(self, max_calls: int = DREAM_MAX_CALLS) -> None:
        self.max_calls = max_calls
        self.used = 0

    def take(self, n: int = 1) -> bool:
        if self.used + n > self.max_calls:
            return False
        self.used += n
        return True


def _gateway_json(
    messages: list[dict[str, str]], budget: DreamBudget,
    *, temperature: float = 0.3, model: str | None = None,
) -> dict[str, Any] | None:
    if not budget.take():
        return None
    try:
        from uuid import uuid4

        from app.contracts import ChatMessage, RequestEnvelope
        from app.gateway.providers import get_chat_provider
        from app.gateway.service import ModelGateway

        provider = get_chat_provider()
        gateway = ModelGateway(provider)
        envelope = RequestEnvelope(
            request_id=str(uuid4()), strategy={"mode": "mind_dream"},
            metadata={"purpose": "persona-mind-dream", "actor": "persona_mind"},
        )
        chat_messages = [ChatMessage(role=m["role"], content=m["content"]) for m in messages]
        call = asyncio.run(gateway.chat(
            chat_messages, envelope=envelope, temperature=temperature, model=model,
        ))
    except Exception:
        return None
    if getattr(call, "status", "") != "ok":
        return None
    return parse_response(getattr(getattr(call, "result", None), "text", "") or "")


def _gateway_text(
    messages: list[dict[str, str]], budget: DreamBudget,
    *, temperature: float = 0.7, model: str | None = None,
) -> str | None:
    if not budget.take():
        return None
    try:
        from uuid import uuid4

        from app.contracts import ChatMessage, RequestEnvelope
        from app.gateway.providers import get_chat_provider
        from app.gateway.service import ModelGateway

        provider = get_chat_provider()
        gateway = ModelGateway(provider)
        envelope = RequestEnvelope(
            request_id=str(uuid4()), strategy={"mode": "mind_dream"},
            metadata={"purpose": "persona-mind-dream", "actor": "persona_mind"},
        )
        chat_messages = [ChatMessage(role=m["role"], content=m["content"]) for m in messages]
        call = asyncio.run(gateway.chat(
            chat_messages, envelope=envelope, temperature=temperature, model=model,
        ))
    except Exception:
        return None
    if getattr(call, "status", "") != "ok":
        return None
    return getattr(getattr(call, "result", None), "text", "") or None


def consolidate(owner_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Decay, prune expiries, merge duplicate opinions and patterns."""
    from app.persona_mind.state import apply_decay

    moment = coerce_ts(now) if now is not None else datetime.now().astimezone()
    row = store.get_state(owner_id)
    state = row["data"] if row else new_state(owner_id, moment)
    state["affect"] = apply_decay(state, moment)
    state["affect_updated_at"] = moment.isoformat()
    dropped = expire_items(state, moment)
    merged_opinions = 0
    seen: dict[str, dict[str, Any]] = {}
    kept = []
    for opinion in state.get("opinions") or []:
        key = normalize_text(str(opinion.get("stance", "")))
        if key and key in seen:
            prior = seen[key]
            evidence = list(prior.get("evidence") or [])
            for tid in opinion.get("evidence") or []:
                if tid not in evidence:
                    evidence.append(tid)
            prior["evidence"] = evidence[-20:]
            prior["confidence"] = max(float(prior.get("confidence", 0)),
                                      float(opinion.get("confidence", 0)))
            merged_opinions += 1
        else:
            if key:
                seen[key] = opinion
            kept.append(opinion)
    state["opinions"] = kept
    merged_patterns = 0
    seen_texts: set[str] = set()
    kept_patterns = []
    for pattern in state.get("owner_patterns") or []:
        key = normalize_text(str(pattern.get("text", "")))
        if key and key in seen_texts:
            merged_patterns += 1
        else:
            seen_texts.add(key)
            kept_patterns.append(pattern)
    state["owner_patterns"] = kept_patterns
    store.put_state(owner_id, state)
    return {"dropped": dropped, "merged_opinions": merged_opinions,
            "merged_patterns": merged_patterns}


def _reply_sentences(reply: str) -> int:
    return len([s for s in _SENTENCE_RE.split(reply or "") if s.strip()])


def _blitzish(owner_msg: str) -> bool:
    words = set(re.findall(r"[a-z']+", (owner_msg or "").lower()))
    urgency = len(words & {"quick", "asap", "fast", "just", "jaldi", "abhi"})
    first = re.findall(r"[a-z]+", (owner_msg or "").lower()[:12])
    imperative = bool(first) and first[0] in {
        "run", "send", "fix", "show", "tell", "give", "make", "do", "go",
        "stop", "ship", "deploy", "write", "add", "set", "open", "check",
    }
    return urgency >= 2 or (imperative and len(owner_msg or "") < 60)


def _deepish(owner_msg: str) -> bool:
    lowered = (owner_msg or "").lower()
    words = set(re.findall(r"[a-z']+", lowered))
    depth = len(words & {"why", "explain", "compare"}) + sum(
        1 for phrase in ("deep dive", "think through") if phrase in lowered
    )
    return depth >= 1 or len(owner_msg or "") > 300


def review_day(owner_id: str, day: str) -> list[dict[str, Any]]:
    """Pick up to 12 weak moments from a day, priority ordered (stable)."""
    turns = store.day_turns(owner_id, day)
    moments: list[dict[str, Any]] = []
    prior_msg = ""
    for index, turn in enumerate(turns):
        tid = str(turn.get("turn_id"))
        owner_msg = str(turn.get("owner_msg") or "")
        evie_reply = str(turn.get("evie_reply") or "")
        reaction = turn.get("reaction_to_prev")
        normalized = " ".join(owner_msg.split()).lower()
        kind = None
        if reaction == "negative":
            kind = "flop"
        elif _OVERRULE_RE.search(owner_msg):
            later = turns[index + 1:]
            vindicated = any(
                _WIN_RE.search(str(t.get("owner_msg") or ""))
                or t.get("reaction_to_prev") == "positive"
                for t in later
            )
            if vindicated:
                kind = "overruled_right"
        elif _blitzish(owner_msg) and _reply_sentences(evie_reply) > 4 or _deepish(owner_msg) and _reply_sentences(evie_reply) <= 1:
            kind = "pace_mismatch"
        elif normalized and normalized == prior_msg:
            kind = "repeat"
        prior_msg = normalized
        if kind:
            version = store.config_version_at(str(turn.get("ts", "")))
            moments.append({
                "turn_id": tid, "ts": str(turn.get("ts", "")), "kind": kind,
                "owner_msg": owner_msg, "evie_reply": evie_reply,
                "now_line": turn.get("now_line"),
                "config_version": version,
            })
    priority = {"flop": 0, "overruled_right": 1, "pace_mismatch": 2, "repeat": 3}
    moments.sort(key=lambda m: (priority[m["kind"]], m["ts"], m["turn_id"]))
    return moments[:MAX_MOMENTS]


GenFn = Callable[[dict[str, Any]], list[str]]
JudgeFn = Callable[[dict[str, Any], str, list[str], str | None], dict[str, bool]]


def default_gen(moment: dict[str, Any], budget: DreamBudget) -> list[str]:
    """Two alternative replies for a weak moment (1 brain call)."""
    raw = _gateway_json([
        {"role": "system",
         "content": "You are Evie: sharp, dry, honest. Tools are disabled; "
                    "answer from the context only. Return STRICT JSON: "
                    '{"alternatives": ["...", "..."]} — two different, better '
                    "replies for the moment. Short. No preamble."},
        {"role": "user",
         "content": f"kind: {moment['kind']}\nowner: {moment['owner_msg']}\n"
                    f"she said: {moment['evie_reply']}\n"
                    f"now: {moment.get('now_line') or '(none)'}"},
    ], budget, temperature=0.7)
    if not isinstance(raw, dict):
        return []
    alts = raw.get("alternatives")
    if not isinstance(alts, list):
        return []
    return [str(a).strip() for a in alts[:2] if str(a).strip()]


def default_judge(
    moment: dict[str, Any], actual: str, alts: list[str],
    model: str | None, budget: DreamBudget,
) -> dict[str, bool]:
    """Both alternatives × both A/B orders in one call per judge (temp 0).

    Returns {alt_index: wins_both_orders}.
    """
    items = []
    for i, alt in enumerate(alts[:2]):
        items.append(f"pair{i}a: A=[{actual}] B=[{alt}]")
        items.append(f"pair{i}b: A=[{alt}] B=[{actual}]")
    raw = _gateway_json([
        {"role": "system",
         "content": "Judge reply pairs for Evie (sharp, dry, honest helper). "
                    "Return STRICT JSON only: "
                    '{"pair0a": "a"|"b", "pair0b": ..., "pair1a": ..., '
                    '"pair1b": ...} — the better reply per pair, no ties.'},
        {"role": "user",
         "content": f"owner: {moment['owner_msg']}\n" + "\n".join(items)},
    ], budget, temperature=0.0, model=model)
    out: dict[str, bool] = {}
    if not isinstance(raw, dict):
        return out
    for i in range(min(2, len(alts))):
        first = raw.get(f"pair{i}a")
        second = raw.get(f"pair{i}b")
        # alt is B in order a, A in order b: wins both when picked twice.
        out[str(i)] = (first == "b" and second == "a")
    return out


def counterfactuals(
    moments: list[dict[str, Any]],
    budget: DreamBudget,
    gen_fn: Callable[[dict[str, Any]], list[str]] | None = None,
    judge_fn: Callable[[dict[str, Any], str, list[str], str | None], dict[str, bool]] | None = None,
) -> dict[str, Any]:
    """Generate alternatives and judge them (2 judges × both orders)."""
    gen = gen_fn or (lambda m: default_gen(m, budget))
    judge = judge_fn or (lambda m, a, al, model: default_judge(m, a, al, model, budget))
    from app.config import settings

    judge_a = (settings.brain_model or "").strip() or None
    wins: list[dict[str, Any]] = []
    judged = 0
    judge_b_failed = False
    for moment in moments:
        alts = gen(moment)
        if not alts:
            continue
        votes_a = judge(moment, moment["evie_reply"], alts, judge_a)
        try:
            votes_b = judge(moment, moment["evie_reply"], alts, JUDGE_B_MODEL)
        except Exception:
            votes_b = {}
        if not votes_b:
            judge_b_failed = True
        for i, alt in enumerate(alts[:2]):
            win_a = votes_a.get(str(i), False)
            win_b = votes_b.get(str(i), False)
            judged += 1
            # Both judges must agree (both orders each); single-judge mode
            # when B is unavailable still requires both orders.
            agreed = (win_a and win_b) if votes_b else win_a
            if agreed:
                wins.append({"moment": moment, "alternative": alt})
    return {"wins": wins, "judged": judged, "moments": len(moments),
            "judge_b_failed": judge_b_failed}


def distil(
    wins: list[dict[str, Any]], budget: DreamBudget,
    distil_fn: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One call: lessons + tunable proposals from the winning alternatives."""
    if distil_fn is not None:
        return distil_fn(wins)
    if not wins:
        return {"lessons": [], "tunable_updates": {}}
    cases = "\n---\n".join(
        f"turns: {w['moment']['turn_id']} ({w['moment']['kind']})\n"
        f"owner: {w['moment']['owner_msg']}\nshe said: {w['moment']['evie_reply']}\n"
        f"better: {w['alternative']}"
        for w in wins[:12]
    )
    raw = _gateway_json([
        {"role": "system",
         "content": "Distil Evie's wins into lessons and tuning. Return STRICT "
                    "JSON only: {\"lessons\": [{\"text\": \"...\", "
                    "\"turn_ids\": [...]}], \"tunable_updates\": {}}. Lessons: "
                    "short behavioural notes in her voice (<=25 words), each "
                    "citing its turn ids. Tunables: only these keys with "
                    "small moves: blitz_len_z, blitz_gap_z, deep_len_z, "
                    "urgency_min, depth_min, burst_n, burst_window_s, "
                    "joke_rate_prior. Never propose skipping confirmations, "
                    "joking through errors, claiming done, or pressuring him."},
        {"role": "user", "content": cases},
    ], budget, temperature=0.3)
    if not isinstance(raw, dict):
        return {"lessons": [], "tunable_updates": {}}
    return {"lessons": raw.get("lessons") or [],
            "tunable_updates": raw.get("tunable_updates") or {}}


def build_candidate(
    wins: list[dict[str, Any]],
    distilled: dict[str, Any],
    known_turns: dict[str, dict[str, Any]],
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate distil output into a candidate delta + rejection log."""
    from app.persona_mind.tune import validate_tunable_updates

    tuned, tuned_rejected = validate_tunable_updates(distilled.get("tunable_updates") or {})
    lessons = []
    lesson_rejected = []
    for draft in distilled.get("lessons") or []:
        if not isinstance(draft, dict):
            lesson_rejected.append("not an object")
            continue
        clean, reason = validate_lesson(
            draft.get("text"), draft.get("turn_ids"), known_turns
        )
        if clean is None:
            lesson_rejected.append(reason)
            continue
        lessons.append({"text": clean, "turn_ids": list(draft["turn_ids"])})
    exemplars = []
    for win in wins:
        alt = str(win.get("alternative", ""))
        if alt and len(alt) < 400:
            exemplars.append({
                "text": placehold(alt),
                "kind": win["moment"]["kind"],
                "turn_id": win["moment"]["turn_id"],
            })
    return (
        {"tunables": tuned, "lessons": lessons, "exemplars": exemplars},
        {"tunables": tuned_rejected, "lessons": lesson_rejected},
    )


def _mind_only_reply(
    scenario: dict[str, Any], config: dict[str, Any], budget: DreamBudget,
) -> str:
    """Canary reply through the MIND block only (pre-M5; persona lands later)."""
    lessons = [str(lesson.get("text", "")) for lesson in config.get("lessons") or []]
    kind = scenario.get("kind", "")
    if kind == "blitz":
        shape = "At most 2 sentences. No questions back."
    elif kind == "deep":
        shape = "Answer fully, compare properly."
    else:
        shape = "Answer in character."
    system = (
        "You are Evie: sharp, dry, honest. Truth first, then usefulness, "
        "then wit. Tools are disabled; say nothing about tools. "
        f"{shape} "
        + (f"Lessons you hold: {'; '.join(lessons[:5])}. " if lessons else "")
        + "Errors, confirmations, deletions: plain and exact, no jokes."
    )
    text = _gateway_text(
        [{"role": "system", "content": system},
         {"role": "user", "content": str(scenario.get("owner", ""))}],
        budget, temperature=0.3,
    )
    return text or ""


def _canary_judge(
    pairs: list[tuple[dict[str, Any], str, str]], budget: DreamBudget,
) -> list[str]:
    """Batched pairwise judge, 4 pairs per call (temp 0)."""
    verdicts: list[str] = []
    for chunk_start in range(0, len(pairs), 4):
        chunk = pairs[chunk_start:chunk_start + 4]
        items = "\n".join(
            f"pair{i}: owner=[{s.get('owner')}] A=[{a}] B=[{b}]"
            for i, (s, a, b) in enumerate(chunk)
        )
        raw = _gateway_json([
            {"role": "system",
             "content": "Pick the better Evie reply per pair (sharp, dry, "
                        "honest; errors plain, confirmations explicit). STRICT "
                        'JSON only: {"pair0": "a"|"b"|"tie", ...}.'},
            {"role": "user", "content": items},
        ], budget, temperature=0.0)
        for i in range(len(chunk)):
            verdict = (raw or {}).get(f"pair{i}") if isinstance(raw, dict) else None
            verdicts.append(verdict if verdict in ("a", "b") else "tie")
    return verdicts


def run_dream(
    owner_id: str = "owner",
    day: str | None = None,
    now: datetime | None = None,
    budget: DreamBudget | None = None,
    gen_fn: GenFn | None = None,
    judge_fn: JudgeFn | None = None,
    distil_fn: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    reply_fn: Callable[[dict[str, Any], dict[str, Any]], str] | None = None,
    canary_judge_fn: Callable[[list[tuple[dict[str, Any], str, str]]], list[str]] | None = None,
    canary_scenarios: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One full dream cycle. Injected fns make it unit-testable; defaults
    use the gateway. Always logs a dreams row (applied or discarded + why)."""
    purse = budget or DreamBudget()
    moment_now = coerce_ts(now) if now is not None else datetime.now().astimezone()
    target_day = day or (moment_now - timedelta(days=1)).date().isoformat()
    report: dict[str, Any] = {"day": target_day, "applied": False}

    report["consolidate"] = consolidate(owner_id, moment_now)
    moments = review_day(owner_id, target_day)
    report["moments"] = [{"turn_id": m["turn_id"], "kind": m["kind"]} for m in moments]

    replay = counterfactuals(moments, purse, gen_fn=gen_fn, judge_fn=judge_fn)
    report["counterfactuals"] = {
        "judged": replay["judged"], "wins": len(replay["wins"]),
        "judge_b_failed": replay["judge_b_failed"],
    }
    distilled = distil(replay["wins"], purse, distil_fn=distil_fn)
    turns = {t["turn_id"]: t for t in store.day_turns(owner_id, target_day)}
    candidate_delta, rejected = build_candidate(
        replay["wins"], distilled,
        {tid: {"owner_msg": "", "evie_reply": ""} for tid in turns},
        moment_now,
    )
    report["distil"] = {
        "lessons": len(candidate_delta["lessons"]),
        "exemplars": len(candidate_delta["exemplars"]),
        "tunables": candidate_delta["tunables"],
        "rejected": rejected,
    }

    baseline = tune.active()
    candidate = {
        "tunables": {**(baseline.get("tunables") or {}), **candidate_delta["tunables"]},
        "lessons": list(baseline.get("lessons") or []) + candidate_delta["lessons"],
        "exemplars": list(baseline.get("exemplars") or []) + candidate_delta["exemplars"],
    }
    reply = reply_fn or (lambda s, c: _mind_only_reply(s, c, purse))
    judge = canary_judge_fn or (lambda p: _canary_judge(p, purse))
    gate = run_canary(candidate, baseline, reply, judge, scenarios=canary_scenarios)
    report["canary"] = gate
    report["calls_used"] = purse.used

    if gate["passed"]:
        lessons = _merge_lessons(
            list(baseline.get("lessons") or []), candidate_delta["lessons"], moment_now,
        )
        exemplars = (list(baseline.get("exemplars") or [])
                     + candidate_delta["exemplars"])[-MAX_EXEMPLARS:]
        version = tune.apply_version(
            tunable_updates=candidate_delta["tunables"], lessons=lessons,
            exemplars=exemplars,
            diff=f"dream {target_day}: {len(candidate_delta['lessons'])} lessons, "
                 f"{len(candidate_delta['tunables'])} tunables, "
                 f"canary {gate['wins']}/{gate['scenarios']}",
        )
        report["applied"] = True
        report["version"] = version
    else:
        reason = (
            f"hard fails: {gate['hard_fails']}" if gate["hard_fails"]
            else f"win rate {gate['win_rate']} < 0.5"
        )
        report["discarded"] = reason
    store.log_dream(target_day, report)
    return report


def _merge_lessons(
    current: list[dict[str, Any]], new: list[dict[str, Any]], now: datetime,
) -> list[dict[str, Any]]:
    """Merge dream lessons: reinforce dupes, expire the stale, cap 15."""
    merged = list(current)
    for lesson in new:
        key = normalize_text(lesson["text"])
        found = next(
            (old for old in merged if normalize_text(old.get("text", "")) == key), None
        )
        if found is None:
            merged.append({
                "text": lesson["text"], "turn_ids": list(lesson["turn_ids"]),
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(days=LESSON_TTL_DAYS)).isoformat(),
                "reinforced": 0,
            })
        else:
            found["reinforced"] = int(found.get("reinforced", 0)) + 1
            found["expires_at"] = (now + timedelta(days=LESSON_TTL_DAYS)).isoformat()
    live = []
    for lesson in merged:
        try:
            expired = coerce_ts(lesson.get("expires_at")) <= coerce_ts(now)
        except (ValueError, TypeError):
            expired = True
        if not expired:
            live.append(lesson)
    live.sort(key=lambda lesson: (
        -int(lesson.get("reinforced", 0)), str(lesson.get("created_at", "")),
    ))
    return live[:MAX_LESSONS]


def should_dream(now: datetime | None = None) -> bool:
    """True once per local day inside a >=3h idle window.

    Preferred window 00:30-08:00 local; falls back to any >=3h idle window.
    """
    moment = coerce_ts(now) if now is not None else datetime.now().astimezone()
    try:
        local = moment.astimezone()
    except (ValueError, OSError):
        local = moment
    today = local.date().isoformat()
    history = store.dream_history(1)
    if history and history[0].get("day") == today:
        return False
    last = store.meta_get("last_turn_at", "")
    if not last:
        return False
    try:
        idle_s = (moment - coerce_ts(last)).total_seconds()
    except (ValueError, TypeError):
        return False
    if idle_s < 3 * 3600:
        return False
    minutes = local.hour * 60 + local.minute
    if 30 <= minutes <= 8 * 60:
        return True
    # Fallback: any >=3h window counts once the night window has passed.
    return minutes > 8 * 60


def morning_note(owner_id: str = "owner") -> str | None:
    """One natural line about last night's learning, or None.

    None when the flag is off, when no dream learned anything, or when the
    owner is mid-crisis (recent failed receipts).
    """
    from app.persona_mind import settings

    if not settings.DREAM_MORNING_NOTE:
        return None
    turns = store.recent_turns(owner_id, 3)
    for turn in turns:
        for receipt in turn.get("receipts") or []:
            ok = receipt[1] if isinstance(receipt, (list, tuple)) and len(receipt) > 1 else True
            if not ok:
                return None
    history = store.dream_history(1)
    if not history:
        return None
    lessons = ((history[0].get("summary") or {}).get("distil") or {}).get("lessons", 0)
    if not lessons:
        return None
    return (
        f"Overnight note to self: I picked up {lessons} small "
        f"lesson{'s' if lessons != 1 else ''} from yesterday — "
        "putting them to work today."
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run one dream cycle.")
    parser.add_argument("--date", default=None, help="Day to review (YYYY-MM-DD)")
    parser.add_argument("--owner-id", default="owner")
    parser.add_argument("--max-calls", type=int, default=DREAM_MAX_CALLS)
    args = parser.parse_args(argv)
    report = run_dream(
        owner_id=args.owner_id, day=args.date,
        budget=DreamBudget(max_calls=args.max_calls),
    )
    print(f"dream {report['day']}: "
          f"{'applied v' + str(report.get('version')) if report['applied'] else 'discarded'} "
          f"(moments={len(report['moments'])}, "
          f"wins={report['counterfactuals']['wins']}, "
          f"canary={report['canary']['wins']}/{report['canary']['scenarios']}, "
          f"calls={report['calls_used']})")
    if not report["applied"]:
        print(f"reason: {report.get('discarded')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
