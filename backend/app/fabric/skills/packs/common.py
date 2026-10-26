"""Shared helpers for skill packs: lazy shared objects on a Ctx, risk helpers, plan cache."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..ax import AxClient
from ..registry import Ctx
from .browsers import BrowserDriver, drivers
from .coding_sessions import SessionIndex
from .ghostty import GhosttyDriver

PLAN_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
PLAN_TTL = 300.0


def state_dir(ctx: Ctx) -> Path:
    if ctx.state_dir:
        d = Path(ctx.state_dir)
    else:
        try:
            from app.config import settings

            d = Path(settings.hands_state_dir).expanduser() if settings.hands_state_dir \
                else ctx.rt.home() / ".ev" / "hands"
        except Exception:
            d = ctx.rt.home() / ".ev" / "hands"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ax(ctx: Ctx) -> AxClient:
    if ctx.ax is None:
        ctx.ax = AxClient(ctx.rt)
    return ctx.ax


def ghostty(ctx: Ctx) -> GhosttyDriver:
    if ctx.ghostty is None:
        ctx.ghostty = GhosttyDriver(ctx.rt, ax(ctx))
    return ctx.ghostty


def browsers(ctx: Ctx) -> list[BrowserDriver]:
    if ctx.browsers is None:
        ctx.browsers = drivers(ctx.rt)
    return ctx.browsers


def session_index(ctx: Ctx) -> SessionIndex:
    if ctx.session_index is None:
        ctx.session_index = SessionIndex(ctx.rt, ctx.home)
    return ctx.session_index


def cached_plan(key: str) -> dict[str, Any] | None:
    hit = PLAN_CACHE.get(key)
    if hit and time.time() - hit[0] < PLAN_TTL:
        return hit[1]
    PLAN_CACHE.pop(key, None)
    return None


def cache_plan(key: str, plan: dict[str, Any]) -> None:
    PLAN_CACHE[key] = (time.time(), plan)


def shell_risk(command: str) -> str | None:
    """High-risk class of a shell command per the one policy engine (None = ordinary)."""
    from app.fabric import policy

    return policy.risk_class("shell", arguments={"cmd": command})
