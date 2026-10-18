"""Lane roots (D1): where fabric file/shell lanes may operate.

Fixed roots: ~/Desktop, ~/Documents, ~/Downloads, ~/Code, ~/Projects, plus
registered projects (EV_FABRIC_ROOTS extras), plus per-call explicit roots
(tests pass tmp dirs). Only directories that already exist are offered;
roots are never created as a side effect of resolving them.
"""
from __future__ import annotations

from pathlib import Path

_D1_SUBDIRS = ("Desktop", "Documents", "Downloads", "Code", "Projects")


def _home() -> Path:
    return Path.home()


def default_roots(*, home: Path | None = None, extras: str | None = None) -> list[Path]:
    """D1 roots that exist, plus configured extras that exist."""
    base = Path(str(home)) if home is not None else _home()
    out: list[Path] = []
    for sub in _D1_SUBDIRS:
        candidate = base / sub
        if candidate.is_dir():
            out.append(candidate.resolve())
    if extras is None:
        from app.config import settings

        extras = str(getattr(settings, "fabric_roots", "") or "")
    for raw in (extras or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            resolved = candidate.resolve()
            if resolved not in out:
                out.append(resolved)
    return out


def lane_root(*, explicit=None) -> Path:
    """Root for one lane call: explicit wins, else first default, else home."""
    if explicit is not None:
        return Path(str(explicit)).resolve()
    defaults = default_roots()
    if defaults:
        return defaults[0]
    return _home().resolve()
