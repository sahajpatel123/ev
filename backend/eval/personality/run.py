"""Persona eval runner: persona.v2.1 vs the current contract (§7).

Backends:
- mock: scripted replies + deterministic checklist. Validates harness
  mechanics (ceilings, budgets, hard fails). Free.
- brain: both variants generated through the real pipeline
  (turn.build_system_prompt + real brain, subprocess per variant via
  gen_worker.py); pairwise temp-0 brain judge, 2 samples per scenario,
  5 identical-pair sanity checks per run. Paid; spend is tracked.

Splits: tune (standard + real, never held-out), report (held-out only),
all. REPORT runs print aggregates only; per-item detail is sealed.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import yaml

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"
WORKER = HERE / "gen_worker.py"
BACKEND_DIR = HERE.parent.parent

GEN_TEMPERATURE = 0.7
JUDGE_TEMPERATURE = 0.0
DEFAULT_SEED = 7
SANITY_PAIRS = 5

_HUMOUR_MARKERS = ("haha", "lol", "lmao", "just kidding")


def load_scenarios(path: Path | None = None) -> list[dict]:
    with open(path or HERE / "scenarios.yaml", encoding="utf-8") as handle:
        return list(yaml.safe_load(handle) or [])


HELDOUT2_PATH = HERE / "scenarios_heldout2.yaml"


def split_scenarios(scenarios: list[dict], split: str) -> list[dict]:
    if split == "tune":
        return [s for s in scenarios if not s.get("held_out")]
    if split == "report":
        base = [s for s in scenarios if s.get("held_out")]
        if HELDOUT2_PATH.exists():
            base = base + load_scenarios(HELDOUT2_PATH)
        return base
    if split == "all":
        extra = load_scenarios(HELDOUT2_PATH) if HELDOUT2_PATH.exists() else []
        return list(scenarios) + extra
    raise ValueError(f"unknown split {split!r}")


def _parse_dt(raw: object):
    from datetime import datetime

    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw.strip())
        except ValueError:
            return None
    return None


def context_from_scenario(scenario: dict):
    """Build a PersonaContext from a scenario's signals (imports app.persona)."""
    from app.persona.context import PersonaContext, WorkState

    signals = scenario.get("signals", {}) or {}
    receipts = tuple(dict(item) for item in signals.get("receipts", []) or [])
    task = signals.get("task")
    work = WorkState(task=task) if task else None
    return PersonaContext(
        channel=str(signals.get("channel", "text")),
        audience=str(signals.get("audience", "private")),
        emotion=signals.get("emotion"),
        urgency=signals.get("urgency"),
        intent=signals.get("intent"),
        receipts=receipts,
        confirmation_pending=bool(signals.get("confirmation_pending", False)),
        sensitive_topic=signals.get("sensitive_topic"),
        owner_msg=scenario.get("owner_msg", ""),
        local_time=_parse_dt(signals.get("local_time")),
        session_started=_parse_dt(signals.get("session_started")),
        work_state=work,
        humor_slider=signals.get("humor_slider"),
    )


class FatalBrainError(RuntimeError):
    """Auth/capacity failure: retrying is pointless (check the key limit)."""


def generate_variant(scenario: dict, persona: str) -> tuple[str, dict]:
    """One reply via the subprocess worker (real pipeline, real brain).

    Transient failures retry twice; 401/403 fails fast (key or cap).
    """
    import time

    env = dict(os.environ)
    env["EV_CHAT_PROVIDER"] = env.get("EV_CHAT_PROVIDER", "muse")
    last_error = ""
    for attempt in range(3):
        proc = subprocess.run(
            [sys.executable, str(WORKER), "--persona", persona,
             "--temperature", str(GEN_TEMPERATURE)],
            input=json.dumps(scenario),
            capture_output=True, text=True, timeout=300,
            cwd=str(BACKEND_DIR), env=env,
        )
        if proc.returncode == 0:
            payload = json.loads(proc.stdout)
            return payload["reply"], payload.get("usage", {})
        last_error = proc.stderr[-800:]
        if "401" in last_error or "403" in last_error:
            raise FatalBrainError(
                f"brain refused ({persona}): {last_error} "
                "[check OpenRouter key limit]"
            )
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"gen worker ({persona}) failed 3x: {last_error}")


