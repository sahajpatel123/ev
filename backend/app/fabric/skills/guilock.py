"""Python face of scripts/gui-lock (docs/contracts/GUI_LOCK.md).

The protocol lives in the stdlib script so any session can use it without
this package; this module loads that same file so the two cannot drift.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import os
from collections.abc import Iterator
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "gui-lock"
_mod = None


class GuiLockBusy(RuntimeError):
    def __init__(self, holder: dict):
        super().__init__(f"GUI lock held by {holder.get('owner')} (pid {holder.get('pid')}, "
                         f"{holder.get('age_s')}s old)")
        self.holder = holder


def _load():
    global _mod
    if _mod is None:
        loader = importlib.machinery.SourceFileLoader("hands_gui_lock", str(_SCRIPT))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        if spec is None:
            raise RuntimeError(f"gui-lock script not found at {_SCRIPT}")
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        _mod = module
    return _mod


def status() -> dict:
    return _load().status()


@contextlib.contextmanager
def gui_lock(owner: str = "CLI4", purpose: str = "", *, wait_s: float = 0.0,
             pid: int | None = None) -> Iterator[dict]:
    """Hold the shared GUI lock for the block; always released on exit."""
    mod = _load()
    ok, info = mod.acquire(owner, pid or os.getpid(), purpose, wait_s)
    if not ok:
        raise GuiLockBusy(info)
    try:
        yield info
    finally:
        mod.release(owner)
