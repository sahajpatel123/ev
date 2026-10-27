"""Brain scorecard: EVIE vs RAW-HIGH/RAW-MEDIUM over the v2 corpus.

    python -m gauntlet.v2.score --config evie --config raw-high --runs 1

Configs: ``evie`` (the real facade + loop in isolated worlds), ``raw-high``
and ``raw-medium`` (the minimal native-tools harness at reasoning effort
high/medium, same brain model, stateless per turn). Output per category:
pass rate with a 95% Wilson interval, harness penalty (EVIE minus RAW) with
a 95% Newcombe interval and a B8 verdict, cost/turn, latency p50/p95, and a
failure-category histogram. Each run persists under
``docs/brain-probe/scores/<run>.json`` with the commit id.

Real-brain runs require the eval key (see ``evalkey``); without it the run
refuses with an OWNER-ACTION instead of spending the live key.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from gauntlet.v2 import stats
from gauntlet.v2.runner import RESULTS, run_many

BACKEND = Path(__file__).resolve().parents[2]
SCORES = BACKEND.parent / "docs" / "brain-probe" / "scores"

CONFIGS = {
    "evie": {"harness": "evie", "effort": ""},
    "raw-high": {"harness": "raw", "effort": "high"},
    "raw-medium": {"harness": "raw", "effort": "medium"},
}


def _commit() -> str:
    try:
        out = subprocess.run(["git", "-C", str(BACKEND.parent), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _case_latency_ms(row: dict) -> float:
    total = sum(float(r.get("elapsed_ms") or 0) for r in row.get("replies") or [])
    return total if total > 0 else float(row.get("elapsed_s") or 0) * 1000.0


def _case_cost(row: dict) -> float:
    return float((row.get("usage") or {}).get("cost_usd") or 0.0)


def summarize(rows: list[dict]) -> dict[str, Any]:
    n = len(rows)
    passed = sum(1 for r in rows if r.get("passed"))
    costs = [_case_cost(r) for r in rows]
    latencies = [_case_latency_ms(r) for r in rows]
    return {
        **stats.wilson(n, passed),
        "cost_usd": round(sum(costs), 6),
        "cost_per_case_usd": round(sum(costs) / n, 6) if n else 0.0,
        "latency_ms_p50": round(stats.percentile(latencies, 50), 1),
        "latency_ms_p95": round(stats.percentile(latencies, 95), 1),
        "tool_steps_p50": round(stats.percentile([float(r.get("tool_steps") or 0) for r in rows], 50), 1),
        "unauthorized": sum(1 for r in rows if r.get("unauthorized")),
        "false_success": sum(1 for r in rows if r.get("false_success")),
    }


def aggregate(rows: list[dict]) -> dict[str, Any]:
    """Per-config overall + per-domain stats, penalties, histograms."""
    by_config: dict[str, list[dict]] = {}
    for row in rows:
        by_config.setdefault(row.get("config", "evie"), []).append(row)
    overall = {name: summarize(rs) for name, rs in by_config.items()}
    histograms = {name: stats.histogram(rs) for name, rs in by_config.items()}
    domains: dict[str, dict[str, Any]] = {}
    names = sorted({r.get("domain", "?") for r in rows})
    for domain in names:
        per = {name: summarize([r for r in rs if r.get("domain") == domain])
               for name, rs in by_config.items()}
        entry: dict[str, Any] = {"configs": per}
        if "evie" in per:
            for other in sorted(by_config):
                if other == "evie":
                    continue
                a, b = per["evie"], per[other]
                entry[f"penalty_vs_{other}"] = {
                    **stats.newcombe(a["n"], a["k"], b["n"], b["k"]),
                    "verdict": stats.verdict(a, b),
                }
        domains[domain] = entry
    penalties: dict[str, Any] = {}
    if "evie" in overall:
        for other in sorted(by_config):
            if other == "evie":
                continue
            a, b = overall["evie"], overall[other]
            penalties[f"penalty_vs_{other}"] = {
                **stats.newcombe(a["n"], a["k"], b["n"], b["k"]),
                "verdict": stats.verdict(a, b),
            }
    return {"overall": overall, "domains": domains, "penalties": penalties,
            "histograms": histograms}


def run_scorecard(*, configs: list[str], cases: list[dict], runs: int, jobs: int,
                  budget_usd: float | None, out_dir: Path, on_result=None) -> dict[str, Any]:
    """Run the full matrix. One shared budget pool; coverage is recorded."""
    rows: list[dict] = []
    spent = 0.0
    stopped = False
    planned = len(configs) * runs * len(cases)
    for config in configs:
        spec = CONFIGS[config]
        for run_idx in range(runs):
            if stopped:
                break
            tag = f"{config}-r{run_idx}"
            got, spent, stopped = run_many(
                cases, brain="real", jobs=jobs, variant="primary",
                budget_usd=budget_usd, spent_usd=spent, out_dir=out_dir,
                on_result=on_result, harness=spec["harness"], effort=spec["effort"] or "high",
                tag=tag)
            for row in got:
                row["config"] = config
                row["run_idx"] = run_idx
            rows.extend(got)
    return {"rows": rows, "spent_usd": round(spent, 6), "stopped_by_budget": stopped,
            "planned": planned, "ran": len(rows)}


def slim(row: dict) -> dict[str, Any]:
    """Row summary for the scores file (full rows stay under results/<run>/)."""
    replies = [str(r.get("reply") or "") for r in row.get("replies") or []]
    return {
        "id": row.get("id"), "domain": row.get("domain"), "config": row.get("config"),
        "run_idx": row.get("run_idx"), "passed": row.get("passed"),
        "problems": row.get("problems") or [], "branch": row.get("branch") or "",
        "cost_usd": _case_cost(row), "latency_ms": _case_latency_ms(row),
        "tool_steps": row.get("tool_steps") or 0,
        "unauthorized": bool(row.get("unauthorized")),
        "false_success": bool(row.get("false_success")),
        "silent": bool(replies) and not any(t.strip() for t in replies),
    }


def format_result_line(row: dict, spent: float) -> str:
    """One progress line for a finished case (None-safe: runner rows may
    lack config/problems when a case errors before config binds)."""
    return (
        f"{'PASS' if row.get('passed') else 'FAIL'} "
        f"{(row.get('config') or 'none'):<9} {row.get('id', '?'):<34} ${spent:.4f}  "
        f"{'; '.join(row.get('problems') or [])[:160]}")


def main(argv: list[str] | None = None) -> int:
    from gauntlet.v2.corpus import load_corpus
    from gauntlet.v2.evalkey import eval_openrouter_key

    parser = argparse.ArgumentParser(description="brain scorecard (EVIE vs RAW)")
    parser.add_argument("--config", action="append", default=[],
                        choices=sorted(CONFIGS), help="repeatable; default evie+raw-high")
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--budget", type=float,
                        default=float(os.environ.get("BRAIN_EVAL_BUDGET_USD", "15")))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--ids", nargs="*")
    parser.add_argument("--run", default=time.strftime("score-%Y%m%d-%H%M%S"))
    args = parser.parse_args(argv)

    configs = args.config or ["evie", "raw-high"]
    if (args.budget or 0) > 0 and not eval_openrouter_key():
        print("OWNER-ACTION: no eval key. Set one with `ev keys set openrouter-eval` "
              "(or GAUNTLET_EVAL_KEY_FILE); the scorecard never spends the live key.")
        return 2
    cases = load_corpus()
    if args.category:
        cases = [c for c in cases if c["domain"] in set(args.category)]
    if args.ids:
        cases = [c for c in cases if c["id"] in set(args.ids)]
    if args.limit:
        cases = cases[: args.limit]
    out_dir = RESULTS / args.run
    print(f"cases: {len(cases)}  configs: {configs}  runs: {args.runs}  "
          f"budget: ${args.budget}  out: {out_dir}", flush=True)
    result = run_scorecard(configs=configs, cases=cases, runs=args.runs, jobs=args.jobs,
                           budget_usd=args.budget, out_dir=out_dir,
                           on_result=lambda row, spent: print(
                               format_result_line(row, spent), flush=True))
    agg = aggregate(result["rows"])
    SCORES.mkdir(parents=True, exist_ok=True)
    record = {"run": args.run, "commit": _commit(),
              "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "configs": configs, "runs": args.runs, "budget_usd": args.budget,
              "spent_usd": result["spent_usd"],
              "stopped_by_budget": result["stopped_by_budget"],
              "coverage": {"planned": result["planned"], "ran": result["ran"]},
              **agg, "rows": [slim(r) for r in result["rows"]]}
    (SCORES / f"{args.run}.json").write_text(json.dumps(record, indent=1))
    _print_table(agg)
    print(f"\n{result['ran']}/{result['planned']} ran  spent ${result['spent_usd']:.4f}  "
          f"stopped_by_budget={result['stopped_by_budget']}")
    print(f"scores: {SCORES / (args.run + '.json')}")
    return 0


def _print_table(agg: dict[str, Any]) -> None:
    overall = agg["overall"]
    names = sorted(overall)
    print(f"\n{'domain':<14}" + "".join(f"{n + ' k/n [lo,hi]':<26}" for n in names)
          + f"{'penalty':<22}verdict")
    for domain in sorted(agg["domains"]):
        per = agg["domains"][domain]["configs"]
        line = f"{domain:<14}"
        for n in names:
            s = per.get(n, {"k": 0, "n": 0, "lo": 0.0, "hi": 1.0})
            line += f"{s['k']}/{s['n']} [{s['lo']:.2f},{s['hi']:.2f}]{'':<8}"
        pens = [f"{k}={v['diff']:+.2f}[{v['lo']:+.2f},{v['hi']:+.2f}]"
                for k, v in agg["domains"][domain].items() if k.startswith("penalty")]
        verdicts = [v["verdict"] for k, v in agg["domains"][domain].items()
                    if k.startswith("penalty")]
        print(line + f"{';'.join(pens):<22}{';'.join(verdicts)}")
    line = f"{'OVERALL':<14}"
    for n in names:
        s = overall[n]
        line += f"{s['k']}/{s['n']} [{s['lo']:.2f},{s['hi']:.2f}]{'':<8}"
    pens = [f"{k}={v['diff']:+.2f}[{v['lo']:+.2f},{v['hi']:+.2f}]"
            for k, v in agg["penalties"].items()]
    verdicts = [v["verdict"] for v in agg["penalties"].values()]
    print(line + f"{';'.join(pens):<22}{';'.join(verdicts)}")
    for n in names:
        s = overall[n]
        print(f"  {n}: ${s['cost_usd']:.4f} total (${s['cost_per_case_usd']:.6f}/case)  "
              f"lat p50 {s['latency_ms_p50']:.0f}ms p95 {s['latency_ms_p95']:.0f}ms  "
              f"unauth {s['unauthorized']} false-success {s['false_success']}")
    print(f"  histograms: {json.dumps(agg['histograms'])}")


if __name__ == "__main__":
    raise SystemExit(main())
