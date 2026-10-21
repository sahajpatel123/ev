"""Owner announcements for background agent work (live voice + notification bus).

Kept separate from the kernel bridge and the runner so both can announce
without importing each other. Best effort by design: a dead socket or a
disabled notification backend never changes the task's real state.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("ev.agent.notify")


async def announce(title: str, body: str, *, kind: str = "agent") -> None:
    text = (body or "").strip()
    if not text:
        return
    try:
        from app.voice.live.layer import active_lives, record_proactive_spoken

        record_proactive_spoken(text)
        for live in active_lives():
            if getattr(live, "_closed", False):
                continue
            try:
                await live._speak_code_receipt(text)
            except Exception:  # noqa: BLE001 - one dead live socket never blocks the rest
                continue
    except Exception:  # noqa: BLE001 - no live layer in this process
        pass
    try:
        from app.db import SessionLocal
        from app.notify.service import dispatch_notification

        async with SessionLocal() as session:
            await dispatch_notification(
                session,
                title=title,
                body=text[:2000],
                priority=0.4,
                tier="background",
                kind=kind,
                source="agent",
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - notification channel is optional
        logger.debug("agent notification dispatch skipped", exc_info=True)
