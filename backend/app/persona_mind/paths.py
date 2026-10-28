"""Filesystem paths. Our SQLite file lives next to the existing data dir.

Resolution: EV_PERSONA_MIND_DB wins (tests, tooling); otherwise the file is
``persona_mind/mind.sqlite`` under the existing ``storage_root`` setting;
otherwise ``./storage/persona_mind/mind.sqlite``. The settings import is lazy
and cached so the fast path never pays it when the env var is set.
"""

from __future__ import annotations

import contextvars
import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

ENV_VAR = "EV_PERSONA_MIND_DB"
RELATIVE_DB = Path("persona_mind") / "mind.sqlite"

_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mind_db_override", default=None
)


@contextmanager
def db_scope(path: str | Path) -> Iterator[str]:
    """Temporarily point all store access at another database file."""
    token = _override.set(str(path))
    try:
        yield str(path)
    finally:
        _override.reset(token)


@lru_cache(maxsize=1)
def storage_root() -> Path:
    try:
        from app.config import settings

        raw = (getattr(settings, "storage_root", "") or "").strip()
        if raw:
            return Path(raw).expanduser()
    except Exception:
        pass
    return Path("./storage")


def mind_db_path() -> Path:
    override = (os.environ.get(ENV_VAR) or "").strip()
    if override:
        return Path(override).expanduser()
    return storage_root() / RELATIVE_DB


def mind_db_path_str() -> str:
    """Active database locator: scope override, env var, or default."""
    scoped = _override.get()
    if scoped:
        return scoped
    return str(mind_db_path())


def read_only_uri(path: str | Path) -> str:
    """SQLite read-only URI for a database file (inspector use)."""
    return f"file:{Path(path).expanduser().resolve()}?mode=ro"


def lock_path() -> Path:
    return mind_db_path().with_suffix(".lock")


def dream_lock_path() -> Path:
    return mind_db_path().with_name(mind_db_path().stem + ".dream.lock")
