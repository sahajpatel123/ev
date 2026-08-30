"""Private materialized Memory OS directory. Never the source of truth.

Default: ~/Library/Application Support/EV/memory/
Tests/dev: EV_MEMORY_DIR (never the git checkout).
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import suppress
from pathlib import Path
from typing import Any

from app.config import settings

_DIR_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR  # 0o700
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0o600


def memory_root() -> Path:
    raw = (settings.memory_dir or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    if settings.environment == "test":
        return Path(settings.storage_root).expanduser().resolve() / "ev-memory"
    return (Path.home() / "Library/Application Support/EV/memory").resolve()


def ensure_tree() -> Path:
    root = memory_root()
    for part in (
        root,
        root / "journal",
        root / "cards",
        root / "cards" / "projects",
        root / "cards" / "people",
        root / "cards" / "preferences",
        root / "cards" / "episodes",
        root / "cache",
        root / "diagnostics",
    ):
        part.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(part, _DIR_MODE)
    return root


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write temp → replace. Never leave a half-written card as the live file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        os.chmod(path.parent, _DIR_MODE)
    tmp = path.with_name(path.name + ".tmp")
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    tmp.write_text(body, encoding="utf-8")
    with suppress(OSError):
        os.chmod(tmp, _FILE_MODE)
    os.replace(tmp, path)
    with suppress(OSError):
        os.chmod(path, _FILE_MODE)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    with suppress(OSError):
        os.chmod(path, _FILE_MODE)
        os.chmod(path.parent, _DIR_MODE)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None
