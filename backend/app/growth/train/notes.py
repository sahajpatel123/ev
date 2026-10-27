"""GE3 stage 7 NOTES: auto-generated release notes from measured deltas."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


def generate(tag: str, branch: str, base: str, rc_report: dict, shadow_report: dict) -> str:
    log = subprocess.run(["git", "-C", str(REPO), "log", "--oneline", f"{base}...{branch}"],
                         capture_output=True, text=True).stdout.strip()
    lines = [f"# {tag}", ""]
    lines.append("## What changed")
    lines += [f"- {ln}" for ln in log.splitlines()[:40]] or ["- (no commits listed)"]
    lines += ["", "## Measured deltas (RC vs baseline)"]
    for suite, v in (rc_report.get("suites") or {}).items():
        lines.append(f"- {suite}: {v.get('status')} n={v.get('n')} rate={v.get('pass_rate')} "
                     f"{v.get('wilson95')} — {v.get('compare_note', '')}")
    if rc_report.get("gaps"):
        lines += ["", "## Benchmark gaps (ran nothing, counted nothing)"]
        lines += [f"- {g}" for g in rc_report["gaps"]]
    if shadow_report:
        lines += ["", f"## Shadow replay ({shadow_report.get('n_turns', 0)} turns)"]
        lines.append(f"- flags: {len(shadow_report.get('flags', []))}, "
                     f"leaks: {len(shadow_report.get('leaks', []))}, "
                     f"divergences: {len(shadow_report.get('divergences', []))} (info only)")
        for f in shadow_report.get("flags", [])[:10]:
            lines.append(f"  - {f.get('kind')}: {f.get('detail', '')[:150]}")
    lines += ["", "## Known gaps", "- See the RC gaps above; P2 queue feeds `ev improve` (GE6)."]
    return "\n".join(lines) + "\n"