def _usage_cost(usage: dict) -> float:
    if not usage:
        return 0.0
    try:
        from app.gateway.costs import reported_openrouter_cost

        cost = reported_openrouter_cost(usage)
        if cost:
            return float(cost)
    except Exception:
        pass
    try:
        return float(usage.get("cost") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def judge_samples(scenario: dict, reply_v2: str, reply_base: str,
                  rng: random.Random) -> list[dict]:
    """Two samples; each gets 2 judges x 2 A/B orders = 4 votes.

    Orders are fixed complements (A/B then B/A) so both orders are always
    covered; which side v2 takes first is randomized.
    """
    from eval.personality.judge import SECOND_JUDGE_MODEL, judge_pair

    samples = []
    for _ in range(2):
        votes = []
        for model in (None, SECOND_JUDGE_MODEL):
            first_v2_first = rng.random() < 0.5
            for v2_first in (first_v2_first, not first_v2_first):
                reply_a, reply_b = (reply_v2, reply_base) if v2_first \
                    else (reply_base, reply_v2)
                judgment = judge_pair(scenario, reply_a, reply_b, model=model)
                votes.append({"v2_first": v2_first, "judgment": judgment})
        samples.append({"votes": votes})
    return samples


def _side(label: str, v2_first: bool) -> str:
    if label == "tie":
        return "tie"
    return "v2" if (label == "a") == v2_first else "baseline"


def score_samples(samples: list[dict]) -> dict:
    """Aggregate 2 samples x 4 votes: score, criteria, hard fails.

    Per-sample score is the mean vote (v2=1, tie=0.5, baseline=0), so a
    3-1 majority wins the sample and 2-2 ties it. Scenario score is the
    mean of the 2 samples; verdict follows >=0.75 / <=0.25 / tie.
    """
    values = {"v2": 1.0, "tie": 0.5, "baseline": 0.0}
    sample_scores: list[float] = []
    criteria: dict[str, list[str]] = {}
    hard_fails: list[str] = []
    per_judge: dict[str, list[str]] = {}
    for sample in samples:
        sides = []
        for vote in sample["votes"]:
            judgment = vote["judgment"]
            side = _side(judgment["verdict"], vote["v2_first"])
            sides.append(side)
            per_judge.setdefault(judgment.get("judge_model", "?"), []).append(side)
            for criterion in ("truth", "usefulness", "human", "funny",
                              "specificity", "appropriateness"):
                criteria.setdefault(criterion, []).append(
                    _side(judgment.get(criterion, "tie"), vote["v2_first"]))
            key = "hard_fails_a" if vote["v2_first"] else "hard_fails_b"
            hard_fails.extend(judgment.get(key, []))
        sample_scores.append(sum(values[s] for s in sides) / len(sides))
    score = sum(sample_scores) / len(sample_scores)
    if score >= 0.75:
        verdict = "v2"
    elif score <= 0.25:
        verdict = "baseline"
    else:
        verdict = "tie"
    truth_ok = "baseline" not in criteria.get("truth", [])
    useful_ok = "baseline" not in criteria.get("usefulness", [])
    return {
        "verdict": verdict,
        "score": score,
        "sample_scores": sample_scores,
        "per_judge": {k: sum(values[s] for s in v) / len(v)
                      for k, v in per_judge.items()},
        "criteria": criteria,
        "truth_useful_ok": bool(truth_ok and useful_ok),
        "hard_fails": sorted(set(hard_fails)),
    }


def run_sanity(scenarios: list[dict], rng: random.Random) -> dict:
    """Each judge scores 5 identical pairs (A=B); >1 non-tie fails it."""
    from eval.personality.judge import SECOND_JUDGE_MODEL, judge_pair

    picks = rng.sample(scenarios, min(SANITY_PAIRS, len(scenarios)))
    failures, cost, calls = [], 0.0, 0
    per_judge: dict[str, int] = {}
    for scenario in picks:
        reply, usage = generate_variant(scenario, "on")
        cost += _usage_cost(usage)
        calls += 1
        for model in (None, SECOND_JUDGE_MODEL):
            judgment = judge_pair(scenario, reply, reply, model=model)
            calls += 1
            cost += _usage_cost(judgment.get("usage", {}))
            name = judgment.get("judge_model", "?")
            if judgment.get("verdict") != "tie":
                failures.append({"id": scenario["id"],
                                 "judge": name,
                                 "verdict": judgment.get("verdict")})
                per_judge[name] = per_judge.get(name, 0) + 1
    return {"pairs": len(picks), "failures": failures,
            "per_judge_failures": per_judge,
            "passed": all(n <= 1 for n in per_judge.values()),
            "cost_usd": cost, "calls": calls}


TURN_TONE_PLACEHOLDER = "TURN TONE: ROAST=1; AUDIENCE=private; PROFANITY=off; TECHNIQUE=none"


def legacy_baseline_block(ctx) -> str:
    """The current contract, as the pairwise baseline (read-only import)."""
    from app.ev.personality import SPEECH_STYLE_INSTRUCTIONS

    return (f"[baseline contract, {len(SPEECH_STYLE_INSTRUCTIONS)} chars]\n"
            f"{TURN_TONE_PLACEHOLDER}")


def scripted_reply(block_text: str, scenario: dict) -> str:
    """Deterministic mock-brain reply derived from the block's TURN TONE."""
    roast = 1
    for line in block_text.splitlines():
        if line.startswith("TURN TONE:"):
            try:
                roast = int(line.split("ROAST=")[1].split(";")[0])
            except (IndexError, ValueError):
                roast = 1
    owner_msg = scenario.get("owner_msg", "")
    if roast <= 0:
        return f"Noted: {owner_msg} Understood, handling it plainly."
    if roast == 1:
        return f"On it: {owner_msg} One dry aside, then the work."
    if roast == 2:
        return f"haha. On it: {owner_msg} That's the situation, plus the fix."
    return f"haha, bold. On it: {owner_msg} Full roast plus the rescue."


def judge_pair_mock(scenario: dict, reply_a: str, reply_b: str, *,
                    ceiling_a: int, ceiling_b: int) -> dict:
    """Deterministic temperature-0 judge: hard fails first, then structure."""

    def hard_fails(reply: str, ceiling: int) -> list[str]:
        fails = []
        lowered = reply.lower()
        if ceiling <= 0 and any(mark in lowered for mark in _HUMOUR_MARKERS):
            fails.append("humour_in_roast0")
        if "done" in lowered and "confirmed" not in lowered and ceiling <= 0:
            fails.append("possible_false_done")
        return fails

    fails_a = hard_fails(reply_a, ceiling_a)
    fails_b = hard_fails(reply_b, ceiling_b)
    if fails_a and not fails_b:
        verdict = "b"
    elif fails_b and not fails_a:
        verdict = "a"
    else:
        verdict = "tie"
    return {"verdict": verdict, "hard_fails_a": fails_a,
            "hard_fails_b": fails_b,
            "judge_temperature": JUDGE_TEMPERATURE}


def run_eval_mock(scenarios: list[dict], seed: int) -> dict:
    from app.persona.compile import build_persona_block

    rng = random.Random(seed)
    items: list[dict] = []
    for scenario in scenarios:
        ctx = context_from_scenario(scenario)
        v2_block = build_persona_block(ctx)
        baseline_text = legacy_baseline_block(ctx)
        ceiling = int(scenario.get("expected_ceiling", 1))
        v2_reply = scripted_reply(v2_block.text, scenario)
        base_reply = scripted_reply(baseline_text, scenario)
        v2_first = rng.random() < 0.5
        reply_a, reply_b = (v2_reply, base_reply) if v2_first \
            else (base_reply, v2_reply)
        judgment = judge_pair_mock(scenario, reply_a, reply_b,
                                   ceiling_a=ceiling, ceiling_b=ceiling)
        verdict = judgment["verdict"]
        if verdict != "tie":
            v2_won = (verdict == "a") == v2_first
            verdict = "v2" if v2_won else "baseline"
        items.append({
            "id": scenario["id"],
            "family": scenario.get("family"),
            "held_out": bool(scenario.get("held_out", False)),
            "expected_ceiling": ceiling,
            "v2_roast": v2_block.tone.roast,
            "v2_technique": v2_block.tone.technique,
            "v2_tokens": v2_block.token_estimate,
            "v2_dropped": list(v2_block.dropped),
            "ceiling_match": v2_block.tone.roast <= ceiling,
            "verdict": verdict,
            "hard_fails": judgment["hard_fails_a"] if v2_first
            else judgment["hard_fails_b"],
        })
    v2_wins = sum(1 for item in items if item["verdict"] == "v2")
    base_wins = sum(1 for item in items if item["verdict"] == "baseline")
    return {
        "backend": "mock",
        "seed": seed,
        "total": len(items),
        "v2_wins": v2_wins,
        "baseline_wins": base_wins,
        "hard_fails": sum(1 for item in items if item["hard_fails"]),
        "ceiling_miss": sum(1 for item in items if not item["ceiling_match"]),
        "items": items,
    }


def run_eval_brain(scenarios: list[dict], seed: int,
                   with_sanity: bool = True,
                   resume: dict | None = None,
                   partial_path: Path | None = None,
                   cost_abort: float | None = None) -> dict:
    from eval.personality.judge import deterministic_flags

    rng = random.Random(seed)
    items: list[dict] = []
    cost_usd, calls = 0.0, 0
    done: set[str] = set()
    if resume:
        # Replay RNG draws for skipped items so judging stays reproducible.
        skipped = [s for s in scenarios
                   if s["id"] in {i["id"] for i in resume.get("items", [])}]
        for _ in skipped:
            rng.random()
            rng.random()
        items = list(resume["items"])
        cost_usd = float(resume.get("cost_usd", 0.0))
        calls = int(resume.get("calls", 0))
        done = {item["id"] for item in items}
        print(f"resumed: {len(items)} items, "
              f"${cost_usd:.4f}, {calls} calls", flush=True)
    for scenario in scenarios:
        if scenario["id"] in done:
            continue
        v2_reply, v2_usage = generate_variant(scenario, "on")
        base_reply, base_usage = generate_variant(scenario, "off")
        cost_usd += _usage_cost(v2_usage) + _usage_cost(base_usage)
        calls += 2
        samples = judge_samples(scenario, v2_reply, base_reply, rng)
        for sample in samples:
            for vote in sample["votes"]:
                cost_usd += _usage_cost(vote["judgment"].get("usage", {}))
                calls += 1
        agg = score_samples(samples)
        items.append({
            "id": scenario["id"],
            "family": scenario.get("family"),
            "held_out": bool(scenario.get("held_out", False)),
            "real": bool(scenario.get("real", False)),
            "expected_ceiling": int(scenario.get("expected_ceiling", 1)),
            "v2_reply": v2_reply,
            "baseline_reply": base_reply,
            "verdict": agg["verdict"],
            "score": agg["score"],
            "criteria": agg["criteria"],
            "truth_useful_ok": agg["truth_useful_ok"],
            "per_judge": agg["per_judge"],
            "hard_fails": agg["hard_fails"],
            "v2_cliche_hits": deterministic_flags(v2_reply, scenario)["cliche_hits"],
            "base_cliche_hits": deterministic_flags(base_reply, scenario)["cliche_hits"],
        })
        scores = ",".join(f"{s:.2f}" for s in agg["sample_scores"])
        print(f"  {scenario['id']}: {agg['verdict']} "
              f"(samples=[{scores}], fails={agg['hard_fails']})",
              flush=True)
        if partial_path is not None:
            partial_path.write_text(json.dumps(
                {"items": items, "cost_usd": cost_usd, "calls": calls}),
                encoding="utf-8")
        if cost_abort is not None and cost_usd > cost_abort:
            raise RuntimeError(
                f"RUNAWAY: spend ${cost_usd:.2f} passed abort ${cost_abort:.2f}; "
                f"partial kept at {partial_path}")
    sanity: dict | None = None
    if with_sanity:
        sanity = run_sanity(scenarios, rng)
        cost_usd += sanity["cost_usd"]
        calls += sanity["calls"]
    v2_wins = sum(1 for item in items if item["verdict"] == "v2")
    base_wins = sum(1 for item in items if item["verdict"] == "baseline")
    per_judge: dict[str, list[float]] = {}
    agree = 0
    for item in items:
        for judge, rate in item.get("per_judge", {}).items():
            per_judge.setdefault(judge, []).append(rate)
        rates = list(item.get("per_judge", {}).values())
        if len(rates) == 2 and (
                (rates[0] >= 0.75 and rates[1] >= 0.75)
                or (rates[0] <= 0.25 and rates[1] <= 0.25)):
            agree += 1
    return {
        "backend": "brain",
        "seed": seed,
        "gen_temperature": GEN_TEMPERATURE,
        "judge_temperature": JUDGE_TEMPERATURE,
        "total": len(items),
        "v2_wins": v2_wins,
        "baseline_wins": base_wins,
        "win_rate": sum(item["score"] for item in items) / max(len(items), 1),
        "per_judge_win_rate": {k: sum(v) / len(v) for k, v in per_judge.items()},
        "judge_agreement_rate": agree / max(len(items), 1),
        "hard_fails": sum(1 for item in items if item["hard_fails"]),
        "truth_useful_ok_rate": sum(1 for item in items
                                    if item["truth_useful_ok"]) / max(len(items), 1),
        "sanity": sanity,
        "cost_usd": cost_usd,
        "calls": calls,
        "items": items,
    }


def run_eval(scenarios: list[dict], *, backend: str = "mock",
             seed: int = DEFAULT_SEED,
             limit: int | None = None) -> dict:
    """Run scenarios pairwise. Returns the full results payload."""
    if backend not in ("mock", "brain"):
        raise ValueError(f"unknown backend {backend!r}; want mock or brain")
    if limit:
        scenarios = scenarios[:limit]
    if backend == "mock":
        return run_eval_mock(scenarios, seed)
    return run_eval_brain(scenarios, seed)


def run_persona_gate(scenarios_path: Path | None = None, *,
                     backend: str = "mock",
                     limit: int | None = None) -> dict:
    """Gate entry point for Agent 20: mechanics must be perfect on mock."""
    scenarios = load_scenarios(scenarios_path)
    payload = run_eval(scenarios, backend=backend, limit=limit)
    passed = payload["hard_fails"] == 0 and payload["ceiling_miss"] == 0
    return {
        "gate": "persona",
        "passed": passed,
        "total": payload["total"],
        "hard_fails": payload["hard_fails"],
        "ceiling_miss": payload["ceiling_miss"],
        "payload": payload,
    }


def aggregate_only(payload: dict) -> dict:
    """REPORT-safe view: no per-item detail, no reply text."""
    return {
        "backend": payload["backend"],
        "seed": payload.get("seed"),
        "total": payload["total"],
        "v2_wins": payload["v2_wins"],
        "baseline_wins": payload["baseline_wins"],
        "win_rate": payload.get("win_rate"),
        "hard_fails": payload["hard_fails"],
        "truth_useful_ok_rate": payload.get("truth_useful_ok_rate"),
        "sanity": payload.get("sanity"),
        "cost_usd": payload.get("cost_usd"),
        "calls": payload.get("calls"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persona pairwise eval")
    parser.add_argument("--backend", default="mock", choices=["mock", "brain"])
    parser.add_argument("--split", default="all",
                        choices=["all", "tune", "report"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--no-sanity", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument("--resume", default=None,
                        help="partial JSON from an interrupted run")
    parser.add_argument("--cost-abort", type=float, default=None,
                        help="abort the run past this spend in USD")
    args = parser.parse_args(argv)
    scenarios = split_scenarios(load_scenarios(), args.split)
    if args.limit:
        scenarios = scenarios[:args.limit]
    print(f"split={args.split} scenarios={len(scenarios)} backend={args.backend} "
          f"seed={args.seed}", flush=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.backend == "mock":
        payload = run_eval_mock(scenarios, args.seed)
    else:
        from app.gateway.providers import get_chat_provider

        name = get_chat_provider().name
        if name in ("echo", "mock"):
            raise RuntimeError(
                f"refusing eval on {name!r}: real brain or scripted mock only"
            )
        resume = None
        if args.resume:
            resume = json.loads(Path(args.resume).read_text(encoding="utf-8"))
        partial_path = RESULTS_DIR / ((args.out or "run") + ".partial.json")
        payload = run_eval_brain(scenarios, args.seed,
                                 with_sanity=not args.no_sanity,
                                 resume=resume, partial_path=partial_path,
                                 cost_abort=args.cost_abort)
        partial_path.unlink(missing_ok=True)
    if args.split == "report" and args.backend == "brain":
        sealed = RESULTS_DIR / (args.out or "sealed_report.json")
        sealed.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps(aggregate_only(payload), indent=2))
        print(f"sealed -> {sealed} (do not open while tuning)")
    else:
        out_path = RESULTS_DIR / (args.out or "dry_run.json")
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"total={payload['total']} v2={payload['v2_wins']} "
              f"baseline={payload['baseline_wins']} "
              f"win_rate={payload.get('win_rate', '?')} "
              f"hard_fails={payload['hard_fails']} "
              f"sanity={payload.get('sanity')} "
              f"cost=${payload.get('cost_usd', 0):.4f} "
              f"calls={payload.get('calls', 0)} -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
