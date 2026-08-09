"""Consent-gated training corpus harvesting (prerequisite for any fine-tuning).

Builds versioned snapshots from rated response logs, filter-ledger final
texts, and normal events. Never includes `never_send_to_model` or sensitive
content; credentials are redacted before storage; snapshots are reproducible
(deterministic content hash), rollback-able, and fully erasable.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event, FilterLedger, ResponseLog, TrainingCorpusSnapshot
from app.security.boundary import redact_secrets
from app.services.access_log import log_access
from app.training.consent import require_consent
from app.utils.text import canonical_json, sha256_hex

TRACK = "training_corpus"
EXCLUDED_PRIVACY = {"never_send_to_model", "sensitive"}
EXCLUDED_FILTER_STAGES = {"input"}


def _entry_hash(entry: dict) -> str:
    return sha256_hex(canonical_json(entry))[:32]


def _scrub(text: str) -> str:
    return redact_secrets(text or "")


async def _rated_response_entries(session: AsyncSession) -> list[dict]:
    logs = list((await session.execute(select(ResponseLog))).scalars().all())
    entries: list[dict] = []
    for log in logs:
        signals = {
            "was_useful": log.was_useful,
            "followed_recommendation": log.followed_recommendation,
            "was_correction": log.was_correction,
            "intervention_appropriate": log.intervention_appropriate,
        }
        if not any(value is not None for value in signals.values()):
            continue
        user_entry = {
            "kind": "response",
            "role": "user",
            "text": _scrub(log.request_text),
            "source": f"response_log:{log.id}",
            "signals": {"mode": log.mode, "rated": True},
        }
        assistant_entry = {
            "kind": "response",
            "role": "assistant",
            "text": _scrub(log.reply_text),
            "source": f"response_log:{log.id}",
            "signals": {key: value for key, value in signals.items() if value is not None},
        }
        entries.extend([user_entry, assistant_entry])
    return entries


async def _filter_entries(session: AsyncSession) -> list[dict]:
    rows = list(
        (
            await session.execute(
                select(FilterLedger).where(FilterLedger.final_text.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    entries: list[dict] = []
    for row in rows:
        text = (row.final_text or "").strip()
        if row.stage in EXCLUDED_FILTER_STAGES or not text:
            continue
        entries.append(
            {
                "kind": "filter",
                "role": "assistant",
                "text": _scrub(text),
                "source": f"filter_ledger:{row.id}",
                "signals": {
                    "stage": row.stage,
                    "action": row.action,
                    "iterations": row.iterations,
                },
            }
        )
    return entries


async def _event_entries(session: AsyncSession) -> tuple[list[dict], int]:
    rows = list(
        (
            await session.execute(
                select(Event).where(Event.tombstoned_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    entries: list[dict] = []
    excluded = 0
    for event in rows:
        if event.privacy_level in EXCLUDED_PRIVACY:
            excluded += 1
            continue
        text = (event.content or {}).get("text")
        if not text or not str(text).strip():
            continue
        entries.append(
            {
                "kind": "event",
                "role": "user",
                "text": _scrub(str(text)),
                "source": f"event:{event.id}",
                "signals": {
                    "source": event.source,
                    "event_type": event.event_type,
                    "privacy_level": event.privacy_level,
                },
            }
        )
    return entries, excluded


async def current_snapshot(session: AsyncSession) -> TrainingCorpusSnapshot | None:
    result = await session.execute(
        select(TrainingCorpusSnapshot)
        .where(TrainingCorpusSnapshot.is_current.is_(True))
        .order_by(TrainingCorpusSnapshot.version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def build_snapshot(
    session: AsyncSession,
    *,
    actor: str,
    reason: str | None = None,
) -> tuple[TrainingCorpusSnapshot, int]:
    """Harvest a new versioned corpus snapshot. Requires active consent."""
    consent = await require_consent(session, TRACK)
    response_entries = await _rated_response_entries(session)
    filter_entries = await _filter_entries(session)
    event_entries, excluded = await _event_entries(session)

    entries = sorted(
        [*response_entries, *filter_entries, *event_entries],
        key=lambda entry: (entry["kind"], entry["source"], entry["text"]),
    )
    for entry in entries:
        entry["hash"] = _entry_hash(entry)

    source_counts = {
        "response_log": len(response_entries),
        "filter_ledger": len(filter_entries),
        "events": len(event_entries),
    }
    content_hash = sha256_hex(canonical_json(entries))
    current = await current_snapshot(session)
    max_version = max(
        (
            (
                await session.execute(
                    select(TrainingCorpusSnapshot.version)
                )
            )
            .scalars()
            .all()
        ),
        default=0,
    )
    version = max_version + 1
    if current is not None:
        current.is_current = False
    snapshot = TrainingCorpusSnapshot(
        version=version,
        is_current=True,
        entries=entries,
        source_counts=source_counts,
        entry_count=len(entries),
        content_hash=content_hash,
        reason_for_change=reason or "consent-gated corpus harvest",
        consent_id=consent.id,
        supersedes_id=current.id if current is not None else None,
    )
    session.add(snapshot)
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="corpus_build",
        endpoint="POST /v1/training/corpus/build",
        resource_type="training_corpus",
        resource_ids=[snapshot.id],
        details={
            "version": version,
            "entry_count": len(entries),
            "excluded": excluded,
            "content_hash": content_hash,
        },
    )
    return snapshot, excluded


async def list_snapshots(session: AsyncSession) -> list[TrainingCorpusSnapshot]:
    result = await session.execute(
        select(TrainingCorpusSnapshot).order_by(TrainingCorpusSnapshot.version.desc())
    )
    return list(result.scalars().all())


async def get_snapshot(session: AsyncSession, version: int) -> TrainingCorpusSnapshot:
    result = await session.execute(
        select(TrainingCorpusSnapshot).where(TrainingCorpusSnapshot.version == version)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise KeyError(f"Corpus snapshot version {version} not found")
    return row


async def rollback(
    session: AsyncSession,
    *,
    target_version: int,
    actor: str,
    reason: str | None = None,
) -> TrainingCorpusSnapshot:
    await require_consent(session, TRACK)
    target = await get_snapshot(session, target_version)
    current = await current_snapshot(session)
    if current is not None and current.id != target.id:
        current.is_current = False
    target.is_current = True
    if reason:
        target.reason_for_change = reason
    await log_access(
        session,
        actor=actor,
        action="corpus_rollback",
        endpoint="POST /v1/training/corpus/rollback",
        resource_type="training_corpus",
        resource_ids=[target.id],
        details={"target_version": target_version},
    )
    return target


async def redact_snapshot(
    session: AsyncSession,
    snapshot: TrainingCorpusSnapshot,
    *,
    reason: str,
) -> None:
    """Redact one snapshot: entries, hash, and counts are removed."""
    snapshot.entries = []
    snapshot.source_counts = {}
    snapshot.entry_count = 0
    snapshot.content_hash = None
    snapshot.is_current = False
    snapshot.redacted = True
    snapshot.reason_for_change = f"{reason} (redacted)"


async def delete_all(
    session: AsyncSession,
    *,
    actor: str,
    reason: str,
) -> int:
    rows = list((await session.execute(select(TrainingCorpusSnapshot))).scalars().all())
    for row in rows:
        await redact_snapshot(session, row, reason=reason)
    await log_access(
        session,
        actor=actor,
        action="corpus_delete",
        endpoint="POST /v1/training/corpus/delete",
        resource_type="training_corpus",
        resource_ids=[r.id for r in rows],
        details={"redacted": len(rows), "reason": reason},
    )
    return len(rows)


async def delete_due_snapshots(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    reason: str = "retention policy",
) -> int:
    """Redact snapshots whose retention window has expired (compliance sweep)."""
    from app.compliance.policy import TRAINING_SNAPSHOT, deletion_due

    rows = list(
        (
            await session.execute(
                select(TrainingCorpusSnapshot).where(
                    TrainingCorpusSnapshot.redacted.is_(False)
                )
            )
        )
        .scalars()
        .all()
    )
    count = 0
    for row in rows:
        if deletion_due(TRAINING_SNAPSHOT, row.created_at, now=now):
            await redact_snapshot(session, row, reason=reason)
            count += 1
    return count
