"""Internal notification records and backend receipts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID


@dataclass(frozen=True)
class NotificationRecord:
    id: UUID
    kind: str
    title: str
    body: str
    priority: float = 0.5
    tier: str = "background"
    source: str | None = None
    fingerprint: str = ""
    queued_at: datetime | None = None
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryReceipt:
    status: Literal["delivered", "failed"]
    backend: str
    backend_ref: str | None = None
    reason: str | None = None
    details: dict = field(default_factory=dict)


class NotifierError(RuntimeError):
    """Backend could not produce a receipt (unavailable or rejected)."""
