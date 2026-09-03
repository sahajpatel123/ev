"""Guards the coordination layer that ~20 parallel agents share.

Drives the real ``tools/baseline.py`` measurements against the real in-repo
docs. The point is that the roster can never silently fork into two numbering
schemes again, and that the ownership table can never point at paths that do
not exist.

Deliberately platform-agnostic and dependency-free: ``tools/baseline.py`` is
stdlib only and imports nothing from ``app``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_TOOL = REPO_ROOT / "tools" / "baseline.py"


def _load_baseline_tool() -> ModuleType:
    name = "ev_workspace_baseline"
    spec = importlib.util.spec_from_file_location(name, BASELINE_TOOL)
    assert spec is not None and spec.loader is not None, f"cannot load {BASELINE_TOOL}"
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so the module's dataclasses can resolve their own
    # __module__ during class creation.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def baseline() -> ModuleType:
    assert BASELINE_TOOL.is_file(), f"missing measurement tool: {BASELINE_TOOL}"
    return _load_baseline_tool()


def test_agents_md_exists_at_repo_root() -> None:
    """Agents are told to read AGENTS.md first; it has to be there."""

    agents_md = REPO_ROOT / "AGENTS.md"
    assert agents_md.is_file(), "AGENTS.md is missing from the repository root"
    text = agents_md.read_text(encoding="utf-8")
    for pointer in ("FLEET_LAW.md", "AGENT_FLEET.md", "WORKSPACE_ANALYSIS.md"):
        assert pointer in text, f"AGENTS.md must point at {pointer}"


def test_fleet_size_agrees_across_governance_docs(baseline: ModuleType) -> None:
    """FLEET_LAW, the AGENT_FLEET roster, and AGENTS.md must state one number.

    Three fleet models (A0-A9, 1-15, 1-20) have coexisted in this repo, and the
    same agent number named different owners in each. This test is the lock
    that keeps the authoritative roster single-valued.
    """

    roster_size = baseline.fleet_size_from_roster()
    assert roster_size > 0, "AGENT_FLEET.md ownership table has no parseable agent rows"

    law = (REPO_ROOT / "docs" / "FLEET_LAW.md").read_text(encoding="utf-8")
    assert f"binding on all {roster_size} agents" in law, (
        f"FLEET_LAW.md must bind exactly the {roster_size} agents defined by the "
        "AGENT_FLEET.md ownership table"
    )

    assert baseline.agents_md_declared_fleet_size() == {roster_size}, (
        f"AGENTS.md must declare '{baseline.AGENTS_MD_SENTINEL} {roster_size}' to match "
        "the AGENT_FLEET.md ownership table"
    )


def test_every_owned_path_resolves_on_disk(baseline: ModuleType) -> None:
    """An OWNS entry pointing at nothing is an ownership gap nobody notices."""

    unresolved = baseline.unresolved_owns_paths()
    assert not unresolved, "AGENT_FLEET.md OWNS paths missing from the tree: " + ", ".join(
        f"agent {agent}: {path}" for agent, path in unresolved
    )


def test_shared_append_only_files_exist(baseline: ModuleType) -> None:
    shared = baseline.shared_files()
    assert shared, "AGENT_FLEET.md section 3 declares no shared files"
    missing = [token for token in shared if not (REPO_ROOT / token).exists()]
    assert not missing, f"shared append-only files declared but absent: {missing}"


def test_every_agent_has_exclusive_paths(baseline: ModuleType) -> None:
    owned = baseline.owns_paths()
    roster_size = baseline.fleet_size_from_roster()
    assert sorted(owned) == list(range(1, roster_size + 1)), (
        f"ownership table must cover agents 1..{roster_size}, got {sorted(owned)}"
    )
    empty = [agent for agent, paths in owned.items() if not paths]
    assert not empty, f"agents with no OWNS paths: {empty}"


def test_recorded_baseline_is_current(baseline: ModuleType) -> None:
    """The measured tree must stay within the recorded drift budget.

    Growth is expected and silent; a 2x swing means AGENTS.md section 5 and
    tools/baseline.json need `make baseline-write`.
    """

    recorded = baseline.load_baseline()
    assert recorded, "tools/baseline.json is missing or empty; run `make baseline-write`"
    problems = baseline.drifted(recorded, baseline.measure())
    assert not problems, (
        "workspace baseline drifted; run `make baseline-write` and update AGENTS.md "
        "section 5:\n  - " + "\n  - ".join(problems)
    )


def test_locked_contract_covers_only_v1(baseline: ModuleType) -> None:
    failures = [f for f in baseline.check_invariants() if "contract" in f]
    assert not failures, failures
