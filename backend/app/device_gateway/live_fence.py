"""Exclusive sandbox live audio. Never fences the Mac owner EV.app session."""

from __future__ import annotations

import contextlib
from typing import Any


async def fence_sandbox_lives(*, except_live: Any | None = None) -> int:
    """Close every sandbox companion live socket except the one about to speak."""

    from app.voice.live.events import ConversationMovedEvent
    from app.voice.live.layer import active_lives

    closed = 0
    keep_id = id(except_live) if except_live is not None else None
    for live in list(active_lives()):
        if keep_id is not None and id(live) == keep_id:
            continue
        if getattr(live, "memory_scope", "owner") != "sandbox":
            continue
        now = 0
        with contextlib.suppress(Exception):
            now_fn = getattr(live, "now", None)
            now = int(now_fn()) if callable(now_fn) else 0
        with contextlib.suppress(Exception):
            await live.emit(
                ConversationMovedEvent(
                    at_ms=now,
                    to_device_id=str(getattr(except_live, "device_id", None) or ""),
                    reason="lease",
                )
            )
        with contextlib.suppress(Exception):
            live.close()
        closed += 1
    return closed
