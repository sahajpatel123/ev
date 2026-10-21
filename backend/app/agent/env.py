"""Load the owner's env for local CLI runs — keys only, never printed.

The server normally runs with its environment exported (launchd or a shell
that sourced ``.env``). ``python -m app.agent`` usually does not, so this
helper fills the gaps the same way the behavioral-harness runner does: the repo
``.env`` first, then ``~/.ev/secrets/production.env``, never overriding a
value the operator already exported. Keys are then pushed into the live
``Settings`` instance so every module that imported ``settings`` sees them.

Returns only the key NAMES that were loaded; values never leave the process.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _env_files() -> list[Path]:
    files = [REPO_ROOT / ".env"]
    overlay = Path(
        os.environ.get("EV_SECRETS_FILE", "~/.ev/secrets/production.env")
    ).expanduser()
    files.append(overlay)
    return files


def prime_agent_env() -> list[str]:
    """Fill missing env keys from the owner's env files; sync Settings."""
    loaded: dict[str, str] = {}
    try:
        from dotenv import dotenv_values
    except Exception:  # noqa: BLE001 - dotenv is a dev dependency of the repo scripts
        return []
    for path in _env_files():
        if not path.is_file():
            continue
        try:
            for key, value in dotenv_values(path).items():
                if value is None or not key.startswith("EV_"):
                    continue
                if (os.environ.get(key) or "").strip():
                    continue  # explicit operator export wins
                os.environ[key] = value
                loaded[key] = value
        except Exception:  # noqa: BLE001 - a bad env file must not kill the CLI
            continue
    if not loaded:
        return []
    try:
        from app.config import settings

        for key in loaded:
            field = key[3:].lower()
            if field in type(settings).model_fields:
                setattr(settings, field, loaded[key])
    except Exception:  # noqa: BLE001
        pass
    return sorted(loaded)
