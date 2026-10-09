"""Import third-party chat history (Grok export) into the event log.

Each exported turn becomes one event (source ``import``): human turns map to
``message.user``, everything else to ``message.assistant``. Imported events
carry ``metadata={provider, foreign_id, conversation_title}`` and re-runs
skip already-imported foreign_ids. New events run through the full live
pipeline (memory extract+write AND owner distill) but WITHOUT owner state
snapshots — historical affect must not become current state.

--apply forces owner distill ON in-process (explicit owner action) and
requires an active life_data_personalization consent record (fail closed,
exit 2). Dry-run forces shadow and writes nothing. Output is counts and the
last foreign_id only — turn text is never printed or logged.

Grok export shape (keys only): top-level dict with ``conversations`` list;
each item holds ``conversation={id, ..., title}`` and ``responses`` list of
``{response={_id, conversation_id, message, sender, create_time, ...}}``.
``sender`` case varies (human/assistant/ASSISTANT); ``create_time`` is Mongo
extended JSON ``{$date: {$numberLong: <ms epoch str>}}``.

Usage:
    uv run python -m app.scripts.chat_history_import --path export.json
    uv run python -m app.scripts.chat_history_import --path export.json --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ImportedTurn:
    conversation_id: str
    response_id: str
    role: str  # "message.user" | "message.assistant"
    text: str
    occurred_at: datetime
    # Carried so run_import can stamp metadata without re-reading the file.
    conversation_title: str = ""


def _parse_mongo_time(value: object) -> datetime | None:
    """Parse a Grok create_time (Mongo extended JSON ms) defensively."""
    candidate: object = value
    if isinstance(candidate, dict):
        inner = candidate.get("$date", candidate.get("date"))
        if isinstance(inner, dict):
            inner = inner.get("$numberLong", inner.get("numberLong"))
        candidate = inner
    ms: int | None = None
    if isinstance(candidate, bool):
        return None
    if isinstance(candidate, (int, float)):
        ms = int(candidate)
    elif isinstance(candidate, str):
        text = candidate.strip()
        if text.isdigit():
            ms = int(text)
        else:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
    if ms is None:
        return None
    if ms < 0:
        return None
    # Heuristic: 10-digit values are seconds, 13-digit are ms.
    seconds = ms / 1000.0 if ms >= 10_000_000_000 else float(ms)
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_response(
    item: object, conversation_id: str, fallback_at: datetime | None
) -> ImportedTurn | None:
    if not isinstance(item, dict):
        return None
    payload = item.get("response")
    if not isinstance(payload, dict):
        return None
    response_id = payload.get("_id")
    message = payload.get("message")
    if not isinstance(response_id, str) or not response_id:
        return None
    if not isinstance(message, str) or not message.strip():
        return None
    sender = payload.get("sender")
    sender_text = sender.strip().lower() if isinstance(sender, str) else ""
    role = "message.user" if sender_text == "human" else "message.assistant"
    occurred_at = (
        _parse_mongo_time(payload.get("create_time")) or fallback_at
    )
    if occurred_at is None:
        occurred_at = datetime.now(tz=UTC)
    return ImportedTurn(
        conversation_id=conversation_id,
        response_id=response_id,
        role=role,
        text=message,
        occurred_at=occurred_at,
    )


def parse_grok_export(path: str | Path) -> list[ImportedTurn]:
    """Parse a Grok export file into turns. Pure: reads one file, no writes."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    conversations = raw.get("conversations") if isinstance(raw, dict) else None
    if not isinstance(conversations, list):
        return []
    turns: list[ImportedTurn] = []
    for entry in conversations:
        if not isinstance(entry, dict):
            continue
        header = entry.get("conversation")
        if not isinstance(header, dict):
            continue
        conversation_id = header.get("id")
        if not isinstance(conversation_id, str) or not conversation_id:
            continue
        title = header.get("title")
        title_text = title if isinstance(title, str) else ""
        fallback_at = _parse_mongo_time(header.get("create_time"))
        responses = entry.get("responses")
        if not isinstance(responses, list):
            continue
        for item in responses:
            turn = _parse_response(item, conversation_id, fallback_at)
            if turn is None:
                continue
            turns.append(
                ImportedTurn(
                    conversation_id=turn.conversation_id,
                    response_id=turn.response_id,
                    role=turn.role,
                    text=turn.text,
                    occurred_at=turn.occurred_at,
                    conversation_title=title_text,
                )
            )
    return turns


def foreign_id_for(provider: str, turn: ImportedTurn) -> str:
    return f"{provider}:{turn.conversation_id}:{turn.response_id}"


