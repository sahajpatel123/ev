"""Durable records for compliance actions (erasure manifests)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.db import Base
from app.utils.text import utcnow


class DataErasureRecord(Base):
    """Auditable manifest of a data-subject erasure request.

    The manifest includes object-store keys and biometric enrollment ids so a
    backup/replica purge job can remove residual copies after restore.
    """

    __tablename__ = "data_erasure_records"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    actor: Mapped[str] = mapped_column(String(128), index=True)
    reason: Mapped[str] = mapped_column(Text)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="completed")
