"""CLI for the behavioral gauntlet and the self-improvement brief."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from gauntlet.corpus import build_corpus, by_split
from gauntlet.runner import run_case, summarize


def _select(domain: str, split: str, limit: int) -> list[dict]:
    rows = by_split(split) if split else build_corpus()
    if domain:
        rows = [c for c in rows if c["domain"] == domain]
    if limit:
        rows = rows[:limit]
    return rows


def run_gauntlet(domain: str = "", split: str = "dev", limit: int = 0, report: str = "") -> dict:
    cases = _select(domain, split, limit)
    results = [run_case(c) for c in cases]
    summary = summarize(results)
    summary["split"] = split or "all"
    if report:
        Path(report).write_text(_render(summary), encoding="utf-8")
    return summary


def _render(summary: dict) -> str:
    lines = [
        "# Gauntlet baseline",
        "",
        f"Cases: {summary['n']}. Plumbing cases: {summary.get('plumbing_n', summary['n'])}.",
        f"Plumbing pass rate (lanes, when told what to do): {summary.get('plumbing_pass_at_1', summary['pass_at_1'])}%.",
        "This number is not the headline. The headline is the real-brain behavior pass rate.",
        f"Scripted-only false-success claims: {summary['false_success']}.",
        f"Scripted-only unauthorized effects: {summary['unauthorized']}.",
        "Those zeros are plumbing checks. They are not safety evidence about the model.",
        f"Real-brain-only cases held out of the plumbing score: {summary.get('real_brain_only', 0)}.",
        "",
        "| domain | pass@1 |",
        "| --- | --- |",
    ]
    for domain, rate in sorted(summary["by_domain"].items()):
        lines.append(f"| {domain} | {rate} |")
    lines.append("")
    lines.append("Real-brain numbers are recorded in docs/GAUNTLET_REAL_BRAIN.md when a run finishes.")
    lines.append("A full real-brain pass is gated by GAUNTLET_BUDGET_USD (default 10),")
    lines.append("using measured cost per turn, not a flat guess.")
    if summary["failed"]:
        lines += ["", "## Failures", ""]
        for row in summary["failed"][:40]:
            lines.append(f"- {row['id']}: {row['problems']}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ev-gauntlet")
    parser.add_argument("--domain", default="")
    parser.add_argument("--split", default="dev", choices=["dev", "fresh", "all"])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--report", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--real-brain", action="store_true")
    parser.add_argument("--budget", type=float, default=None)
    args = parser.parse_args(argv)
    if args.budget is not None:
        import os
        os.environ["GAUNTLET_BUDGET_USD"] = str(args.budget)
    if args.real_brain:
        from gauntlet.real_brain import run_stages
        from gauntlet.sample import render

        report = args.report or str(Path(__file__).resolve().parents[3] / "docs" / "GAUNTLET_REAL_BRAIN.md")
        summary = run_stages(report)
        rows = {row["id"]: row for row in summary.get("stage1", {}).get("rows", [])}
        sample_path = Path(report).parent / "GAUNTLET_SAMPLE.md"
        if rows:
            sample_path.write_text(render(real=rows), encoding="utf-8")
        if args.json:
            slim = {k: v for k, v in summary.items() if k not in {"stage1", "stage2", "stage3"}}
            if "stage1" in summary:
                slim["stage1_pass_rate"] = summary["stage1"]["pass_rate"]
                slim["stage1_ran"] = summary["stage1"]["ran"]
            print(json.dumps(slim))
        else:
            if summary.get("owner_action"):
                print(f"OWNER-ACTION {summary.get('command')} {summary.get('detail')}")
            else:
                stage1 = summary.get("stage1") or {}
                print(
                    f"real-brain pass {stage1.get('pass_rate')} "
                    f"ran {stage1.get('ran')}/{stage1.get('eligible')} "
                    f"per_turn ${summary.get('per_turn_usd')} spent ${summary.get('spent_usd')}"
                )
        return 0 if summary.get("ready") else 2
    split = "" if args.split == "all" else args.split
    summary = run_gauntlet(args.domain, split, args.limit, args.report)
    if args.json:
        print(json.dumps({k: v for k, v in summary.items() if k != "failed"}))
    else:
        print(f"pass@1 {summary['pass_at_1']} n={summary['n']} "
              f"false_success={summary['false_success']} unauthorized={summary['unauthorized']}")
    return 0 if summary["false_success"] == 0 and summary["unauthorized"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
