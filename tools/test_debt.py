#!/usr/bin/env python3
"""Classify the branch's test failures against a base worktree (WP13).

The tree carries a known, documented debt: a large set of tests fail on
``HEAD`` for reasons unrelated to any one change. A release gate cannot use
"zero failures" as truth, so it uses **regressions**: a test that fails on the
branch but passes (or does not exist) on the base commit.

Usage::

    # branch run (writes junit XML, ~60 min)
    cd backend && uv run pytest tests -q --junitxml=/tmp/branch.xml

    # classify against a worktree of the base commit
    python3 tools/test_debt.py --branch-xml /tmp/branch.xml \
        --base-dir /private/tmp/ev-wp11-head

Exit code is 1 when a **new** failure exists, so `make test-debt` fails a
release that introduces a regression and never on inherited debt.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "docs" / "TEST_DEBT.md"


def failing_nodeids(xml_path: Path) -> set[str]:
    """``path::test`` for every failing/erroring testcase in a junit XML."""

    tree = ET.parse(xml_path)
    out: set[str] = set()
    for case in tree.iter("testcase"):
        failed = any(child.tag in {"failure", "error"} for child in case)
        if not failed:
            continue
        path = case.get("file") or ""
        name = case.get("name") or ""
        if not path or not name:
            continue
        # pytest's junit `file` is repo-relative; normalise separators.
        out.add(f"{path.replace(chr(92), '/')}::{name}")
    return out


def _per_file(nodeids: set[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for node in nodeids:
        counts[node.split("::", 1)[0]] += 1
    return dict(sorted(counts.items()))


def _base_python(base_dir: Path, override: str = "") -> str:
    """Prefer the base worktree's own venv; it has that commit's dependencies."""

    if override:
        return override
    candidate = base_dir / "backend" / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def base_failures(base_dir: Path, files: list[str], timeout_s: int = 900,
                  python: str = "") -> dict[str, set[str]]:
    """Run only the failing FILES on the base commit and collect their failures."""

    results: dict[str, set[str]] = {}
    interpreter = _base_python(base_dir, python)
    for rel in files:
        target = base_dir / "backend" / rel
        if not target.exists():
            results[rel] = set()
            continue
        cmd = [interpreter, "-m", "pytest", rel, "-q", "--tb=no", "-p", "no:cacheprovider"]
        try:
            proc = subprocess.run(cmd, cwd=str(base_dir / "backend"), capture_output=True,
                                  text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            results[rel] = set()
            continue
        found: set[str] = set()
        for line in ((proc.stdout or "") + (proc.stderr or "")).splitlines():
            line = line.strip()
            if not line.startswith("FAILED ") and not line.startswith("ERROR "):
                continue
            node = line.split(" ", 1)[1].split(" - ", 1)[0].strip()
            if "::" in node:
                # Base paths are relative to backend/, same shape as junit's.
                found.add(node)
        results[rel] = found
    return results


def classify(branch: set[str], base: dict[str, set[str]]) -> dict[str, list[str]]:
    base_all = {node for nodes in base.values() for node in nodes}
    return {
        "new": sorted(branch - base_all),
        "inherited": sorted(branch & base_all),
        "fixed": sorted(base_all - branch),
    }


def render(branch: set[str], base: dict[str, set[str]], verdict: str,
           base_dir: str, counts: dict[str, int]) -> str:
    groups = classify(branch, base)
    lines = [
        "# Test debt: branch vs base",
        "",
        f"Base worktree: `{base_dir}`. Branch failures: **{len(branch)}**; "
        f"inherited: **{len(groups['inherited'])}**; new: **{len(groups['new'])}**; "
        f"fixed on this branch: **{len(groups['fixed'])}**.",
        "",
        f"Verdict: **{verdict}** — a new failure is a regression; inherited "
        "debt is tracked, not hidden.",
        "",
        "## Per-file branch failures",
        "",
        "| file | failures |",
        "| --- | --- |",
    ]
    for path, count in _per_file(branch).items():
        lines.append(f"| `{path}` | {count} |")
    if groups["new"]:
        lines += ["", "## New on this branch (regressions)", ""]
        lines += [f"- `{node}`" for node in groups["new"]]
    if groups["fixed"]:
        lines += ["", "## Fixed on this branch", ""]
        lines += [f"- `{node}`" for node in groups["fixed"]]
    if counts:
        lines += ["", "## Run counts", ""]
        lines += [f"- {key}: {value}" for key, value in sorted(counts.items())]
    lines += ["", "## Inherited failures", ""]
    lines += [f"- `{node}`" for node in groups["inherited"]] or ["- none"]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="test-debt")
    parser.add_argument("--branch-xml", required=True)
    parser.add_argument("--base-dir", required=True)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", default="")
    parser.add_argument("--base-python", default="",
                        help="interpreter for the base worktree (default: its .venv)")
    parser.add_argument("--skip-base", action="store_true",
                        help="only list branch failures (no comparison)")
    args = parser.parse_args(argv)
    branch = failing_nodeids(Path(args.branch_xml))
    files = sorted({node.split("::", 1)[0] for node in branch})
    base: dict[str, set[str]] = {}
    if not args.skip_base:
        print(f"[test-debt] re-running {len(files)} failing file(s) on "
              f"{args.base_dir}", flush=True)
        base = base_failures(Path(args.base_dir), files, python=args.base_python)
    groups = classify(branch, base)
    verdict = "NO REGRESSIONS" if not groups["new"] else "REGRESSIONS"
    counts: dict[str, int] = {}
    tree = ET.parse(args.branch_xml)
    suite = tree.getroot()
    if suite.tag == "testsuites" and len(suite):
        suite = suite[0]
    for attr in ("tests", "failures", "errors", "skipped"):
        if suite.get(attr) is not None:
            counts[attr] = int(str(suite.get(attr)))
    doc = render(branch, base, verdict, args.base_dir, counts)
    Path(args.out).write_text(doc)
    print(f"[test-debt] wrote {args.out}: {verdict} "
          f"({len(branch)} failures, {len(groups['new'])} new)")
    if args.json:
        Path(args.json).write_text(json.dumps({
            "verdict": verdict, "branch": sorted(branch),
            "new": groups["new"], "fixed": groups["fixed"],
            "inherited": groups["inherited"], "counts": counts,
        }, indent=2))
    return 0 if not groups["new"] else 1


if __name__ == "__main__":
    sys.exit(main())
