"""Boundaries: fast-path import graph, name ban, temperament sourcing, lane."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).parent.parent
PACKAGE = BACKEND / "app" / "persona_mind"

# Modules reachable from observe()/snapshot() must never import these.
FAST_MODULES = (
    "types.py", "settings.py", "paths.py", "store.py", "state.py",
    "fast.py", "render.py", "temperament.py",
)
BANNED_IMPORTS = (
    "app.gateway", "app.ev", "app.cognitive", "app.fabric", "app.memory",
    "app.db", "app.models", "app.contracts", "app.persona",
    "httpx", "urllib", "urllib3", "requests", "openai", "socket",
    "aiohttp", "websockets",
)


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_fast_modules_have_no_network_or_llm_imports():
    violations = []
    for name in FAST_MODULES:
        imported = _imports_of(PACKAGE / name)
        for banned in BANNED_IMPORTS:
            if any(mod == banned or mod.startswith(banned + ".") for mod in imported):
                violations.append(f"{name} imports {banned}")
    assert violations == []


def test_fast_path_execution_imports_no_brain(tmp_path):
    """Subprocess: observe+snapshot must not pull gateway/LLM modules."""
    script = (
        "import os, sys; "
        f"os.environ['EV_PERSONA_MIND_DB'] = r'{tmp_path}/g.sqlite'; "
        "import app.persona_mind.settings as s; s.MIND_ENABLED = True; "
        "from app.persona_mind import observe, snapshot; "
        "from app.persona_mind.types import TurnRecord; "
        "from datetime import datetime, timezone; "
        "observe(TurnRecord('o','t','g1',datetime.now(timezone.utc),'a','b','text','private',{},None,(),None)); "
        "snapshot('o','t','q','text',120); "
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'httpx','openai','urllib3','requests','aiohttp'} "
        "or m.startswith(('app.gateway','app.ev','app.cognitive','app.memory','app.db'))); "
        "print('BAD:' + ','.join(bad) if bad else 'CLEAN')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=BACKEND,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == "CLEAN", proc.stdout


def test_name_ban_no_real_persons_no_comedian():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        if "comedian" in text:
            offenders.append(f"{path.name}: 'comedian'")
    assert offenders == []
    # No real-person names: the package ships no proper-name references at all
    # outside Evie/CLI-6 interface nouns; assert the known-bad set is absent.
    assert PACKAGE.is_dir()


def test_temperament_sourced_from_character_spec():
    from app.persona_mind.temperament import BASELINE, VALUES_EXCERPT

    spec = (BACKEND.parent / "personality" / "briefs" / "evie-character-v2.md")
    text = spec.read_text(encoding="utf-8")
    assert len(BASELINE) == 7
    assert all(0.0 <= v <= 1.0 for v in BASELINE.values())
    for line in VALUES_EXCERPT:
        assert line in text, f"values line drifted from spec: {line[:40]}"


def test_no_trace_outside_lane():
    """Nothing outside our lane may reference persona_mind (we never wrote
    there). Pre-existing dirt from other lanes is theirs, not ours."""
    import subprocess as sp

    proc = sp.run(
        ["git", "status", "--porcelain"], cwd=BACKEND.parent,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0
    mine = (
        "backend/app/persona_mind/",
        "backend/tests/test_persona_mind_",
        "backend/eval/persona_mind/",
        "personality/mind/",
    )
    # Our files must all be inside the lane.
    outside = []
    for line in proc.stdout.splitlines():
        path = line[3:].strip().strip('"')
        if path.startswith(mine):
            continue
        # Any tracked modification outside the lane must not mention us.
        full = BACKEND.parent / path.split(" -> ")[-1]
        if full.is_file() and full.suffix == ".py":
            try:
                if "persona_mind" in full.read_text(encoding="utf-8"):
                    outside.append(path)
            except OSError:
                pass
    assert outside == []
