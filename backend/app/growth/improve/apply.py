"""GE6 --apply: run a coding CLI headless in a fresh worktree, then SUBMIT.

Containment (all enforced, asserted after the run):
- Fresh worktree under ~/.ev/growth/worktrees/ + new branch ge-improve-<n>.
- The agent's cwd is the worktree; the live tree is never its target.
- Wall-clock timeout bounds the run (default 45 min).
- After the run: the LIVE worktree must be byte-identical to before
  (git status porcelain compared); any live-tree change FAILS the apply.
- Never merges, deploys, or pushes. SUBMITs the branch to the train.

Budget: --budget caps eval spend for suite runs the agent triggers
(passed in the brief); agent inference spends the owner's coding
subscription (muse exposes no per-run inference cap; the wall timeout
bounds it — stated honestly in the brief and the report).
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
WT_ROOT = Path.home() / ".ev" / "growth" / "worktrees"


def _git(*a: str, cwd: Path | None = None, check: bool = False):
    return subprocess.run(["git", "-C", str(cwd or REPO), *a], capture_output=True,
                          text=True, check=check, timeout=120)


def live_snapshot() -> str:
    return _git("status", "--porcelain").stdout


def new_branch_name() -> str:
    existing = _git("branch", "--list", "ge-improve-*").stdout
    nums = [int(m.group(1)) for m in re.finditer(r"ge-improve-(\d+)", existing)]
    return f"ge-improve-{(max(nums) if nums else 0) + 1}"


def apply(*, item_id: int | None = None, budget: float = 1.0,
          cli: str | None = None, timeout_s: int = 2700,
          base: str = "main") -> dict:
    """Run the full --apply flow. Returns a report dict (never raises)."""
    from ..train.submit import submit
    from . import brief as B
    from . import detect as D

    t0 = time.time()
    report: dict = {"ok": False, "cli": cli or "", "branch": "", "log": []}
    clis = D.detect()
    if not clis:
        report["error"] = "no coding CLI installed (looked for muse, claude, codex)"
        return report
    name = cli or clis[0]
    if name not in clis:
        report["error"] = f"{name} not installed (have: {clis})"
        return report
    report["cli"] = name
    try:
        text, ctx = B.build(item_id=item_id, budget=budget)
    except ValueError as e:
        report["error"] = str(e)
        return report
    branch = new_branch_name()
    report["branch"] = branch
    wt = WT_ROOT / branch.replace("/", "_")
    before = live_snapshot()
    try:
        _git("worktree", "add", "-b", branch, str(wt), base, check=True)
    except subprocess.CalledProcessError as e:
        report["error"] = f"worktree create failed: {(e.stderr or '')[-300:]}"
        return report
    brief_path = wt / "IMPROVE_BRIEF.md"
    brief_path.write_text(text)
    prompt = (f"You are the growth-engine repair agent. Read IMPROVE_BRIEF.md in "
              f"this worktree ({wt}) and do exactly what it says: reproduce, fix "
              f"with regression tests, run the affected checks, commit on this "
              f"branch. {B.STANDING_RULES} When done, print DONE plus the commit sha.")
    prompt_file = wt / "IMPROVE_PROMPT.txt"
    prompt_file.write_text(prompt)
    try:
        if name == "muse":
            cmd = D.headless_cmd_file(name, str(prompt_file))
        else:
            cmd = D.headless_cmd(name, prompt)
        p = subprocess.run(cmd, cwd=str(wt), capture_output=True, text=True,
                           timeout=timeout_s)
        report["rc"] = p.returncode
        report["tail"] = ((p.stdout or "") + (p.stderr or ""))[-2000:]
    except subprocess.TimeoutExpired:
        report["error"] = f"agent timed out after {timeout_s}s (branch kept: {branch})"
        report["branch_kept"] = branch
        return report
    after = live_snapshot()
    if after != before:
        report["error"] = ("LIVE TREE CHANGED during agent run — refusing to submit; "
                           "inspect and revert manually")
        report["live_diff"] = after[:500]
        return report
    wt_status = _git("status", "--porcelain", cwd=wt).stdout.strip()
    if wt_status:
        report["error"] = ("agent left uncommitted changes; refusing to submit "
                           f"(inspect {wt}): {wt_status[:300]}")
        report["branch_kept"] = branch
        return report
    diff = _git("diff", "--stat", f"{base}...{branch}").stdout.strip()
    if not diff:
        report["error"] = "agent made no changes (empty diff); nothing to submit"
        return report
    head = _git("rev-parse", branch).stdout.strip()
    report["diff_stat"] = diff[:1000]
    report["head"] = head
    report["seconds"] = round(time.time() - t0, 1)
    try:
        sub = submit(branch, _scope_from_diff(base, branch),
                     f"improve item={item_id} cli={name} budget={budget}")
        report["submission"] = sub["id"]
        report["ok"] = True
    except ValueError as e:
        report["error"] = f"submit refused: {e}"
    return report


def _scope_from_diff(base: str, branch: str) -> str:
    out = _git("diff", "--name-only", f"{base}...{branch}").stdout.splitlines()
    files = [ln.strip() for ln in out if ln.strip()]
    # Scope = the exact file list (tightest honest scope for an agent branch).
    return ",".join(files)
