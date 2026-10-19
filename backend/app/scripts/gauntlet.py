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
        f"Split: {summary.get('split')}. Cases: {summary['n']}.",
        f"Scripted pass@1: {summary['pass_at_1']}%.",
        f"False-success claims: {summary['false_success']}. Unauthorized effects: {summary['unauthorized']}.",
        "",
        "| domain | pass@1 |",
        "| --- | --- |",
    ]
    for domain, rate in sorted(summary["by_domain"].items()):
        lines.append(f"| {domain} | {rate} |")
    lines.append("")
    lines.append("Scripted mode uses a generic planner over the real lanes. It is not the real model.")
    lines.append("Real-brain majority-of-3 was not run on the full corpus: 480 cases x 3 runs")
    lines.append("at a conservative $0.01 per call is about $14.40, above 25% of the $40 monthly cap ($10).")
    lines.append("A sampled real-brain pass is recorded separately when a provider key is present.")
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
    args = parser.parse_args(argv)
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
