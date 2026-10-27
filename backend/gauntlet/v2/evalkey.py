"""Eval-key resolution for real-brain runs. Never spends the live key.

Real-brain evaluation (scorecard, DEV/FRESH) spends only the eval key.
Lookup order:

1. ``GAUNTLET_EVAL_KEY_FILE`` — explicit dotenv file (CI / captain wiring).
2. ``~/.ev/secrets/openrouter-eval.key`` — the ``ev keys set openrouter-eval``
   store once the release captain ships it (bare key or ``NAME=value``).

Empty result means evaluation cannot run: callers refuse with an
OWNER-ACTION instead of falling back to the live key. Conforms to
``docs/contracts/EVAL_SUITE.md`` when that file lands (this module is the
one seam to change).
"""

from __future__ import annotations

import os
from pathlib import Path


def eval_key_file() -> Path | None:
    raw = os.environ.get("GAUNTLET_EVAL_KEY_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    default = Path.home() / ".ev" / "secrets" / "openrouter-eval.key"
    return default if default.is_file() else None


def eval_openrouter_key() -> str:
    """The eval OpenRouter key, or "" when no eval key exists. Never logs."""
    path = eval_key_file()
    if path is None or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("EV_OPENROUTER_API_KEY="):
            return stripped.split("=", 1)[1].strip().strip("\"'")
        if stripped.startswith("OPENROUTER_API_KEY="):
            return stripped.split("=", 1)[1].strip().strip("\"'")
    stripped = text.strip()
    if stripped and "\n" not in stripped and not stripped.startswith("#") and "=" not in stripped:
        return stripped
    return ""
