"""Bounded offline delivery queue for the ambient collector.

The queue is a JSONL file under ``~/.ev/collector_queue`` (override with
``EV_COLLECTOR_QUEUE_DIR``).  It is bounded two ways: a maximum record count
(default 10 000) and a maximum file size (default 8 MiB); when a bound is hit
the oldest records are dropped first, so a long outage cannot grow without
limit.  Every record carries an idempotency key plus the full delivery
envelope (channel, kind, privacy level, events), so a replay after an outage
is a byte-identical re-post and the server's ``(channel_id, sha256)``
content-hash dedupe turns duplicates into no-ops.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

QUEUE_FILENAME = "pending.jsonl"
DEFAULT_MAX_RECORDS = 10_000
DEFAULT_MAX_BYTES = 8 * 1024 * 1024


def _queue_dir() -> Path:
    override = os.environ.get("EV_COLLECTOR_QUEUE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".ev" / "collector_queue"


def _max_records() -> int:
    raw = os.environ.get("EV_COLLECTOR_QUEUE_MAX_RECORDS")
    if raw and raw.isdigit():
        return max(1, int(raw))
    return DEFAULT_MAX_RECORDS


def _max_bytes() -> int:
    raw = os.environ.get("EV_COLLECTOR_QUEUE_MAX_BYTES")
    if raw and raw.isdigit():
        return max(1024, int(raw))
    return DEFAULT_MAX_BYTES


class CollectorQueue:
    """Append-only JSONL queue with FIFO bounds and idempotency keys."""

    def __init__(
        self,
        queue_dir: Path | str | None = None,
        *,
        max_records: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.queue_dir = Path(queue_dir) if queue_dir is not None else _queue_dir()
        self.max_records = max_records if max_records is not None else _max_records()
        self.max_bytes = max_bytes if max_bytes is not None else _max_bytes()

    @property
    def path(self) -> Path:
        return self.queue_dir / QUEUE_FILENAME

    def records(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[dict] = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def enqueue(
        self,
        *,
        channel: str,
        kind: str,
        events: list[dict],
        channel_id: str | None,
        privacy_level: str,
    ) -> dict:
        """Persist one delivery record; drops oldest records past the bounds."""

        # Freeze the observation time so a replay is byte-identical and the
        # server's (channel_id, sha256) content hash can dedupe it.  Records
        # that already carry occurred_at (e.g. from the live collector) are
        # preserved verbatim.
        occurred_at = datetime.now(UTC).isoformat()
        normalized_events = [
            {**event, "occurred_at": event.get("occurred_at") or occurred_at}
            for event in events
        ]
        record = {
            "idempotency_key": f"collector-{uuid4()}",
            "queued_at": datetime.now(UTC).isoformat(),
            "channel": channel,
            "kind": kind,
            "channel_id": channel_id,
            "privacy_level": privacy_level,
            "events": normalized_events,
        }
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        self._trim()
        return {
            "queued": True,
            "idempotency_key": record["idempotency_key"],
            "queued_at": record["queued_at"],
        }

    def remove(self, idempotency_keys: set[str]) -> int:
        """Remove delivered/quarantined records; returns how many vanished."""

        before = self.records()
        remaining = [record for record in before if record.get("idempotency_key") not in idempotency_keys]
        if len(remaining) != len(before):
            self._write(remaining)
        return len(before) - len(remaining)

    def prune(self) -> None:
        """Rewrite the queue with only valid records (drops corrupt lines)."""

        self._write(self.records())

    def summary(self) -> dict:
        records = self.records()
        return {
            "count": len(records),
            "bytes": self._size_of(records),
        }

    def __len__(self) -> int:
        return len(self.records())

    def _trim(self) -> None:
        records = self.records()
        if len(records) <= self.max_records and self._size_of(records) <= self.max_bytes:
            return
        while len(records) > self.max_records:
            records.pop(0)
        while self._size_of(records) > self.max_bytes and len(records) > 1:
            records.pop(0)
        self._write(records)

    @staticmethod
    def _size_of(records: list[dict]) -> int:
        return sum(len(json.dumps(record, sort_keys=True).encode("utf-8")) + 1 for record in records)

    def _write(self, records: list[dict]) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        tmp.replace(self.path)
