"""Bootstrap: replay existing conversation history through observe+reflect.

Read-only against the event store. Pairs message.user / message.assistant
events per conversation into turns since --since, then drains them through
the worker's drain_once (gateway reflector when the brain can serve).

Usage: python -m app.persona_mind.replay --since 2026-01-01 [--limit 500]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from typing import Any


def _parse_since(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def _fetch_events(since: datetime, limit: int) -> list[Any]:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Event

    async with SessionLocal() as session:
        stmt = (
            select(Event)
            .where(
                Event.occurred_at >= since,
                Event.tombstoned_at.is_(None),
                Event.event_type.in_(["message.user", "message.assistant"]),
            )
            .order_by(Event.occurred_at.asc(), Event.id.asc())
            .limit(max(1, min(limit, 5000)))
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


def _text_of(event: Any) -> str:
    content = getattr(event, "content", None) or {}
    if isinstance(content, dict):
        return str(content.get("text") or "")
    return ""


def _pair_turns(events: list[Any], owner_id: str) -> list[dict[str, Any]]:
    try:
        from app.persona.reactions import classify_reaction
    except Exception:
        classify_reaction = None  # type: ignore[assignment]

    turns: list[dict[str, Any]] = []
    pending: dict[str, Any] = {}
    index = 0
    for event in events:
        thread = str(getattr(event, "conversation_id", "") or "default")
        occurred = getattr(event, "occurred_at", None) or datetime.now(UTC)
        if getattr(event, "event_type", "") == "message.user":
            pending[thread] = {"msg": _text_of(event), "ts": occurred}
            continue
        user = pending.pop(thread, None)
        if user is None:
            continue
        reaction = None
        if classify_reaction is not None:
            try:
                reaction = classify_reaction(user["msg"])
            except Exception:
                reaction = None
        index += 1
        turns.append({
            "owner_id": owner_id,
            "thread_id": thread,
            "turn_id": f"replay-{event.id}-{index}",
            "ts": occurred,
            "owner_msg": user["msg"],
            "evie_reply": _text_of(event),
            "channel": "text",
            "audience": "private",
            "tone": {},
            "reaction_to_prev": reaction,
            "receipts": (),
            "now_line": None,
        })
    return turns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay history into persona_mind.")
    parser.add_argument("--since", required=True, help="ISO date, e.g. 2026-01-01")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--owner-id", default="owner")
    args = parser.parse_args(argv)

    from app.persona_mind import settings as mind_settings

    if not mind_settings.MIND_ENABLED:
        print("MIND_ENABLED is False; refusing to write state. Flip it to replay.",
              file=sys.stderr)
        return 2

    since = _parse_since(args.since)
    events = asyncio.run(_fetch_events(since, args.limit))
    turns = _pair_turns(events, args.owner_id)
    print(f"fetched {len(events)} events -> {len(turns)} turns since {since.date()}")

    from app.persona_mind.fast import observe
    from app.persona_mind.types import TurnRecord
    from app.persona_mind.worker import drain_once

    for turn in turns:
        observe(TurnRecord(**turn))
    passes = 0
    while True:
        report = drain_once()
        passes += 1
        if report["reflected"] == 0 or passes > 50:
            break
    print(f"replayed {len(turns)} turns in {passes} drain passes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
