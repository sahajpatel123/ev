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


def _render_capability(summary: dict, rows: list[dict]) -> str:
    lines = [
        "# Capability gauntlet",
        "",
        f"Cases: {summary['n']}. Ran: {summary['ran']}. Skipped: {summary['skipped']}.",
        f"Pass rate: {summary['pass_rate']}%.",
        "",
        "This is the behaviour pass rate with the live agent: the model was "
        "given the goal and the world, and had to work it out.",
        "",
        "| tier | n | pass@1 | mean tool calls | mean ms |",
        "| --- | --- | --- | --- | --- |",
    ]
    for tier, row in summary["by_tier"].items():
        lines.append(
            f"| {tier} | {row['n']} | {row['pass_rate']}% | "
            f"{row['mean_tool_calls']} | {row['mean_elapsed_ms']} |"
        )
    lines += ["", "| case | tier | result | tools |", "| --- | --- | --- | --- |"]
    for row in rows:
        if row.get("skipped"):
            mark = "SKIP"
        elif row.get("passed"):
            mark = "PASS"
        else:
            mark = "FAIL " + "; ".join(row.get("problems") or [])[:80]
        lines.append(
            f"| {row['id']} | {row['tier']} | {mark} | {', '.join(row.get('tools') or [])[:70]} |"
        )
    return "\n".join(lines) + "\n"


def _run_capability(args) -> int:
    """Run the capability tiers: real agent, real provider, sandboxed world."""
    import asyncio

    # Credentials the same way the app loads them: repo .env, then the
    # production secret file.
    try:
        from gauntlet.real_brain import _prime_env

        _prime_env()
    except Exception:  # pragma: no cover - import guard
        pass

    from gauntlet.capability import run_case, summarize
    from gauntlet.tiers import cases_for

    cases = cases_for(args.tiers or "all")
    if args.case:
        cases = [c for c in cases_for("all") if c.id == args.case]
        if not cases:
            print(f"no such case: {args.case}")
            return 2

    async def go():
        out = []
        for case in cases:
            row = await run_case(case)
            out.append(row)
            mark = "SKIP" if row.get("skipped") else ("PASS" if row.get("passed") else "FAIL")
            print(
                f"{mark:4} {row['id']:32} {row.get('tool_calls', 0):>3} calls "
                f"{row.get('elapsed_ms', 0):>6} ms  {row.get('problems') or ''}",
                flush=True,
            )
        return out

    rows = asyncio.run(go())
    summary = summarize(rows)
    if args.report:
        Path(args.report).write_text(_render_capability(summary, rows), encoding="utf-8")
    if args.json:
        print(json.dumps(summary))
    else:
        print()
        print(f"capability pass {summary['pass_rate']}% over {summary['ran']} cases")
        for tier, row in summary["by_tier"].items():
            print(f"  {tier}: {row['pass_rate']}% ({row['n']} cases, "
                  f"{row['mean_tool_calls']} calls avg)")
    return 0 if not summary["failed"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ev-gauntlet")
    parser.add_argument("--domain", default="")
    parser.add_argument("--split", default="dev", choices=["dev", "fresh", "all"])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--report", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--real-brain", action="store_true")
    parser.add_argument("--budget", type=float, default=None)
    parser.add_argument(
        "--tiers",
        default="",
        help=(
            "Run the capability tiers against the live agent instead of the "
            "scripted lane gauntlet: t1 (foundations), t2 (advanced), "
            "t3 (frontier), or all."
        ),
    )
    parser.add_argument("--case", default="", help="Run one capability case id.")
    args = parser.parse_args(argv)
    if args.budget is not None:
        import os
        os.environ["GAUNTLET_BUDGET_USD"] = str(args.budget)
    if args.tiers or args.case:
        return _run_capability(args)
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