async def _existing_foreign_ids(session: AsyncSession) -> set[str]:
    from app.models import Event

    rows = (
        await session.execute(select(Event.metadata_).where(Event.source == "import"))
    ).scalars().all()
    found: set[str] = set()
    for metadata in rows:
        if not isinstance(metadata, dict):
            continue
        foreign_id = metadata.get("foreign_id")
        if isinstance(foreign_id, str) and foreign_id:
            found.add(foreign_id)
    return found


async def run_import(
    *,
    provider: str,
    path: str,
    apply: bool,
    limit: int,
    batch: int,
    after_foreign_id: str | None,
) -> int:
    """Import turns from a provider export file. Returns an exit code."""
    from app.config import settings
    from app.db import SessionLocal
    from app.schemas import EventCreate
    from app.services.event_service import EventService
    from app.services.processor import process_event_sync
    from app.training.consent import active_consent

    try:
        turns = parse_grok_export(path)
    except FileNotFoundError:
        print(f"error: export file not found: {path}", file=sys.stderr)
        return 2
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        print(f"error: cannot parse export file: {exc}", file=sys.stderr)
        return 2

    if after_foreign_id:
        remaining: list[ImportedTurn] = []
        skipping = True
        for turn in turns:
            if skipping:
                if foreign_id_for(provider, turn) == after_foreign_id:
                    skipping = False
                continue
            remaining.append(turn)
        turns = remaining
    scanned = turns[: max(limit, 0)]

    previous_mode = getattr(settings, "owner_distill_mode", "shadow")
    settings.owner_distill_mode = "on" if apply else "shadow"
    try:
        async with SessionLocal() as session:
            if apply:
                consent = await active_consent(session, "life_data_personalization")
                if consent is None:
                    print(
                        "error: --apply needs an active life_data_personalization "
                        "consent record; refusing to write.",
                        file=sys.stderr,
                    )
                    return 2
            existing = await _existing_foreign_ids(session)
            fresh = [
                turn
                for turn in scanned
                if foreign_id_for(provider, turn) not in existing
            ]
            skipped = len(scanned) - len(fresh)
            if not apply:
                last = (
                    foreign_id_for(provider, scanned[-1]) if scanned else "(none)"
                )
                print(
                    f"chat history import (dry-run): parsed={len(turns)} "
                    f"scanned={len(scanned)} new={len(fresh)} "
                    f"skipped_existing={skipped} last_foreign_id={last}"
                )
                return 0
            new_ids: list[UUID] = []
            last_foreign_id = "(none)"
            service = EventService(session, actor="import")
            created_count = 0
            for turn in fresh:
                foreign_id = foreign_id_for(provider, turn)
                if foreign_id in existing:
                    skipped += 1
                    continue
                created_event = await service.create(
                    EventCreate(
                        source="import",
                        event_type=turn.role,
                        text=turn.text,
                        metadata={
                            "provider": provider,
                            "foreign_id": foreign_id,
                            "conversation_title": turn.conversation_title,
                        },
                        occurred_at=turn.occurred_at,
                    )
                )
                existing.add(foreign_id)
                new_ids.append(created_event.id)
                last_foreign_id = foreign_id
                created_count += 1
                if created_count % max(batch, 1) == 0:
                    await session.commit()
            await session.commit()
            # Full live pipeline per new event, minus owner state snapshots:
            # historical affect must not become current state.
            for event_id in new_ids:
                await process_event_sync(event_id, record_owner_state=False)
            print(
                f"chat history import (APPLY): scanned={len(scanned)} "
                f"imported={len(new_ids)} skipped_existing={skipped} "
                f"last_foreign_id={last_foreign_id}"
            )
            return 0
    finally:
        settings.owner_distill_mode = previous_mode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import chat history exports.")
    parser.add_argument("--provider", default="grok", help="Export provider tag.")
    parser.add_argument("--path", required=True, help="Path to the export JSON file.")
    parser.add_argument(
        "--apply", action="store_true", help="Write events (default is dry-run)."
    )
    parser.add_argument(
        "--limit", type=int, default=10000, help="Max turns to scan (default 10000)."
    )
    parser.add_argument(
        "--batch", type=int, default=50, help="Commit every N events (default 50)."
    )
    parser.add_argument(
        "--after-foreign-id",
        default=None,
        help="Resume after this foreign_id (exclusive).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        run_import(
            provider=args.provider,
            path=args.path,
            apply=args.apply,
            limit=args.limit,
            batch=args.batch,
            after_foreign_id=args.after_foreign_id,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
