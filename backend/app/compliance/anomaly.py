"""Access-pattern anomaly detection (plan 1.8 direction).

Scans recent access-log entries for behavioral signals that warrant a
security review: bursts of sensitive deletions/erasures, export/backup
exfiltration patterns, and repeated failed/rejected actions. Detection is
rule-based and configurable through thresholds; every scan is itself audited
by the API layer.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccessLog
from app.utils.text import utcnow

DELETION_ACTIONS = {"data_erasure", "voice_delete", "consent_revoke", "corpus_delete"}
EXPORT_ACTIONS = {"export", "voice_export", "backup", "access_log.read"}
FAILURE_MARKERS = ("failed", "rejected", "denied", "locked")


async def detect_access_anomalies(
    session: AsyncSession,
    *,
    window_minutes: int = 60,
    deletion_threshold: int = 5,
    export_threshold: int = 3,
    failure_threshold: int = 10,
    now: datetime | None = None,
) -> list[dict]:
    """Return rule-based anomalies from the access log."""
    now = now or utcnow()
    since = now - timedelta(minutes=max(1, window_minutes))
    rows = list(
        (
            await session.execute(
                select(AccessLog).where(AccessLog.occurred_at >= since)
            )
        )
        .scalars()
        .all()
    )
    anomalies: list[dict] = []

    deletions = [row for row in rows if row.action in DELETION_ACTIONS]
    for actor, count in Counter(row.actor for row in deletions).items():
        if count >= deletion_threshold:
            anomalies.append(
                {
                    "kind": "deletion_spike",
                    "severity": "high",
                    "actor": actor,
                    "count": count,
                    "window_minutes": window_minutes,
                    "detail": "sensitive deletions/erasures above threshold",
                }
            )

    exports = [row for row in rows if row.action in EXPORT_ACTIONS]
    for actor, count in Counter(row.actor for row in exports).items():
        if count >= export_threshold:
            anomalies.append(
                {
                    "kind": "export_spike",
                    "severity": "medium",
                    "actor": actor,
                    "count": count,
                    "window_minutes": window_minutes,
                    "detail": "export/backup activity above threshold",
                }
            )

    failures = [
        row
        for row in rows
        if any(marker in row.action for marker in FAILURE_MARKERS)
    ]
    for actor, count in Counter(row.actor for row in failures).items():
        if count >= failure_threshold:
            anomalies.append(
                {
                    "kind": "failure_burst",
                    "severity": "medium",
                    "actor": actor,
                    "count": count,
                    "window_minutes": window_minutes,
                    "detail": "repeated failed/rejected actions above threshold",
                }
            )

    anomalies.sort(key=lambda item: (item["severity"], -item["count"]))
    return anomalies
