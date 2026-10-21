"""Seeded, stratified transcripts. Not a score."""
from __future__ import annotations

import random

from gauntlet.brain import TOOL_SCHEMAS, plan
from gauntlet.corpus import build_corpus
from gauntlet.runner import run_case
from gauntlet.world import World


def select(seed: int = 19) -> list[dict]:
    rng = random.Random(seed)
    cases = build_corpus()
    safety = [c for c in cases if c["domain"] == "safety"]
    novel = [c for c in cases if c["domain"] == "novel"]
    chosen = rng.sample(safety, 5) + rng.sample(novel, 5)
    rest = [c for c in cases if c not in chosen]
    # One more from as many other domains as needed to reach 25.
    by_domain: dict[str, list] = {}
    for case in rest:
        by_domain.setdefault(case["domain"], []).append(case)
    domains = sorted(by_domain)
    rng.shuffle(domains)
    for domain in domains:
        if len(chosen) >= 25:
            break
        chosen.append(rng.choice(by_domain[domain]))
    return chosen[:25]


def render(seed: int = 19, real: dict | None = None) -> str:
    lines = [
        "# Gauntlet sample",
        "",
        f"25 cases, seed {seed}, stratified so 5 are safety and 5 are novel.",
        "Each transcript is utterance, plan, tool calls, then the oracle result.",
        "Real-brain rows are filled when that case was part of the real-brain run.",
        "",
    ]
    for case in select(seed):
        lines.append(f"## {case['id']} ({case['domain']}, {case['difficulty']}, {case['class']})")
        lines.append("")
        lines.append(f"Utterance: {case['utterance']}")
        lines.append("")
        real_row = (real or {}).get(case["id"])
        if case.get("real_brain_only") and real_row is None:
            lines.append("Plan: held for the real brain. A blind planner is not scored on this case.")
            lines.append("")
            continue
        if real_row is not None:
            lines.append("Plan (real brain):")
            lines.append("")
            lines.append("```text")
            for op in real_row.get("ops") or []:
                lines.append(f"{op.get('lane')}.{op.get('op')} {op.get('args')}")
            if real_row.get("clarify"):
                lines.append(f"clarify: {real_row['clarify']}")
            if not real_row.get("ops") and not real_row.get("clarify"):
                lines.append("(no tool call)")
            lines.append("```")
            lines.append("")
            lines.append(f"Speech: {real_row.get('speech')}")
            lines.append(
                f"Oracle: {'pass' if real_row.get('passed') else 'fail'} "
                f"policy={real_row.get('policy')}"
            )
            if real_row.get("problems"):
                lines.append(f"Oracle problems: {real_row['problems']}")
            lines.append("")
            continue
        world = World()
        try:
            for item in case.get("setup") or []:
                world.write(item["path"], item.get("text") or "")
            planned = plan(case["utterance"], world, TOOL_SCHEMAS)
        finally:
            world.destroy()
        lines.append("Plan:")
        lines.append("")
        lines.append("```text")
        for op in planned.get("ops") or []:
            lines.append(f"{op.get('lane')}.{op.get('op')} {op.get('args')}")
        if not planned.get("ops"):
            lines.append("(no tool call)")
        lines.append("```")
        lines.append("")
        result = run_case(case)
        lines.append(f"Speech: {result['speech']}")
        lines.append(f"Oracle: {'pass' if result['passed'] else 'fail'} policy={result['policy']}")
        if result["problems"]:
            lines.append(f"Oracle problems: {result['problems']}")
        lines.append("")
    return "\n".join(lines)
