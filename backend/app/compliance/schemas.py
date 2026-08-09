"""API schemas for the compliance surface."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class CompliancePolicyOut(BaseModel):
    region: str
    residency_mode: str
    local_residency_required: bool
    retention_days: dict[str, int]
    remote_processing: dict[str, bool]
    disclosures: list[str]


class TransparencyOut(BaseModel):
    generated_at: str
    region: str
    residency_mode: str
    local_residency_required: bool
    stored: list[dict]
    trained: list[dict]
    processed: list[dict]
    transmitted: list[dict]


class ErasureRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=512)


class ErasureOut(BaseModel):
    requested_at: datetime
    status: str
    manifest: dict


class RetentionSweepRequest(BaseModel):
    reason: str = Field(default="retention policy", min_length=1, max_length=512)


class RetentionSweepOut(BaseModel):
    ran_at: datetime
    voiceprints_deleted: int
    enrollment_ids: list[str]
    corpus_snapshots_redacted: int = 0
    access_logs_deleted: int = 0
    policy_retention_days: int


class AccessLogEntryOut(BaseModel):
    id: UUID
    occurred_at: datetime
    actor: str
    action: str
    endpoint: str | None
    resource_type: str | None
    resource_ids: list
    request_id: str | None
    details: dict

    model_config = {"from_attributes": True}


class AccessLogPageOut(BaseModel):
    logs: list[AccessLogEntryOut] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class AccessAnomalyOut(BaseModel):
    kind: str
    severity: str
    actor: str
    count: int
    window_minutes: int
    detail: str


class AccessAnomaliesOut(BaseModel):
    detected_at: datetime
    window_minutes: int
    anomalies: list[AccessAnomalyOut] = Field(default_factory=list)
