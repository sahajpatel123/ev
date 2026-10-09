"""Backfill owner rows from historical events (owner memory).

Runs the owner-fact extractor over past owner-authored events and distills
the survivors into versioned owner rows. Fingerprint dedupe makes re-runs
safe; secrets and assistant speculation are dropped by the distill path.

Dry-run by default (no writes). --apply writes. Consent on the
life_data_personalization track is required for --apply and the run fails
closed without it.

Usage:
    uv run python -m app.scripts.owner_backfill --limit 200
    uv run python -m app.scripts.owner_backfill --apply --limit 2000
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from sqlalchemy import select

OWNER_EVENT_TYPES = ("message.user", "note", "voice")


async def run_backfill(
    *, apply: bool, limit: int, batch: int, after_id: str | None
) -> int:
    from app.db import SessionLocal
    from app.memory.extraction import Extractor
    from app.memory.owner_distill import distill_owner_candidates
    from app.models import Event
    from app.training.consent import active_consent

    after: UUID | None = None
    if after_id:
        try:
            after = UUID(after_id)
        except ValueError:
            print(f"error: --after-id is not a UUID: {after_id}", file=sys.stderr)
            return 2

    async with SessionLocal() as session:
        if apply:
            from app.config import settings

            consent = await active_consent(session, "life_data_personalization")
            if consent is None:
                print(
                    "error: --apply needs an active life_data_personalization "
                    "consent record; refusing to write.",
                    file=sys.stderr,
                )
                return 2
            # Explicit owner action: distill ON for this run regardless of the
            # ambient mode. Dry-runs always ledger without writing.
            settings.owner_distill_mode = "on"
        else:
            from app.config import settings

            settings.owner_distill_mode = "shadow"

        stmt = (
            select(Event)
            .where(
                Event.tombstoned_at.is_(None),
                Event.event_type.in_(OWNER_EVENT_TYPES),
            )
            .order_by(Event.occurred_at.asc(), Event.id.asc())
            .limit(limit + 1 if after else limit)
        )
        events = list((await session.execute(stmt)).scalars().all())
        if after:
            skipped = True
            remaining: list = []
            for event in events:
                if skipped:
                    if event.id == after:
                        skipped = False
                    continue
                remaining.append(event)
            events = remaining[:limit]

        extractor = Extractor()
        seen = 0
        candidates = 0
        written = 0
        dropped_secret = 0
        kinds: dict[str, int] = {}
        last_id: str | None = None
        for index, event in enumerate(events, start=1):
            owner_only = [
                c for c in extractor.extract(event) if getattr(c, "owner_kind", None)
            ]
            if not owner_only:
                last_id = str(event.id)
                continue
            seen += 1
            candidates += len(owner_only)
            for candidate in owner_only:
                kind = candidate.owner_kind
                if kind is None:  # unreachable in practice (pre-filtered)
                    continue
                kinds[kind] = kinds.get(kind, 0) + 1
            summary = await distill_owner_candidates(session, event, owner_only)
            written += summary["written"]
            dropped_secret += summary["dropped_secret"]
            last_id = str(event.id)
            if apply and index % batch == 0:
                await session.commit()
        if apply:
            await session.commit()

    print(
        f"owner backfill ({'APPLY' if apply else 'dry-run'}): "
        f"events_scanned={len(events)} events_with_owner_facts={seen} "
        f"candidates={candidates} written={written} "
        f"dropped_secret={dropped_secret} kinds={kinds}"
    )
    if last_id:
        print(f"last_event_id={last_id} (resume with --after-id)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill owner rows from history.")
    parser.add_argument(
        "--apply", action="store_true", help="Write rows (default is dry-run)."
    )
    parser.add_argument(
        "--limit", type=int, default=500, help="Max events to scan (default 500)."
    )
    parser.add_argument(
        "--batch", type=int, default=50, help="Commit every N events (default 50)."
    )
    parser.add_argument(
        "--after-id", default=None, help="Resume after this event id (exclusive)."
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        run_backfill(
            apply=args.apply, limit=args.limit, batch=args.batch, after_id=args.after_id
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
