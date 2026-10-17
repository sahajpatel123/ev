"""Facade: turn.py re-exports run_turn per assignment target."""
from __future__ import annotations

from typing import Any, Callable

from .run_turn import run_turn


class CancelToken:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def turn(prompt: str, root=None, **kw) -> dict:
    return run_turn(prompt, root=root, **kw)


def iphone_turn(prompt: str, root=None, **kw) -> dict:
    return run_turn(prompt, root=root, **kw)


__all__ = ["CancelToken", "run_turn", "turn", "iphone_turn"]
