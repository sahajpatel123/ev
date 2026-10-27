"""Eval-key resolution for real-brain runs (GE2 conformant). Never spends live.

Resolution order:

1. ``EV_GROWTH_EVAL=1`` (growth runner child): ``EV_OPENROUTER_API_KEY`` from
   the process env, which ``app.growth.keys.suite_env`` routed from the eval
   key after stripping every live credential.
2. ``EV_OPENROUTER_EVAL_KEY`` in the process env (direct export).
3. ``GAUNTLET_EVAL_KEY_FILE`` — explicit dotenv file (CI / captain wiring).
4. The growth store: ``settings.openrouter_eval_key`` else
   ``~/.ev/secrets/eval.env`` (``ev keys set openrouter-eval``).

Empty result means evaluation cannot run: callers refuse with an
OWNER-ACTION instead of falling back to the live key.
"""

from __future__ import annotations

import os
from pathlib import Path


def _read_key_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("EV_OPENROUTER_EVAL_KEY="):
            return stripped.split("=", 1)[1].strip().strip("\"'")
        if stripped.startswith("EV_OPENROUTER_API_KEY="):
            return stripped.split("=", 1)[1].strip().strip("\"'")
    stripped = text.strip()
    if stripped and "\n" not in stripped and not stripped.startswith("#") and "=" not in stripped:
        return stripped
    return ""


def eval_openrouter_key() -> str:
    """The eval OpenRouter key, or "" when no eval key exists. Never logs."""
    if os.environ.get("EV_GROWTH_EVAL") == "1":
        return os.environ.get("EV_OPENROUTER_API_KEY", "").strip()
    direct = os.environ.get("EV_OPENROUTER_EVAL_KEY", "").strip()
    if direct:
        return direct
    override = os.environ.get("GAUNTLET_EVAL_KEY_FILE", "").strip()
    if override:
        return _read_key_file(Path(override).expanduser())
    try:
        from app.growth.keys import load_eval_key

        return load_eval_key().strip()
    except Exception:  # noqa: BLE001 - growth may predate GE2 in old trees
        return ""
