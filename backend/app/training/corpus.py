"""Consent-gated training corpus harvesting (prerequisite for any fine-tuning).

Builds versioned snapshots from rated response logs, filter-ledger final
texts, and normal events. Never includes `never_send_to_model` or sensitive
content; credentials are redacted before storage; snapshots are reproducible
(deterministic content hash), rollback-able, and fully erasable.
"""

from __future__ import annotations

import json
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
EXPORT_FORMATS = ("canonical", "sft", "preference", "tool")


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
        signals_payload: dict[str, object] = {
            "mode": log.mode,
            **{key: value for key, value in signals.items() if value is not None},
        }
        tool_calls = _tool_calls_from(strategy=log.strategy)
        if tool_calls:
            signals_payload["tool_calls"] = tool_calls
        assistant_entry = {
            "kind": "response",
            "role": "assistant",
            "text": _scrub(log.reply_text),
            "source": f"response_log:{log.id}",
            "signals": signals_payload,
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
        signals_payload: dict[str, object] = {
            "stage": row.stage,
            "action": row.action,
            "iterations": row.iterations,
            "draft": _scrub(row.draft) if row.draft else None,
        }
        tool_calls = _tool_calls_from(detail=row.detail)
        if tool_calls:
            signals_payload["tool_calls"] = tool_calls
        entries.append(
            {
                "kind": "filter",
                "role": "assistant",
                "text": _scrub(text),
                "source": f"filter_ledger:{row.id}",
                "signals": signals_payload,
            }
        )
    return entries


def _tool_calls_from(*, strategy: dict | None = None, detail: dict | None = None) -> list[dict]:
    """Extract deterministic tool-call records from strategy/detail payloads."""

    raw = strategy or detail or {}
    for key in ("tool_calls", "tools", "tool_call"):
        value = raw.get(key)
        if isinstance(value, list):
            calls = [v for v in value if isinstance(v, dict)]
            if calls:
                return calls
        if isinstance(value, dict):
            return [value]
    return []


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


def _record_allowed(entry: dict) -> bool:
    """Defensive export gate: never re-export excluded privacy levels."""

    signals = entry.get("signals") or {}
    return signals.get("privacy_level") not in EXCLUDED_PRIVACY


def dataset_records(entries: list[dict]) -> list[dict]:
    """Convert corpus snapshot entries to canonical fine-tune records.

    Each JSONL record carries ``input``, ``output``, and ``signals``:

    - response-log entries are paired by source (user request -> assistant reply);
    - filter-ledger entries become draft -> final text;
    - events become single-turn input records (empty output), so they can be
      dropped by a provider that only accepts paired examples.

    Text is re-scrubbed here so the exported artifact can never carry a
    credential even if a snapshot entry was created by an older, less strict
    harvester.
    """

    pairs: dict[str, dict[str, dict]] = {}
    for entry in entries:
        if (
            entry.get("kind") == "response"
            and entry.get("role") in ("user", "assistant")
            and str(entry.get("source", "")).startswith("response_log:")
        ):
            pairs.setdefault(str(entry["source"]), {})[str(entry["role"])] = entry

    records: list[dict] = []
    for source in sorted(pairs):
        user = pairs[source].get("user") or {}
        assistant = pairs[source].get("assistant") or {}
        if not _record_allowed(user) or not _record_allowed(assistant):
            continue
        output = str(assistant.get("text") or "").strip()
        if not output:
            continue
        signals = dict(user.get("signals") or {})
        signals.update(assistant.get("signals") or {})
        records.append(
            {
                "kind": "response",
                "input": _scrub(str(user.get("text") or "")),
                "output": _scrub(output),
                "signals": signals,
                "source": source,
            }
        )

    for entry in sorted(
        entries, key=lambda e: (e.get("kind", ""), e.get("source", ""), e.get("text", ""))
    ):
        if entry.get("kind") != "filter" or not _record_allowed(entry):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        signals = dict(entry.get("signals") or {})
        records.append(
            {
                "kind": "filter",
                "input": _scrub(str(signals.get("draft") or "")),
                "output": _scrub(text),
                "signals": signals,
                "source": str(entry.get("source", "")),
            }
        )

    for entry in sorted(
        entries, key=lambda e: (e.get("kind", ""), e.get("source", ""), e.get("text", ""))
    ):
        if entry.get("kind") != "event" or not _record_allowed(entry):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        records.append(
            {
                "kind": "event",
                "input": _scrub(text),
                "output": "",
                "signals": dict(entry.get("signals") or {}),
                "source": str(entry.get("source", "")),
            }
        )

    for record in records:
        record["hash"] = _entry_hash(record)
    return records


def format_records(records: list[dict], fmt: str = "canonical") -> list[dict]:
    """Render canonical records into a provider-facing training format.

    Supported formats:

    - ``canonical``: the original input/output/signals records.
    - ``sft``: instruction/response pairs (events with empty output dropped).
    - ``preference``: chosen/rejected pairs for DPO-style training. Filter-ledger
      draft -> final rows are chosen/rejected directly; response rows are emitted
      only when a ``corrected_text`` signal is present (the corrected reply).
    - ``tool``: instruction/response rows carrying a JSON tool call for
      tool-schema teaching.

    Every format re-applies the privacy filter and credential scrub so a
    ``never_send_to_model``/``sensitive`` row or a planted secret can never
    cross the export boundary, and every emitted record is content-hashed.
    Ordering is deterministic (sorted by source/hash) for reproducibility.
    """

    if fmt == "canonical":
        return list(records)
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"Unknown dataset format {fmt!r}; expected one of {EXPORT_FORMATS}")

    rendered: list[dict] = []
    for record in sorted(
        records,
        key=lambda r: (str(r.get("source", "")), str(r.get("hash", "")), str(r.get("input", ""))),
    ):
        if not _record_allowed(record):
            continue
        signals = dict(record.get("signals") or {})
        source = str(record.get("source", ""))
        instruction = _scrub(str(record.get("input") or ""))
        output = _scrub(str(record.get("output") or ""))
        if fmt == "sft":
            if not output:
                continue
            rendered.append(
                {
                    "kind": "sft",
                    "instruction": instruction,
                    "output": output,
                    "signals": signals,
                    "source": source,
                }
            )
            tool_calls = _tool_calls_from(detail=signals)
            if tool_calls:
                rendered.append(
                    {
                        "kind": "sft",
                        "instruction": instruction,
                        "output": _scrub(
                            json.dumps(tool_calls, ensure_ascii=False, sort_keys=True)
                        ),
                        "signals": {
                            "mode": signals.get("mode"),
                            "tool_teaching": True,
                            "tool_calls": tool_calls,
                        },
                        "source": f"{source}#tool",
                    }
                )
        elif fmt == "preference":
            corrected_text = signals.get("corrected_text")
            if str(record.get("kind")) == "filter" and instruction:
                rendered.append(
                    {
                        "kind": "preference",
                        "prompt": "",
                        "chosen": output,
                        "rejected": instruction,
                        "signals": signals,
                        "source": source,
                    }
                )
            elif corrected_text and output:
                rendered.append(
                    {
                        "kind": "preference",
                        "prompt": instruction,
                        "chosen": _scrub(str(corrected_text)),
                        "rejected": output,
                        "signals": signals,
                        "source": source,
                    }
                )
        elif fmt == "tool":
            tool_calls = _tool_calls_from(detail=signals)
            if not tool_calls or not output:
                continue
            rendered.append(
                {
                    "kind": "tool",
                    "instruction": instruction,
                    "tool_calls": tool_calls,
                    "output": output,
                    "signals": signals,
                    "source": source,
                }
            )

    for record in rendered:
        record["hash"] = _entry_hash(record)
    return rendered


def to_jsonl(records: list[dict]) -> str:
    """Render dataset records as canonical, deterministic NDJSON."""

    if not records:
        return ""
    return "\n".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records
    ) + "\n"


def dataset_summary(records: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for record in records:
        kind = str(record.get("kind") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "record_count": len(records),
        "sources": counts,
        "jsonl_bytes": len(to_jsonl(records).encode("utf-8")),
    }


async def export_dataset(
    session: AsyncSession, version: int, fmt: str = "canonical"
) -> tuple[TrainingCorpusSnapshot, list[dict], str]:
    """Return (snapshot, records, NDJSON payload) for one version and format."""

    row = await get_snapshot(session, version)
    if row.redacted:
        raise ValueError(f"Corpus snapshot v{version} is redacted")
    records = dataset_records(list(row.entries or []))
    formatted = format_records(records, fmt=fmt)
    return row, formatted, to_jsonl(formatted)


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
