"""Settings added on evie-fabric must exist in .env.example and ENVIRONMENT.md.

Pre-existing gaps (Settings fields already missing at pre-fabric-baseline) are
listed and do not fail this test. A field added on this branch, or a new bare
os.getenv("EV_*") in backend/app or backend/clients, fails it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASELINE = "pre-fabric-baseline"


def _fields(src: str) -> set[str]:
    return set(re.findall(r"^    ([a-z][a-z0-9_]*)\s*:", src, re.M))


def _env_name(field: str) -> str:
    return "EV_" + field.upper()


def test_new_settings_are_on_every_surface() -> None:
    current = (REPO / "backend" / "app" / "config.py").read_text()
    base = subprocess.check_output(
        ["git", "show", f"{BASELINE}:backend/app/config.py"],
        cwd=REPO, text=True, timeout=30,
    )
    added = _fields(current) - _fields(base)
    assert added, "expected the fabric settings added on this branch"
    example = (REPO / ".env.example").read_text()
    doc = (REPO / "docs" / "ENVIRONMENT.md").read_text()
    missing = [
        _env_name(name)
        for name in sorted(added)
        if _env_name(name) not in example or _env_name(name) not in doc
    ]
    assert not missing, missing


def _listed(section: str) -> list[str]:
    text = (REPO / "docs" / "CONFIG_DRIFT_PREEXISTING.txt").read_text()
    lines = text.splitlines()
    start = lines.index(section) + 1
    out = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        if line.strip():
            out.append(line.strip())
    return out


def test_preexisting_settings_drift_is_listed_not_ignored() -> None:
    """The inherited gap is the committed list. New gaps fail the other test.

    Documenting a pre-existing field removes it from the computed set and from
    this list in the same change. Deleting the list to go green fails here.
    """
    base = subprocess.check_output(
        ["git", "show", f"{BASELINE}:backend/app/config.py"],
        cwd=REPO, text=True, timeout=30,
    )
    example = (REPO / ".env.example").read_text()
    doc = (REPO / "docs" / "ENVIRONMENT.md").read_text()
    # A setting that was deliberately deleted from Settings is neither a gap nor
    # something to document; only fields that still exist can drift.
    base_fields = _fields(base) & _fields((REPO / "backend" / "app" / "config.py").read_text())
    absent_example = sorted(
        _env_name(name) for name in base_fields if _env_name(name) not in example
    )
    absent_doc = sorted(
        _env_name(name) for name in base_fields if _env_name(name) not in doc
    )
    assert absent_example == _listed("# absent from .env.example")
    assert absent_doc == _listed("# absent from docs/ENVIRONMENT.md")
    assert absent_example and absent_doc


def test_no_new_bare_ev_getenv() -> None:
    diff = subprocess.check_output(
        ["git", "diff", "-U0", BASELINE, "--", "backend/app", "backend/clients"],
        cwd=REPO, text=True, timeout=60, errors="replace",
    )
    added = [
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
        and ('os.getenv("EV_' in line or "os.getenv('EV_" in line)
    ]
    assert added == []
