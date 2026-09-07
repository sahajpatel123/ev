#!/usr/bin/env python3
"""Measure the EV workspace and check the recorded baseline for drift.

Stdlib only, no backend imports, no network: this must run on a bare checkout
before ``uv sync`` so that any agent can establish ground truth first.

    python3 tools/baseline.py            # human-readable table
    python3 tools/baseline.py --json     # machine-readable metrics
    python3 tools/baseline.py --write    # record measurements in baseline.json
    python3 tools/baseline.py --check    # fail on invariant breaks or metric drift

``--check`` separates two kinds of failure:

* **Invariants** are exact. They encode facts that cannot legitimately change
  without a decision, such as the fleet size agreeing across the governance
  docs, or every OWNS path in the fleet roster resolving on disk.
* **Metrics** are counts that grow as the product grows. They are compared
  against ``baseline.json`` with a drift budget, so ordinary growth is silent
  and a large unexplained swing is loud.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"

# A metric may drift this fraction from the recorded baseline before --check
# complains. Growth is expected; a 25% swing means the docs need a re-read.
DRIFT_BUDGET = 0.25

# AGENTS.md documents all three historical rosters, so its own declaration has
# to be unambiguous. This exact phrase is the one the invariant reads.
AGENTS_MD_SENTINEL = "Authoritative fleet size:"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _py_files(root: Path) -> list[Path]:
    return [p for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts]


def _line_count(paths: list[Path]) -> int:
    return sum(len(_read(p).splitlines()) for p in paths)


def _count_matches(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, re.MULTILINE))


def _expand_braces(token: str) -> list[str]:
    """Expand one ``a/{b,c}.py`` group, the form the fleet docs use."""

    match = re.match(r"^(.*?)\{([^}]*)\}(.*)$", token)
    if not match:
        return [token]
    head, body, tail = match.groups()
    expanded: list[str] = []
    for part in body.split(","):
        expanded.extend(_expand_braces(f"{head}{part.strip()}{tail}"))
    return expanded


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------


def measure() -> dict[str, int]:
    app = REPO_ROOT / "backend" / "app"
    clients = REPO_ROOT / "backend" / "clients"
    tests = REPO_ROOT / "backend" / "tests"
    docs = REPO_ROOT / "docs"

    app_files = _py_files(app)
    client_files = _py_files(clients)
    test_files = sorted(tests.glob("test_*.py"))
    api_text = "".join(_read(p) for p in sorted((app / "api").glob("*.py")))
    tests_text = "".join(_read(p) for p in test_files)

    contract = json.loads(_read(REPO_ROOT / "backend" / "eval" / "contract_v1.json") or "{}")
    contract_paths = contract.get("paths", {})

    metrics: dict[str, int] = {
        "app_python_modules": len(app_files),
        "app_python_lines": _line_count(app_files),
        "app_subpackages": len([p for p in app.iterdir() if p.is_dir() and p.name != "__pycache__"]),
        "client_python_modules": len(client_files),
        "client_python_lines": _line_count(client_files),
        "test_modules": len(test_files),
        "test_functions": _count_matches(r"^\s*(?:async\s+)?def test_", tests_text),
        "api_routers": len([p for p in (app / "api").glob("*.py") if p.name != "__init__.py"]),
        "api_route_decorators": _count_matches(
            r"@router\.(?:get|post|put|patch|delete|head|options)\(", api_text
        ),
        "contract_paths": len(contract_paths),
        "contract_operations": sum(len(v) for v in contract_paths.values()),
        "settings_fields": _count_matches(
            r"^    [a-z][a-z0-9_]*\s*:", _read(app / "config.py")
        ),
        "orm_tables": _count_matches(r"__tablename__", _read(app / "models.py")),
        "alembic_migrations": len(
            [p for p in (REPO_ROOT / "backend" / "alembic" / "versions").glob("*.py")]
        ),
        "docs_markdown_files": len(list(docs.glob("*.md"))),
        "env_example_keys": _count_matches(r"^EV_[A-Z0-9_]+=", _read(REPO_ROOT / ".env.example")),
        "env_api_first_keys": _count_matches(
            r"^EV_[A-Z0-9_]+=", _read(REPO_ROOT / ".env.api-first")
        ),
        "swift_files": len(list(REPO_ROOT.rglob("*.swift"))),
        "swift_lines": _line_count(sorted(REPO_ROOT.rglob("*.swift"))),
        "fleet_size": fleet_size_from_roster(),
    }
    return metrics


def fleet_size_from_roster() -> int:
    """Highest agent number in the AGENT_FLEET.md ownership table."""

    text = _read(REPO_ROOT / "docs" / "AGENT_FLEET.md")
    section = text.split("## 2. File-level exclusive ownership")
    if len(section) < 2:
        return 0
    rows = section[1].split("## 3.")[0]
    numbers = [int(n) for n in re.findall(r"^\|\s*\*\*(\d+)\*\*\s*\|", rows, re.MULTILINE)]
    return max(numbers) if numbers else 0


def agents_md_declared_fleet_size() -> set[int]:
    """Fleet size declared by the AGENTS_MD_SENTINEL line in AGENTS.md.

    AGENTS.md documents all three historical rosters (A0-A9, 1-15, 1-20) so that
    agents can recognise whichever one they were handed. Only the explicit
    sentinel counts as its own declaration.
    """

    agents_md = _read(REPO_ROOT / "AGENTS.md")
    pattern = re.escape(AGENTS_MD_SENTINEL) + r"[^\d]{0,4}(\d+)"
    return {int(n) for n in re.findall(pattern, agents_md)}


def owns_paths() -> dict[int, list[str]]:
    """Agent number -> OWNS path tokens, parsed from the fleet ownership table.

    Only the OWNS column is parsed. The MUST NOT TOUCH column is prose written
    in repo-relative shorthand (``app/vision/**`` rather than
    ``backend/app/vision/**``) and is not resolvable.
    """

    text = _read(REPO_ROOT / "docs" / "AGENT_FLEET.md")
    section = text.split("## 2. File-level exclusive ownership")
    if len(section) < 2:
        return {}
    rows = section[1].split("## 3.")[0]
    owned: dict[int, list[str]] = {}
    for line in rows.splitlines():
        match = re.match(r"^\|\s*\*\*(\d+)\*\*\s*\|(.*?)\|", line)
        if not match:
            continue
        agent = int(match.group(1))
        tokens = [t for t in re.findall(r"`([^`]+)`", match.group(2)) if "/" in t]
        owned[agent] = tokens
    return owned


def unresolved_owns_paths() -> list[tuple[int, str]]:
    """OWNS tokens that do not resolve to anything on disk."""

    unresolved: list[tuple[int, str]] = []
    for agent, tokens in sorted(owns_paths().items()):
        for token in tokens:
            for candidate in _expand_braces(token):
                target = candidate.rstrip("/")
                if target.endswith("/**"):
                    target = target[:-3]
                if not (REPO_ROOT / target).exists():
                    unresolved.append((agent, candidate))
    return unresolved


def shared_files() -> list[str]:
    """Shared append-only files declared by AGENT_FLEET.md section 3."""

    text = _read(REPO_ROOT / "docs" / "AGENT_FLEET.md")
    section = text.split("## 3. Shared append-only files")
    if len(section) < 2:
        return []
    body = section[1].split("Rules:")[0]
    tokens: list[str] = []
    for token in re.findall(r"`([^`]+)`", body):
        tokens.extend(_expand_braces(token))
    return sorted(set(tokens))


# --------------------------------------------------------------------------
# invariants
# --------------------------------------------------------------------------


@dataclass
class InvariantResult:
    failures: list[str] = field(default_factory=list)

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            self.failures.append(message)


def check_invariants() -> list[str]:
    result = InvariantResult()
    docs = REPO_ROOT / "docs"

    fleet_size = fleet_size_from_roster()
    result.check(fleet_size > 0, "AGENT_FLEET.md ownership table has no parseable agent rows")

    law = _read(docs / "FLEET_LAW.md")
    law_sizes = {int(n) for n in re.findall(r"binding on all (\d+) agents", law)}
    result.check(
        law_sizes == {fleet_size},
        f"FLEET_LAW.md declares {law_sizes or 'no'} agents but the AGENT_FLEET.md "
        f"ownership table defines {fleet_size}",
    )

    agents_md = _read(REPO_ROOT / "AGENTS.md")
    result.check(bool(agents_md), "AGENTS.md is missing from the repository root")
    if agents_md:
        declared = agents_md_declared_fleet_size()
        result.check(
            declared == {fleet_size},
            f"AGENTS.md '{AGENTS_MD_SENTINEL} N' declares {declared or 'nothing'} but the "
            f"fleet roster defines {fleet_size}",
        )

    unresolved = unresolved_owns_paths()
    result.check(
        not unresolved,
        "AGENT_FLEET.md OWNS paths that do not exist on disk: "
        + ", ".join(f"agent {a}: {p}" for a, p in unresolved),
    )

    for token in shared_files():
        result.check(
            (REPO_ROOT / token).exists(),
            f"shared append-only file declared in AGENT_FLEET.md does not exist: {token}",
        )

    contract = json.loads(_read(REPO_ROOT / "backend" / "eval" / "contract_v1.json") or "{}")
    paths = contract.get("paths", {})
    result.check(bool(paths), "backend/eval/contract_v1.json has no locked paths")
    result.check(
        all(key.startswith("/v1") for key in paths),
        "contract_v1.json locks non-/v1 paths; the contract gate only covers /v1",
    )

    return result.failures


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def load_baseline() -> dict[str, int]:
    data = json.loads(_read(BASELINE_PATH) or "{}")
    metrics = data.get("metrics", {})
    return {k: int(v) for k, v in metrics.items()}


def drifted(recorded: dict[str, int], measured: dict[str, int]) -> list[str]:
    problems: list[str] = []
    for key, expected in sorted(recorded.items()):
        actual = measured.get(key)
        if actual is None:
            problems.append(f"{key}: recorded {expected}, no longer measured")
            continue
        allowed = max(1.0, expected * DRIFT_BUDGET)
        if abs(actual - expected) > allowed:
            problems.append(
                f"{key}: recorded {expected}, measured {actual} "
                f"(drift budget +/-{allowed:.0f})"
            )
    for key in sorted(set(measured) - set(recorded)):
        problems.append(f"{key}: measured {measured[key]}, not recorded in baseline.json")
    return problems


def render(metrics: dict[str, int]) -> str:
    width = max(len(k) for k in metrics)
    lines = [f"EV workspace baseline ({REPO_ROOT})", ""]
    lines += [f"  {key.ljust(width)}  {value}" for key, value in metrics.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print metrics as JSON")
    parser.add_argument("--write", action="store_true", help="record metrics in baseline.json")
    parser.add_argument("--check", action="store_true", help="verify invariants and metric drift")
    args = parser.parse_args(argv)

    metrics = measure()

    if args.write:
        BASELINE_PATH.write_text(
            json.dumps(
                {
                    "_comment": (
                        "Measured by tools/baseline.py. Refresh with "
                        "`make baseline-write` when a change legitimately moves these "
                        "numbers, and update the baseline table in AGENTS.md to match."
                    ),
                    "drift_budget": DRIFT_BUDGET,
                    "metrics": metrics,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {BASELINE_PATH.relative_to(REPO_ROOT)}")
        return 0

    if args.check:
        failures = check_invariants()
        recorded = load_baseline()
        if not recorded:
            failures.append("tools/baseline.json is missing or empty; run `make baseline-write`")
        else:
            failures.extend(drifted(recorded, metrics))
        if failures:
            print("baseline check FAILED:")
            for failure in failures:
                print(f"  - {failure}")
            return 1
        print(f"baseline check OK: {len(metrics)} metrics within drift budget, invariants hold")
        return 0

    print(json.dumps(metrics, indent=2) if args.json else render(metrics))
    return 0


if __name__ == "__main__":
    sys.exit(main())
