"""Load and validate the authored corpus.

Cases live in ``gauntlet/v2/cases/<domain>.py``. Each module exports
``CASES``. Oracles are fixed in those files before any run (R9). The
production code never imports this package (anti-cheat lint).
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

REQUIRED = ("id", "domain", "split", "difficulty", "turns", "oracle", "expect")
SPLITS = {"dev", "fresh"}
DIFFICULTY = {"easy", "medium", "hard"}


def _validate(case: dict[str, Any]) -> list[str]:
    issues = [f"missing {k}" for k in REQUIRED if k not in case]
    if case.get("split") not in SPLITS:
        issues.append(f"bad split {case.get('split')!r}")
    if case.get("difficulty") not in DIFFICULTY:
        issues.append(f"bad difficulty {case.get('difficulty')!r}")
    if not case.get("turns") or not str(case["turns"][0].get("say") or "").strip():
        issues.append("no first turn")
    if not case.get("oracle"):
        issues.append("empty oracle")
    return issues


def load_corpus(*, strict: bool = True) -> list[dict[str, Any]]:
    from gauntlet.v2 import cases as pkg

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    problems: list[str] = []
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
        module = importlib.import_module(f"gauntlet.v2.cases.{info.name}")
        for case in getattr(module, "CASES", []):
            issues = _validate(case)
            if case.get("id") in seen:
                issues.append("duplicate id")
            seen.add(case.get("id"))
            if issues:
                problems.append(f"{info.name}:{case.get('id')}: {issues}")
            out.append(case)
    if strict and problems:
        raise ValueError("corpus invalid:\n" + "\n".join(problems[:40]))
    return out
