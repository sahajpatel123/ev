from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.config import settings
from app.db import Base
from app.utils.text import utcnow

EMBEDDING_DIM = 384

# Portable embedding column: pgvector in Postgres, JSON list elsewhere (tests).
EmbeddingType = Vector(EMBEDDING_DIM).with_variant(JSON, "sqlite")

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Event(Base):
    """Raw, immutable input. Tombstoned, never updated or deleted."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_type_occurred", "event_type", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow)
    source: Mapped[str] = mapped_column(String(32), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[dict] = mapped_column(JSONType, default=dict)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    device_id: Mapped[str | None] = mapped_column(String(128), index=True)
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    privacy_level: Mapped[str] = mapped_column(String(32), default="normal", index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tombstone_reason: Mapped[str | None] = mapped_column(String(512))


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    entity_type: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(256), index=True)
    aliases: Mapped[list] = mapped_column(JSONType, default=list)
    summary: Mapped[str | None] = mapped_column(Text)
    canonical_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Memory(Base):
    """Derived memory. Versioned, provenance-linked, rebuildable from events."""

    __tablename__ = "memories"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    memory_type: Mapped[str] = mapped_column(String(32), index=True)
    text: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    importance: Mapped[float] = mapped_column(Float, default=0.5, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    source_type: Mapped[str] = mapped_column(String(16), default="inferred", index=True)
    privacy_level: Mapped[str] = mapped_column(String(32), default="normal", index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow)
    created_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version_group: Mapped[UUID] = mapped_column(Uuid, index=True, default=uuid4)
    version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    supersedes_id: Mapped[UUID | None] = mapped_column(ForeignKey("memories.id"), index=True)
    superseded_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("memories.id"))
    reason_for_change: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    redacted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    embedding: Mapped[list | None] = mapped_column(EmbeddingType)
    # --- AGENT 8 SYNAPSE (embeddings) ---
    # Records which embedding model produced this vector. NULL = legacy
    # hash-era vectors (hash was the default before model-version tracking).
    # Mixed versions are detectable and never compared semantically.
    embedding_model_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    # --- END AGENT 8 SYNAPSE ---
    extra: Mapped[dict] = mapped_column(JSONType, default=dict)

    source_events: Mapped[list[Event]] = relationship(secondary="memory_events")
    entities: Mapped[list[Entity]] = relationship(secondary="memory_entities")
    supersedes: Mapped[Memory | None] = relationship(
        remote_side=[id],
        foreign_keys=[supersedes_id],
        back_populates="superseded_by",
    )
    superseded_by: Mapped[Memory | None] = relationship(
        foreign_keys=[superseded_by_id],
        back_populates="supersedes",
    )


class MemoryEvent(Base):
    __tablename__ = "memory_events"

    memory_id: Mapped[UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )


class MemoryEntity(Base):
    __tablename__ = "memory_entities"

    memory_id: Mapped[UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    entity_id: Mapped[UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32), default="related")
    weight: Mapped[float] = mapped_column(Float, default=1.0)


class EntityRelationship(Base):
    __tablename__ = "entity_relationships"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    from_entity_id: Mapped[UUID] = mapped_column(ForeignKey("entities.id"), index=True)
    to_entity_id: Mapped[UUID] = mapped_column(ForeignKey("entities.id"), index=True)
    relationship_type: Mapped[str] = mapped_column(String(64), index=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_type: Mapped[str] = mapped_column(String(16), default="inferred")
    source_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id"))
    created_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conflict(Base):
    __tablename__ = "conflicts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    memory_id_a: Mapped[UUID] = mapped_column(ForeignKey("memories.id"), index=True)
    memory_id_b: Mapped[UUID] = mapped_column(ForeignKey("memories.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    resolution: Mapped[str | None] = mapped_column(Text)
    resolution_memory_id: Mapped[UUID | None] = mapped_column(ForeignKey("memories.id"))
    created_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccessLog(Base):
    __tablename__ = "access_log"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow)
    actor: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    endpoint: Mapped[str | None] = mapped_column(String(256))
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_ids: Mapped[list] = mapped_column(JSONType, default=list)
    request_id: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict] = mapped_column(JSONType, default=dict)


# --------------------------------------------------------------------------- #
# Identity & trust lifecycle — owner binding, recovery, re-verification
# --------------------------------------------------------------------------- #


class OwnerIdentity(Base):
    """The single authoritative 'this is my owner' record.

    One owner row exists today (single-user invariant). Devices, voice
    enrollments, recovery codes, and re-verification proofs all anchor to this
    record so a second identity can be added later without schema churn.
    """

    __tablename__ = "owner_identities"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    display_name: Mapped[str] = mapped_column(String(128))
    recovery_failures: Mapped[int] = mapped_column(Integer, default=0)
    recovery_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RecoveryCode(Base):
    """Single-use, time-limited recovery code. Only the SHA-256 hash is stored."""

    __tablename__ = "identity_recovery_codes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("owner_identities.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(128))


class ReVerificationProof(Base):
    """Short-lived, purpose-bound proof that a sensitive action was re-authorized.

    A fresh proof is required for sensitive actions even when the voice session
    is already unlocked, so an unlocked session can never be silently inherited
    or used for privileged operations by another person/device.
    """

    __tablename__ = "identity_reverifications"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("owner_identities.id"), index=True)
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), index=True)
    voice_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("voice_sessions.id"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasskeyCredential(Base):
    """A registered WebAuthn passkey bound to the owner record.

    Only the credential ID hash is stored (a high-entropy public identifier,
    not a secret). Possession is proven later by a WebAuthn challenge-response;
    this row is the binding anchor so passkeys, voiceprints, and devices all
    resolve to exactly one owner identity.
    """

    __tablename__ = "identity_passkeys"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("owner_identities.id"), index=True)
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), index=True)
    credential_id_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(128))


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128))
    token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    owner_id: Mapped[UUID | None] = mapped_column(ForeignKey("owner_identities.id"), index=True)
    trust_level: Mapped[str] = mapped_column(String(16), default="device", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(256))
    capabilities: Mapped[list] = mapped_column(JSONType, default=list)
    # --- AGENT 14 PULSE (WAVE LIFE, additive) ---
    device_type: Mapped[str | None] = mapped_column(String(32), index=True)
    platform: Mapped[str | None] = mapped_column(String(32))
    paired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    push_token: Mapped[str | None] = mapped_column(Text)
    push_token_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    push_bundle_id: Mapped[str | None] = mapped_column(String(256))
    battery_percent: Mapped[float | None] = mapped_column(Float)
    storage_free_bytes: Mapped[int | None] = mapped_column(Integer)
    bootstrapped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bootstrapped_spoken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Cross-platform Device Gateway (additive). NULL memory_scope means owner.
    role: Mapped[str | None] = mapped_column(String(32), index=True)
    memory_scope: Mapped[str | None] = mapped_column(String(16), index=True)
    client_version: Mapped[str | None] = mapped_column(String(64))
    protocol_version: Mapped[str | None] = mapped_column(String(16))
    # --- G2 EVIE EVERYWHERE (additive): per-device sync cursor over the
    # canonical event history. Routing hint + resume point, never authority.
    sync_cursor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_cursor_id: Mapped[UUID | None] = mapped_column(Uuid)
    # --- G2 SESSION AUTHORITY (additive): server-owned generation counter.
    # Bumped on every trust transition; sessions established under an older
    # generation are invalid and must rebind (promotion AND revocation).
    auth_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    @property
    def push_platform(self) -> str | None:
        return "apns" if self.push_token else None

    @property
    def push_registered(self) -> bool:
        return bool(self.push_token)


class DevicePairingToken(Base):
    """One-time pairing code. Tailnet membership is not Evie device trust."""

    __tablename__ = "device_pairing_tokens"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(32), default="companion")
    display_name: Mapped[str] = mapped_column(String(128), default="Evie phone")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConversationLease(Base):
    """One response-output device at a time. Not Memory OS."""

    __tablename__ = "conversation_leases"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_key: Mapped[str] = mapped_column(String(64), unique=True, default="owner")
    lease_id: Mapped[str] = mapped_column(String(64), index=True)
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"), index=True)
    instance_id: Mapped[str] = mapped_column(String(64), default="")
    method: Mapped[str] = mapped_column(String(32), default="manual")
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_activity: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ActiveConversationState(Base):
    """Short-lived handoff context. Not production Memory OS."""

    __tablename__ = "active_conversation_states"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_key: Mapped[str] = mapped_column(String(64), unique=True, default="owner")
    active_device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"))
    topic: Mapped[str | None] = mapped_column(String(240))
    turns: Mapped[list] = mapped_column(JSONType, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SandboxFact(Base):
    """Isolated cross-platform test memory. Never mixed with Memory OS rows."""

    __tablename__ = "sandbox_facts"
    __table_args__ = (UniqueConstraint("namespace", "fact_key", name="uq_sandbox_facts_ns_key"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    namespace: Mapped[str] = mapped_column(String(64), index=True, default="cross_platform_test")
    fact_key: Mapped[str] = mapped_column(String(160), index=True)
    value: Mapped[str] = mapped_column(Text)
    source_device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ObservationRecord(Base):
    """Structured, provenance-first observation in the personal world model.

    This table deliberately keeps the observation's epistemic status separate
    from its content.  A model guess is never accepted as a stored fact by the
    world-model writer; ``fact_kind`` is limited by that writer to observed,
    reported, or inferred.
    """

    __tablename__ = "observations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    subject: Mapped[str] = mapped_column(String(256), index=True)
    subject_type: Mapped[str] = mapped_column(String(32), index=True, default="owner")
    object_or_event: Mapped[str] = mapped_column("object", String(256), index=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    location: Mapped[str] = mapped_column(String(512), default="unknown")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow)
    source_device: Mapped[str] = mapped_column(String(128), index=True, default="unknown")
    evidence_ref: Mapped[str] = mapped_column(String(512), index=True, default="unknown")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    uncertainty: Mapped[str] = mapped_column(String(512), default="unknown")
    consent_state: Mapped[str] = mapped_column(String(32), index=True, default="unknown")
    retention_class: Mapped[str] = mapped_column(String(64), index=True, default="standard")
    freshness_state: Mapped[str] = mapped_column(String(16), index=True, default="fresh")
    stale_after_seconds: Mapped[int] = mapped_column(Integer, default=86_400)
    fact_kind: Mapped[str] = mapped_column(String(16), index=True, default="observed")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OwnerObject(Base):
    """An explicitly enrolled owner-owned object and its last evidence."""

    __tablename__ = "owner_objects"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner: Mapped[str] = mapped_column(String(256), default="owner", index=True)
    name: Mapped[str] = mapped_column(String(256), index=True)
    object_type: Mapped[str] = mapped_column(String(64), index=True, default="thing")
    enrollment_source: Mapped[str] = mapped_column(String(128), default="user", index=True)
    appearance_references: Mapped[list] = mapped_column(JSONType, default=list)
    common_locations: Mapped[list] = mapped_column(JSONType, default=list)
    last_observed_location: Mapped[str | None] = mapped_column(String(512))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_evidence_ref: Mapped[str | None] = mapped_column(String(512))
    last_confidence: Mapped[float | None] = mapped_column(Float)
    last_uncertainty: Mapped[str | None] = mapped_column(String(512))
    last_freshness_state: Mapped[str] = mapped_column(String(16), default="unknown")
    possible_matches: Mapped[list] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CameraState(Base):
    """Current visible state of one permissioned camera provider."""

    __tablename__ = "camera_states"
    __table_args__ = (UniqueConstraint("device_id", name="uq_camera_states_device"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    platform: Mapped[str] = mapped_column(String(32), default="mac", index=True)
    state: Mapped[str] = mapped_column(String(32), index=True, default="off")
    visible: Mapped[bool] = mapped_column(Boolean, default=False)
    permission_state: Mapped[str] = mapped_column(String(32), default="unknown")
    explicit_request: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_reason: Mapped[str | None] = mapped_column(String(256))
    consent_state: Mapped[str] = mapped_column(String(32), default="not_granted")
    raw_frames_persisted: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(String(512))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- #
# E.V. advanced layer
# --------------------------------------------------------------------------- #


class HealthSnapshot(Base):
    """One point in the personal vitals series (readiness + raw metrics)."""

    __tablename__ = "health_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow)
    source: Mapped[str] = mapped_column(String(32), index=True, default="api")
    device_id: Mapped[str | None] = mapped_column(String(128), index=True)
    metrics: Mapped[dict] = mapped_column(JSONType, default=dict)
    readiness: Mapped[float | None] = mapped_column(Float, index=True)
    band: Mapped[str | None] = mapped_column(String(16))
    anomalies: Mapped[list] = mapped_column(JSONType, default=list)
    permission_state: Mapped[str] = mapped_column(String(32), index=True, default="authorized")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow)
    units: Mapped[dict] = mapped_column(JSONType, default=dict)
    source_metadata: Mapped[dict] = mapped_column(JSONType, default=dict)
    freshness_state: Mapped[str] = mapped_column(String(16), index=True, default="fresh")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WatchlistItem(Base):
    """A topic/project/person/company the user wants EV to watch."""

    __tablename__ = "watchlist_items"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    value: Mapped[str] = mapped_column(String(256), index=True)
    priority: Mapped[float] = mapped_column(Float, default=0.5)
    sources: Mapped[list] = mapped_column(JSONType, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Alert(Base):
    """A scored, deduplicated notification candidate produced by EV radar layers."""

    __tablename__ = "alerts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text)
    priority: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    tier: Mapped[str] = mapped_column(String(16), default="background", index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    source: Mapped[str | None] = mapped_column(String(64))
    trigger_ids: Mapped[list] = mapped_column(JSONType, default=list)
    rationale: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_reason: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict] = mapped_column(JSONType, default=dict)


class TacticalCard(Base):
    """Cached tactical quick card (ev.hud.quickcard.v1) for <800 ms HUD reads."""

    __tablename__ = "tactical_cards"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    topic: Mapped[str] = mapped_column(String(500), index=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    schema_version: Mapped[str] = mapped_column(String(32), default="ev.hud.quickcard.v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Prediction(Base):
    """A stored EV Sense prediction with tracked outcome."""

    __tablename__ = "predictions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    basis_ids: Mapped[list] = mapped_column(JSONType, default=list)
    rationale: Mapped[str | None] = mapped_column(Text)
    intervention_score: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict] = mapped_column(JSONType, default=dict)


class GearSnapshot(Base):
    """Device/gear telemetry: battery, storage, load, uptime."""

    __tablename__ = "gear_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow)
    battery_percent: Mapped[float | None] = mapped_column(Float)
    storage_free_bytes: Mapped[int | None] = mapped_column(Integer)
    memory_used_percent: Mapped[float | None] = mapped_column(Float)
    cpu_percent: Mapped[float | None] = mapped_column(Float)
    uptime_seconds: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DecisionOutcome(Base):
    """Expected-vs-actual follow-up for a decision memory."""

    __tablename__ = "decision_outcomes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    decision_memory_id: Mapped[UUID | None] = mapped_column(ForeignKey("memories.id"), index=True)
    decision_topic: Mapped[str] = mapped_column(String(256), index=True)
    expected_outcome: Mapped[str | None] = mapped_column(Text)
    actual_outcome: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lesson: Mapped[str | None] = mapped_column(Text)
    lesson_memory_id: Mapped[UUID | None] = mapped_column(ForeignKey("memories.id"))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResearchSession(Base):
    """A memory-grounded research session or durable research job.

    ``mode=session`` preserves the original interactive note-taking flow.
    ``mode=job`` uses the same durable row as the long-running execution
    substrate: the row contains the owner, bounded scope, progress,
    checkpoints, citations, artifacts, and provider evidence needed to resume
    after a worker restart without inventing a second job framework.
    """

    __tablename__ = "research_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    question: Mapped[str] = mapped_column(Text, index=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(128), default="master", index=True)
    mode: Mapped[str] = mapped_column(String(16), default="session", index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    conclusion: Mapped[str | None] = mapped_column(Text)
    allowed_tools: Mapped[list] = mapped_column(JSONType, default=list)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    budget: Mapped[dict] = mapped_column(JSONType, default=dict)
    checkpoints: Mapped[list] = mapped_column(JSONType, default=list)
    progress: Mapped[dict] = mapped_column(JSONType, default=dict)
    final_artifacts: Mapped[list] = mapped_column(JSONType, default=list)
    citations: Mapped[list] = mapped_column(JSONType, default=list)
    evidence: Mapped[dict] = mapped_column(JSONType, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ResearchNote(Base):
    __tablename__ = "research_notes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("research_sessions.id"), index=True)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("events.id"), index=True)
    note: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(1024))
    source_title: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MakerProject(Base):
    """Maker companion: physical projects with a state machine, BOM, and print queue."""

    __tablename__ = "maker_projects"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(256), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="idea", index=True)
    current_step: Mapped[str | None] = mapped_column(String(256))
    goal_memory_id: Mapped[UUID | None] = mapped_column(ForeignKey("memories.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class BomItem(Base):
    __tablename__ = "bom_items"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("maker_projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    qty: Mapped[float] = mapped_column(Float, default=1.0)
    unit: Mapped[str | None] = mapped_column(String(32))
    location: Mapped[str | None] = mapped_column(String(256))
    reorder_at: Mapped[float | None] = mapped_column(Float)  # quantity threshold
    cost: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PrintJob(Base):
    __tablename__ = "print_jobs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("maker_projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    estimated_minutes: Mapped[int | None] = mapped_column(Integer)
    filament_grams: Mapped[float | None] = mapped_column(Float)
    vendor_job_id: Mapped[str | None] = mapped_column(String(128), index=True)
    adapter: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    error_log: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PersonalityProfile(Base):
    """Versioned communication characteristics; core identity invariants never change."""

    __tablename__ = "personality_profiles"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    directness: Mapped[int] = mapped_column(Integer, default=3)
    humor: Mapped[int] = mapped_column(Integer, default=2)
    formality: Mapped[int] = mapped_column(Integer, default=2)
    technicality: Mapped[int] = mapped_column(Integer, default=4)
    assertiveness: Mapped[int] = mapped_column(Integer, default=3)
    verbosity: Mapped[int] = mapped_column(Integer, default=3)
    proactivity: Mapped[int] = mapped_column(Integer, default=3)
    challenge_level: Mapped[int] = mapped_column(Integer, default=3)
    emotional_style: Mapped[str] = mapped_column(String(16), default="calm")
    reason_for_change: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResponseLog(Base):
    """Self-evaluation record for important interactions."""

    __tablename__ = "response_log"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    request_text: Mapped[str] = mapped_column(Text)
    reply_text: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[dict] = mapped_column(JSONType, default=dict)
    provenance_ids: Mapped[list] = mapped_column(JSONType, default=list)
    context_tokens: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String(128))
    was_useful: Mapped[bool | None] = mapped_column(Boolean)
    followed_recommendation: Mapped[bool | None] = mapped_column(Boolean)
    was_correction: Mapped[bool | None] = mapped_column(Boolean)
    intervention_appropriate: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class FilterLedger(Base):
    """Audit trail for intelligence-filter decisions (input and output)."""

    __tablename__ = "filter_ledger"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    stage: Mapped[str] = mapped_column(String(16), index=True)  # input | output | pipeline
    action: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    severity: Mapped[str] = mapped_column(String(16), default="info")
    detail: Mapped[dict] = mapped_column(JSONType, default=dict)
    draft: Mapped[str | None] = mapped_column(Text)
    final_text: Mapped[str | None] = mapped_column(Text)
    scores: Mapped[dict | None] = mapped_column(JSONType)
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    costs: Mapped[dict | None] = mapped_column(JSONType)
    envelope_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ModelCallLog(Base):
    """Append-only audit record for every model call through the gateway.

    Stores the request envelope (strategy, memory refs, request id, metadata),
    provider/model, latency, usage, tool-validation outcome, and any error so
    every model interaction is auditable.
    """

    __tablename__ = "model_calls"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    request_id: Mapped[str] = mapped_column(String(128), index=True)
    actor: Mapped[str] = mapped_column(String(128), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    model: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[list] = mapped_column(JSONType, default=list)
    envelope: Mapped[dict] = mapped_column(JSONType, default=dict)
    envelope_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# --------------------------------------------------------------------------- #
# Single continuous conversation + live data recording
# --------------------------------------------------------------------------- #


class ConversationThread(Base):
    """One lifelong conversation window. The user talks to EV, not to chat #N."""

    __tablename__ = "conversation_threads"
    __table_args__ = (
        Index(
            "uq_conversation_threads_one_default",
            "is_default",
            unique=True,
            sqlite_where=text("is_default = 1"),
            postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(256), default="EV — continuous conversation")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ConversationState(Base):
    """Ephemeral per-thread state: current focus, topics, pending questions, context."""

    __tablename__ = "conversation_states"

    thread_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversation_threads.id"), primary_key=True
    )
    focus: Mapped[str | None] = mapped_column(String(512))
    recent_topics: Mapped[list] = mapped_column(JSONType, default=list)
    pending_questions: Mapped[list] = mapped_column(JSONType, default=list)
    working_context: Mapped[dict] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConversationRollup(Base):
    """Durable rolling summary of a conversation thread.

    Derived state: rebuildable from immutable events, so it can be overwritten
    safely. Keeps the long arc of the conversation in a compact, token-bounded
    form so a single lifelong thread never requires loading full history.
    """

    __tablename__ = "conversation_rollups"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    thread_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversation_threads.id"), unique=True, index=True
    )
    summary: Mapped[str] = mapped_column(Text, default="")
    topics: Mapped[list] = mapped_column(JSONType, default=list)
    open_questions: Mapped[list] = mapped_column(JSONType, default=list)
    decisions: Mapped[list] = mapped_column(JSONType, default=list)
    arc: Mapped[list] = mapped_column(JSONType, default=list)
    last_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id"), index=True)
    covered_turn_count: Mapped[int] = mapped_column(Integer, default=0)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class LiveChannel(Base):
    """A user-permissioned source of continuous live data (screen, audio, health, app)."""

    __tablename__ = "live_channels"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # screen|audio|health|app|vision|location
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    privacy_level: Mapped[str] = mapped_column(String(32), default="normal")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class LiveEvent(Base):
    """One unit of live data. Immutable; derived state rebuilds from these."""

    __tablename__ = "live_events"
    __table_args__ = (
        UniqueConstraint("channel_id", "sha256", name="uq_live_events_channel_sha256"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("live_channels.id"), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    device_id: Mapped[str | None] = mapped_column(String(128), index=True)
    collector: Mapped[str | None] = mapped_column(String(128), index=True)
    privacy_level: Mapped[str] = mapped_column(String(32), default="normal", index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class LiveDerivedState(Base):
    """Deterministic per-channel derived state, rebuilt from live events.

    Dropped and replayed from the immutable ``live_events`` stream by
    ``POST /v1/live/rebuild``; never edited in place.  Contains only derived
    summaries and signal flags (with live-event provenance), never raw payloads.
    """

    __tablename__ = "live_derived_state"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    channel_id: Mapped[UUID] = mapped_column(
        ForeignKey("live_channels.id"), unique=True, index=True
    )
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    consumed_count: Mapped[int] = mapped_column(Integer, default=0)
    first_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("live_events.id"), index=True
    )
    signals: Mapped[list] = mapped_column(JSONType, default=list)
    rebuilt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FocusDesignation(Base):
    """E.D.I.T.H.-style targeting adapted to attention: what EV is locked onto."""

    __tablename__ = "focus_designations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    label: Mapped[str] = mapped_column(String(256), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # task|project|person|topic|goal
    target_id: Mapped[str | None] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FleetTask(Base):
    """A permissioned task dispatched to a registered device (E.D.I.T.H. fleet, adapted)."""

    __tablename__ = "fleet_tasks"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"), index=True)
    task_type: Mapped[str] = mapped_column(String(64), index=True)
    # requested -> accepted -> running -> completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="requested", index=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONType)
    requested_by: Mapped[str] = mapped_column(String(128), default="master")
    accepted_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RecognitionLog(Base):
    """User-tagged recognition over user-owned media/live frames (never stranger scanning)."""

    __tablename__ = "recognition_log"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id"), index=True)
    live_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("live_events.id"), index=True)
    attachment_id: Mapped[UUID | None] = mapped_column(ForeignKey("attachments.id"), index=True)
    entity_id: Mapped[UUID | None] = mapped_column(ForeignKey("entities.id"), index=True)
    label: Mapped[str] = mapped_column(String(256), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source: Mapped[str] = mapped_column(String(32), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------- #
# AGENT 7 ROSTER — consented face enrollment, recognition, public-figure cache
# --------------------------------------------------------------------------- #

FACE_EMBEDDING_DIM = 512


class FaceEnrollment(Base):
    """One consented person's face template set (mean template + per-sample QC).

    Mirrors ``VoiceEnrollment``: the mean embedding is Fernet-encrypted at
    rest (``ciphertext`` + ``salt``), versioned, revocable, and deletable on
    demand. A face that matches no enrolled template resolves to ``unknown``;
    there is no code path that attempts to identify a non-enrolled person.
    """

    __tablename__ = "face_enrollments"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    entity_id: Mapped[UUID] = mapped_column(ForeignKey("entities.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    algorithm: Mapped[str] = mapped_column(String(32), default="sface-onnx", index=True)
    embedding_dim: Mapped[int] = mapped_column(Integer, default=FACE_EMBEDDING_DIM)
    threshold: Mapped[float] = mapped_column(Float, default=0.55)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active|revoked|deleted
    consent_id: Mapped[UUID | None] = mapped_column(ForeignKey("consent_records.id"))
    ciphertext: Mapped[str | None] = mapped_column(Text)  # Fernet token
    salt: Mapped[str | None] = mapped_column(String(64))
    privacy_level: Mapped[str] = mapped_column(String(32), default="sensitive")
    redacted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    supersedes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("face_enrollments.id"), index=True
    )
    superseded_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("face_enrollments.id"))
    reason_for_change: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class FaceSample(Base):
    """One encrypted per-sample template kept for enrollment quality control."""

    __tablename__ = "face_samples"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    enrollment_id: Mapped[UUID] = mapped_column(
        ForeignKey("face_enrollments.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[UUID] = mapped_column(ForeignKey("entities.id"), index=True)
    sample_index: Mapped[int] = mapped_column(Integer, default=0)
    ciphertext: Mapped[str] = mapped_column(Text)  # Fernet token
    salt: Mapped[str | None] = mapped_column(String(64))
    quality: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(32), default="photo")
    attachment_id: Mapped[UUID | None] = mapped_column(ForeignKey("attachments.id"), index=True)
    live_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("live_events.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PublicFigureCache(Base):
    """Licensed, attributed public-figure biodata (Wikidata CC0 + Wikipedia CC BY-SA)."""

    __tablename__ = "public_figure_cache"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(256), index=True)
    canonical_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    entity_id: Mapped[UUID | None] = mapped_column(ForeignKey("entities.id"), index=True)
    data: Mapped[dict] = mapped_column(JSONType, default=dict)
    source_url: Mapped[str | None] = mapped_column(String(1024))
    license: Mapped[str] = mapped_column(String(128), default="CC0 / CC BY-SA 4.0")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CommandLedger(Base):
    """Append-only E.D.I.T.H. command ledger.

    Every command issued through the E.D.I.T.H. surface (focus designation,
    fleet dispatch, recognition annotation, task transitions) is recorded here
    with its actor, target, payload, status, and result so the command surface
    is explicit, authorized, and auditable.
    """

    __tablename__ = "command_ledger"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    command_type: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(128), index=True)
    actor: Mapped[str] = mapped_column(String(128), index=True)
    # issued | completed | failed | rejected
    status: Mapped[str] = mapped_column(String(16), default="issued", index=True)
    request: Mapped[dict] = mapped_column(JSONType, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- #
# Training & personalization — consent-gated voice enrollment and voiceprints
# --------------------------------------------------------------------------- #

VOICEPRINT_DIM = 192

# Portable voiceprint vector: pgvector in Postgres, JSON list elsewhere (tests).
VoiceprintEmbeddingType = Vector(VOICEPRINT_DIM).with_variant(JSON, "sqlite")


class ConsentRecord(Base):
    """Explicit, revocable consent for one training/personalization track."""

    __tablename__ = "consent_records"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    track: Mapped[str] = mapped_column(String(64), index=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)
    consent_version: Mapped[str] = mapped_column(String(32), default="1.0")
    purpose: Mapped[str] = mapped_column(Text)
    scope: Mapped[dict] = mapped_column(JSONType, default=dict)
    source: Mapped[str] = mapped_column(String(64), default="privacy_center")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VoiceEnrollment(Base):
    """One logical owner-voice enrollment.

    Combines the consent-gated training track (status/encoder/privacy) with the
    encrypted, versioned voiceprint track (ciphertext/salt/version chain).
    """

    __tablename__ = "voice_enrollments"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("owner_identities.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    algorithm: Mapped[str] = mapped_column(String(32), default="profile-v1", index=True)
    embedding_dim: Mapped[int] = mapped_column(Integer, default=0)
    threshold: Mapped[float] = mapped_column(Float, default=0.82)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active|revoked|deleted
    encoder: Mapped[str] = mapped_column(String(32), default="hash")
    encoder_model: Mapped[str | None] = mapped_column(String(128))
    privacy_level: Mapped[str] = mapped_column(String(32), default="sensitive")
    redacted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    consent_id: Mapped[UUID | None] = mapped_column(ForeignKey("consent_records.id"))
    ciphertext: Mapped[str | None] = mapped_column(Text)  # Fernet token
    salt: Mapped[str | None] = mapped_column(String(64))
    supersedes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("voice_enrollments.id"), index=True
    )
    superseded_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("voice_enrollments.id"))
    reason_for_change: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class VoicePrint(Base):
    """Versioned protected voiceprint template. Raw audio samples are never retained.

    The biometric embedding is authenticated-encrypted (Fernet) before it ever
    touches the database. ``embedding`` is a read-only convenience property that
    decrypts on access; it is never a stored plaintext column.
    """

    __tablename__ = "voice_prints"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    enrollment_id: Mapped[UUID] = mapped_column(ForeignKey("voice_enrollments.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    embedding_ciphertext: Mapped[str | None] = mapped_column(Text)  # Fernet token
    embedding_salt: Mapped[str | None] = mapped_column(String(64))
    threshold: Mapped[float] = mapped_column(Float, default=0.72)
    sample_hashes: Mapped[list] = mapped_column(JSONType, default=list)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    supersedes_id: Mapped[UUID | None] = mapped_column(ForeignKey("voice_prints.id"))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)
    redacted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def embedding(self) -> list[float] | None:
        """Decrypted template embedding, or None when redacted/unavailable."""
        if not self.embedding_ciphertext or not self.embedding_salt:
            return None
        try:
            # Imported lazily to avoid a package-init cycle (voice/__init__
            # imports lifecycle, which imports models).
            from app.voice.security import decrypt_payload

            payload = decrypt_payload(
                self.embedding_ciphertext,
                self.embedding_salt,
                master_key=settings.master_key,
            )
        except ValueError:
            return None
        value = payload.get("embedding")
        return list(value) if isinstance(value, list) else None


# --------------------------------------------------------------------------- #
# Voice & speech (EVIE)
# --------------------------------------------------------------------------- #


class VoiceSession(Base):
    """One wake → verify → listen → act → reply → follow-up → idle session."""

    __tablename__ = "voice_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("owner_identities.id"), index=True
    )
    device_id: Mapped[str] = mapped_column(String(128), index=True)
    wake_word: Mapped[str] = mapped_column(String(32), default="evie")
    state: Mapped[str] = mapped_column(String(24), default="idle", index=True)
    owner_verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    speaker_confidence: Mapped[float | None] = mapped_column(Float)
    verifier_name: Mapped[str | None] = mapped_column(String(64))
    wake_confidence: Mapped[float | None] = mapped_column(Float)
    challenge_nonce: Mapped[str | None] = mapped_column(String(128), index=True)
    challenge_phrase: Mapped[str | None] = mapped_column(String(256))
    wake_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id"), index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_utterance_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    follow_up_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(128))
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversation_threads.id"), index=True
    )
    greeted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    malfunction_spoken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ReplayNonce(Base):
    """Single-use challenge nonces that make replay attacks fail."""

    __tablename__ = "voice_replay_nonces"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    nonce: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    session_id: Mapped[UUID | None] = mapped_column(ForeignKey("voice_sessions.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(32), index=True)  # wake | verify
    challenge_phrase: Mapped[str | None] = mapped_column(String(256))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VoiceAttemptLog(Base):
    """Privacy-safe audit of voice attempts. Never stores audio or transcripts."""

    __tablename__ = "voice_attempt_log"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow)
    device_id: Mapped[str | None] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # wake|verify|utterance|follow_up|refusal|replay|end
    outcome: Mapped[str] = mapped_column(String(32), index=True)  # accepted|refused|rejected|timeout|error
    session_id: Mapped[UUID | None] = mapped_column(ForeignKey("voice_sessions.id"), index=True)
    reason: Mapped[str | None] = mapped_column(String(256))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)


# --------------------------------------------------------------------------- #
# 24/7 runtime & devices
# --------------------------------------------------------------------------- #


class RuntimeSession(Base):
    """One wake cycle of the centralized 24/7 runtime state machine.

    Lifecycle: idle -> verifying -> awake -> processing -> responding ->
    follow_up -> idle. At most one session is active (ended_at is NULL); a new
    wake supersedes any stale active session.
    """

    __tablename__ = "runtime_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    state: Mapped[str] = mapped_column(String(16), default="idle", index=True)
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), index=True)
    wake_signal: Mapped[float | None] = mapped_column(Float)
    priority: Mapped[float] = mapped_column(Float, default=0.5)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(128))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Owner-only activation gates. Set only after speaker verification passes.
    owner_verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    speaker_confidence: Mapped[float | None] = mapped_column(Float)
    verifier_name: Mapped[str | None] = mapped_column(String(64))
    wake_word: Mapped[str] = mapped_column(String(32), default="evie")
    challenge_nonce: Mapped[str | None] = mapped_column(String(128), index=True)
    challenge_phrase: Mapped[str | None] = mapped_column(String(256))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuntimeHeartbeat(Base):
    """One device pulse: listener state, battery, latency, status."""

    __tablename__ = "runtime_heartbeats"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"), index=True)
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, default=utcnow
    )
    status: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    listener_state: Mapped[str] = mapped_column(String(16), default="listening")
    battery_percent: Mapped[float | None] = mapped_column(Float)
    storage_free_bytes: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)


class ApprovedAction(Base):
    """A command routed to an approved action with a per-action permission check."""

    __tablename__ = "approved_actions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    action_type: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str | None] = mapped_column(String(256))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    requested_by: Mapped[str | None] = mapped_column(String(128))
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), index=True)
    session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runtime_sessions.id"), index=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(128))
    denied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    denied_reason: Mapped[str | None] = mapped_column(String(256))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_reason: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class MemoryCurationJob(Base):
    """Postgres outbox for DeepSeek memory curation. Not Redis/RQ ingestion."""

    __tablename__ = "memory_curation_jobs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    job_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="curate", index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    curator_version: Mapped[str] = mapped_column(String(32), default="1")
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    source_event_ids: Mapped[list] = mapped_column(JSONType, default=list)
    result: Mapped[dict] = mapped_column(JSONType, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class DeadLetter(Base):
    """A failed background job that must be observable and recoverable."""

    __tablename__ = "dead_letters"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    queue: Mapped[str] = mapped_column(String(64), index=True)
    job_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1, index=True)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    last_error_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuntimeEvent(Base):
    """Append-only runtime observability log: every pulse of the runtime.

    Records wake arbitrations, state transitions, heartbeats, action
    decisions, dead-letter changes, and daemon ticks so failures and lifecycles
    are auditable and devices can converge on the same runtime state.
    """

    __tablename__ = "runtime_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, default=utcnow
    )
    kind: Mapped[str] = mapped_column(String(32), index=True)
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), index=True)
    session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("runtime_sessions.id"), index=True
    )
    action_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("approved_actions.id"), index=True
    )
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)


# --------------------------------------------------------------------------- #
# Routines & automations
# --------------------------------------------------------------------------- #


class Routine(Base):
    """A controlled proactive execution rule: scheduled or trigger-based.

    Every routine owns an explicit action (routed through the existing
    ApprovedAction permission layer), an audit trail (RoutineRun rows), and a
    one-tap disable switch.  A routine can only make an action *more* strict
    (requires_approval=True); it can never lower the runtime permission matrix.
    """

    __tablename__ = "routines"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="scheduled", index=True)
    # scheduled | trigger
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    schedule: Mapped[str | None] = mapped_column(String(128))  # 5-field cron
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    quiet_hours_skip: Mapped[bool] = mapped_column(Boolean, default=True)
    backfill_max: Mapped[int] = mapped_column(Integer, default=1)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=0)
    trigger: Mapped[dict] = mapped_column(JSONType, default=dict)
    action_type: Mapped[str] = mapped_column(String(64))
    action_title: Mapped[str | None] = mapped_column(String(256))
    action_payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    undoable: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_status: Mapped[str | None] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RoutineRun(Base):
    """One execution attempt of a routine: the run history + failure state.

    The unique dedupe_key is the duplicate-prevention invariant: a scheduled
    occurrence or a (routine, trigger event) pair can only ever produce one
    run row, even under concurrent scheduler ticks or event replays.
    """

    __tablename__ = "routine_runs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    routine_id: Mapped[UUID] = mapped_column(ForeignKey("routines.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    # queued | awaiting_approval | approved | executed | failed | skipped |
    # cancelled | denied | rolled_back
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    trigger_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id"), index=True)
    trigger_live_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("live_events.id"), index=True
    )
    trigger_snapshot: Mapped[dict] = mapped_column(JSONType, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    action_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("approved_actions.id"), index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSONType)
    undoable: Mapped[bool] = mapped_column(Boolean, default=False)
    undo_status: Mapped[str] = mapped_column(String(16), default="none", index=True)
    undo_payload: Mapped[dict | None] = mapped_column(JSONType)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------- #
# Integrations & ecosystem — adapters, credentials, webhooks, plugins
# --------------------------------------------------------------------------- #


class Integration(Base):
    """A permissioned external-system connection.

    One adapter instance with its own privacy scope, live channel, credential
    record, and audit trail. Revocation is immediate: status flips to revoked,
    credentials are wiped, and the bound live channel is deactivated.
    """

    __tablename__ = "integrations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    adapter: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    scopes: Mapped[list] = mapped_column(JSONType, default=list)
    # active | revoked
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    privacy_level: Mapped[str] = mapped_column(String(32), default="normal")
    config: Mapped[dict] = mapped_column(JSONType, default=dict)
    live_channel_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("live_channels.id"), index=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_webhook_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class IntegrationCredential(Base):
    """Encrypted credential for one integration (OAuth token or webhook secret).

    Values are Fernet-encrypted at rest; only a fingerprint is retained for
    verification. Revocation clears the ciphertext so the credential is
    unusable even if the database is later exfiltrated.
    """

    __tablename__ = "integration_credentials"
    __table_args__ = (
        UniqueConstraint("integration_id", "kind", name="uq_integration_credentials_kind"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    integration_id: Mapped[UUID] = mapped_column(ForeignKey("integrations.id"), index=True)
    # oauth | webhook_secret
    kind: Mapped[str] = mapped_column(String(24), index=True)
    provider_account_id: Mapped[str | None] = mapped_column(String(256))
    scopes: Mapped[list] = mapped_column(JSONType, default=list)
    encrypted_access: Mapped[str | None] = mapped_column(Text)
    encrypted_refresh: Mapped[str | None] = mapped_column(Text)
    token_type: Mapped[str | None] = mapped_column(String(32))
    token_fingerprint: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Plugin(Base):
    """A user-submitted custom skill/command pack.

    Manifests are validated, checksummed, and must be explicitly approved by
    the master key before any command can run. Approved permissions are the
    exact set declared in the manifest (least privilege); disabling is instant.
    """

    __tablename__ = "plugins"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(32))
    # pending | approved | rejected | disabled
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    manifest: Mapped[dict] = mapped_column(JSONType, default=dict)
    permissions: Mapped[list] = mapped_column(JSONType, default=list)
    checksum: Mapped[str] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(128))
    rejected_reason: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PersonalizationCalibration(Base):
    """Versioned, evidence-backed retrieval calibration (life-data personalization).

    One snapshot maps memory types to importance multipliers derived from logged
    corrections/usefulness/follow signals. Only the current row is applied by
    retrieval; older rows are kept for audit and one-step rollback.
    """

    __tablename__ = "personalization_calibrations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    calibrations: Mapped[dict] = mapped_column(JSONType, default=dict)
    evidence: Mapped[dict] = mapped_column(JSONType, default=dict)
    reason_for_change: Mapped[str] = mapped_column(Text, default="evidence-backed calibration")
    consent_id: Mapped[UUID | None] = mapped_column(ForeignKey("consent_records.id"))
    supersedes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("personalization_calibrations.id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class TrainingCorpusSnapshot(Base):
    """Versioned training corpus snapshot (consent-gated, rebuildable, erasable).

    Entries are harvested from rated response logs, filter-ledger final texts,
    and normal/sensitive-excluded events. `never_send_to_model` and sensitive
    content is never included; credentials are redacted before storage. A
    snapshot is reproducible from the same sources (deterministic content hash).
    """

    __tablename__ = "training_corpus_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    name: Mapped[str] = mapped_column(String(128), default="evie-training-corpus")
    entries: Mapped[list] = mapped_column(JSONType, default=list)
    source_counts: Mapped[dict] = mapped_column(JSONType, default=dict)
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    reason_for_change: Mapped[str] = mapped_column(Text, default="corpus harvest")
    consent_id: Mapped[UUID | None] = mapped_column(ForeignKey("consent_records.id"))
    supersedes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("training_corpus_snapshots.id")
    )
    redacted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class FilterRecalibration(Base):
    """Versioned filter self-improvement report driven by the filter ledger.

    Ledger aggregates (block rate, over-refinement, redactions, repairs) plus
    user correction/usefulness signals produce deterministic threshold
    proposals. Snapshots are consent-gated, rollback-able, and erasable; an
    explicitly applied snapshot carries the concrete runtime ``policy`` that
    the live filter consumes.
    """

    __tablename__ = "filter_recalibrations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    metrics: Mapped[dict] = mapped_column(JSONType, default=dict)
    proposals: Mapped[list] = mapped_column(JSONType, default=list)
    policy: Mapped[dict] = mapped_column(JSONType, default=dict)
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    applied_by: Mapped[str | None] = mapped_column(String(128))
    reason_for_change: Mapped[str] = mapped_column(Text, default="ledger-driven recalibration")
    consent_id: Mapped[UUID | None] = mapped_column(ForeignKey("consent_records.id"))
    supersedes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("filter_recalibrations.id")
    )
    redacted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AdapterRegistration(Base):
    """Versioned EVIE adapter (LoRA-style) registry with eval gates.

    A registration binds a corpus snapshot, runs deterministic eval gates, and
    can be activated or rolled back. Actual weight training is provider-
    dependent (local LoRA or hosted fine-tune); this registry is the
    versioning/rollback/eval boundary around it.
    """

    __tablename__ = "adapter_registrations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="approved", index=True)
    provider: Mapped[str] = mapped_column(String(64), default="local-lora")
    base_model: Mapped[str | None] = mapped_column(String(128))
    adapter_ref: Mapped[str | None] = mapped_column(String(512))
    corpus_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("training_corpus_snapshots.id")
    )
    eval_metrics: Mapped[dict] = mapped_column(JSONType, default=dict)
    reason_for_change: Mapped[str | None] = mapped_column(Text)
    consent_id: Mapped[UUID | None] = mapped_column(ForeignKey("consent_records.id"))
    supersedes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("adapter_registrations.id")
    )
    redacted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class WebhookDelivery(Base):
    """Idempotent webhook delivery ledger (provider retries are exact dedupes).

    A provider may retry the same logical delivery with a fresh timestamp and
    signature. ``X-EV-Delivery-Id`` lets EV return the original result instead
    of ingesting a duplicate. The unique (integration, delivery_key) pair makes
    retries idempotent at the database level.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "integration_id",
            "delivery_key",
            name="uq_webhook_deliveries_integration_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    integration_id: Mapped[UUID] = mapped_column(ForeignKey("integrations.id"), index=True)
    delivery_key: Mapped[str] = mapped_column(String(128))
    event_ids: Mapped[list] = mapped_column(JSONType, default=list)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


# ============================================================================
# SHARED APPEND-ONLY SECTION — docs/FLEET_LAW.md §3
# Additive only. Append inside YOUR block; never modify, reorder, reformat, or
# delete another agent's lines, table columns, or models.
#
# --- AGENT 1 CONDUCTOR ---
# Reserved by Agent 1 (Conductor): fleet governance, integration, contract.
#
# --- AGENT 2 FOUNDRY ---
# --- AGENT 3 EARS ---
# --- AGENT 4 VOICE ---
# --- AGENT 5 SENTRY ---
# --- AGENT 6 EYES ---
# --- AGENT 7 ROSTER ---
# --- AGENT 8 SYNAPSE ---
# --- AGENT 9 MNEMO ---
# --- AGENT 10 CORTEX ---
# --- AGENT 11 FORGE ---
# --- AGENT 12 CONDUIT ---
# --- AGENT 13 AMBIENT ---
# --- AGENT 14 PULSE ----------------------------------------------------------


class Notification(Base):
    """Delivery ledger: every attempted EV notification with its receipt.

    Status is one of ``attempted``, ``delivered``, ``failed``, or
    ``suppressed``. ``delivered`` is only ever set from backend evidence; a
    caller-supplied result is never treated as delivery proof (FLEET_LAW §8).
    ``suppressed`` always carries a human-answerable ``reason`` (quiet_hours,
    daily_cap, duplicate, max_attempts).
    """

    __tablename__ = "notifications"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text)
    priority: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    tier: Mapped[str] = mapped_column(String(16), default="background", index=True)
    source: Mapped[str | None] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="attempted", index=True)
    reason: Mapped[str | None] = mapped_column(String(128))
    backend: Mapped[str | None] = mapped_column(String(32))
    backend_ref: Mapped[str | None] = mapped_column(String(256))
    alert_id: Mapped[UUID | None] = mapped_column(ForeignKey("alerts.id"), index=True)
    action_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("approved_actions.id"), index=True
    )
    # --- AGENT 14 PULSE (WAVE LIFE, additive) ---
    device_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("devices.id"), index=True
    )
    attention_kind: Mapped[str] = mapped_column(
        String(24), default="incoming", index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict] = mapped_column(JSONType, default=dict)


# --- AGENT 15 ORACLE ---
# --- AGENT 16 CONSCIENCE ---
# --- AGENT 17 WORKBENCH ---
# --- AGENT 18 SUIT ---
# --- AGENT 19 VAULT --------------------------------------------------------


class PasskeyChallenge(Base):
    """Server-issued, single-use challenge for a WebAuthn ceremony.

    Only the SHA-256 digest is stored at rest; the raw challenge is returned
    once in the options response and must be echoed inside ``clientDataJSON``.
    A challenge is purpose-bound (register vs authenticate), expires, and is
    consumed on first use so a captured response cannot be replayed.
    """

    __tablename__ = "identity_passkey_challenges"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    purpose: Mapped[str] = mapped_column(String(16), index=True)  # register | authenticate
    owner_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("owner_identities.id"), index=True
    )
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), index=True)
    challenge_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    rp_id: Mapped[str] = mapped_column(String(256))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasskeyAuthMaterial(Base):
    """Cryptographic WebAuthn material for one registered passkey.

    Kept as a separate additive table so the existing ``identity_passkeys``
    binding rows and their API/CLI surface are untouched. The stored COSE
    public key, sign counter, and attestation result are what let a passkey
    actually prove possession during a challenge-response ceremony.
    """

    __tablename__ = "identity_passkey_materials"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    passkey_id: Mapped[UUID] = mapped_column(
        ForeignKey("identity_passkeys.id"), index=True, unique=True
    )
    public_key_cose: Mapped[dict] = mapped_column(JSONType, default=dict)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    aaguid: Mapped[str | None] = mapped_column(String(32))
    transports: Mapped[list] = mapped_column(JSONType, default=list)
    attestation_format: Mapped[str | None] = mapped_column(String(32))
    attestation_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    attestation_level: Mapped[str | None] = mapped_column(String(32))
    rp_id: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- #
# AGENT 12 CONDUIT (WAVE LIFE) — Apple life bridges (additive)
# --------------------------------------------------------------------------- #


class LifeOutboundAction(Base):
    """One queued outbound life action for a registered device actuator.

    The device_proxy adapter queues messages/calls here; a registered iPhone
    (SUIT app) polls the outbox and posts authenticated delivery results.
    ``delivered`` is only set when the device returns provider evidence
    (message_id/call_id + timestamp); nothing is ever marked delivered
    without evidence.
    """

    __tablename__ = "life_outbound_actions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    integration_id: Mapped[UUID] = mapped_column(
        ForeignKey("integrations.id"), index=True
    )
    device_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("devices.id"), index=True
    )
    action: Mapped[str] = mapped_column(String(64), index=True)
    args: Mapped[dict] = mapped_column(JSONType, default=dict)
    # queued | delivered | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    result: Mapped[dict | None] = mapped_column(JSONType)
    evidence: Mapped[dict | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # --- AGENT 14 PULSE (WAVE LIFE, additive) ---
    # Lifecycle mirrors the brief's queued → dispatched → acknowledged →
    # executed / failed contract while Agent 12's ``status`` column keeps the
    # device-proxy outbox semantics (queued → delivered/failed).
    lifecycle: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
# --- AGENT 20 LAUNCH ---
# ============================================================================


# --------------------------------------------------------------------------- #
# Day-long companion: owner profile, calibration cache, callouts, gates
# --------------------------------------------------------------------------- #


class AssistantProfile(Base):
    """Singleton owner-scoped spoken identity and companion stamps."""

    __tablename__ = "assistant_profiles"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("owner_identities.id"), index=True
    )
    nickname: Mapped[str] = mapped_column(String(64), default="EVIE")
    owner_preferred_name: Mapped[str | None] = mapped_column(String(128))
    greeting_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tts_voice_id: Mapped[str | None] = mapped_column(String(64))
    tts_rate: Mapped[float | None] = mapped_column(Float)
    last_sense_why: Mapped[str | None] = mapped_column(Text)
    last_sense_source_ids: Mapped[list] = mapped_column(JSONType, default=list)
    last_sense_callout_id: Mapped[UUID | None] = mapped_column(Uuid)
    morning_brief_spoken_on: Mapped[str | None] = mapped_column(String(16))
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedication_text: Mapped[str | None] = mapped_column(String(500))
    dedication_blob_id: Mapped[str | None] = mapped_column(String(256))
    dedication_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    live_conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversation_threads.id"), index=True
    )
    training_wheels_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    training_wheels_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    social_turn_count: Mapped[int] = mapped_column(Integer, default=0)
    social_nudge_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    isolation_scan_ran_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    isolation_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    quiet_hours_start: Mapped[str | None] = mapped_column(String(16))
    quiet_hours_end: Mapped[str | None] = mapped_column(String(16))
    quiet_digest_spoken_on: Mapped[str | None] = mapped_column(String(16))
    tts_voice: Mapped[str | None] = mapped_column(String(64), default="default")
    hud_layout: Mapped[dict] = mapped_column(JSONType, default=dict)
    training_steps: Mapped[dict] = mapped_column(JSONType, default=dict)
    volume_percent: Mapped[int] = mapped_column(Integer, default=70)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CalibrationReportRow(Base):
    """Cached last self-calibration report (item 26 hook)."""

    __tablename__ = "calibration_reports"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    overall: Mapped[str] = mapped_column(String(16), index=True)
    report: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Callout(Base):
    """Persisted status narration; spoken only when policy allows."""

    __tablename__ = "callouts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_item: Mapped[str | None] = mapped_column(String(128), index=True)
    hud: Mapped[dict] = mapped_column(JSONType, default=dict)
    spoken: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    emergency: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class FeatureGate(Base):
    """Minimal feature-gate / training-wheels flags for the protocol sheet."""

    __tablename__ = "feature_gates"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="locked", index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    setup_hint: Mapped[str | None] = mapped_column(String(256))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------- #
# House / lab / devices (items 11–25)
# --------------------------------------------------------------------------- #


class OwnerTimer(Base):
    """Durable owner timer. Survives process restart; daemon due-scans fire it."""

    __tablename__ = "timers"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    late: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Delegate(Base):
    """Time- and scope-boxed account share. Never a second owner."""

    __tablename__ = "delegates"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    person_id: Mapped[UUID | None] = mapped_column(ForeignKey("entities.id"), index=True)
    person_name: Mapped[str] = mapped_column(String(256), index=True)
    device_id: Mapped[UUID | None] = mapped_column(ForeignKey("devices.id"), index=True)
    scopes: Mapped[list] = mapped_column(JSONType, default=list)
    not_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    granted_by: Mapped[str] = mapped_column(String(64), default="owner")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HomeEntity(Base):
    """Smart-home inventory so names like 'lab lights' resolve."""

    __tablename__ = "home_entities"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    entity_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256), index=True)
    area: Mapped[str | None] = mapped_column(String(128), index=True)
    domain: Mapped[str] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(64), default="unknown")
    attributes: Mapped[dict] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class IndoorNode(Base):
    """Owner-authored indoor map node (a room or waypoint)."""

    __tablename__ = "indoor_nodes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    aliases: Mapped[list] = mapped_column(JSONType, default=list)
    photo_ref: Mapped[str | None] = mapped_column(String(512))
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IndoorEdge(Base):
    """Directed walk between two indoor nodes."""

    __tablename__ = "indoor_edges"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    from_node_id: Mapped[UUID] = mapped_column(ForeignKey("indoor_nodes.id"), index=True)
    to_node_id: Mapped[UUID] = mapped_column(ForeignKey("indoor_nodes.id"), index=True)
    instruction: Mapped[str] = mapped_column(Text, default="")
    meters: Mapped[float | None] = mapped_column(Float)


class LocationShare(Base):
    """Opt-in live location share. Never created without that person's consent."""

    __tablename__ = "location_shares"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    person_id: Mapped[UUID | None] = mapped_column(ForeignKey("entities.id"), index=True)
    person_name: Mapped[str] = mapped_column(String(256), index=True)
    token_expires: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_lat: Mapped[float | None] = mapped_column(Float)
    last_lon: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), default="consent")
    owner_family_device: Mapped[bool] = mapped_column(Boolean, default=False)
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Beacon(Base):
    """Owner-registered finder tag for *their* gear — never a person hunt."""

    __tablename__ = "beacons"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    label: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="ev_device", index=True)
    last_lat: Mapped[float | None] = mapped_column(Float)
    last_lon: Mapped[float | None] = mapped_column(Float)
    owner_only: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TelemetrySession(Base):
    """Owner-started vehicle/drone test window."""

    __tablename__ = "telemetry_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    label: Mapped[str] = mapped_column(String(128), default="test")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TelemetrySample(Base):
    """One owner-posted telemetry pulse (drone, vehicle, or phone stand-in)."""

    __tablename__ = "telemetry_samples"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("telemetry_sessions.id"), index=True
    )
    source: Mapped[str] = mapped_column(String(32), index=True)
    battery: Mapped[float | None] = mapped_column(Float)
    alt: Mapped[float | None] = mapped_column(Float)
    speed: Mapped[float | None] = mapped_column(Float)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)


class MailDraft(Base):
    """Draft-not-send mail. Helper send requires owner confirm."""

    __tablename__ = "mail_drafts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    mail_id: Mapped[str] = mapped_column(String(128), index=True)
    to_addr: Mapped[str | None] = mapped_column(String(256))
    subject: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    confirm: Mapped[bool] = mapped_column(Boolean, default=False)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HardwareAudit(Base):
    """Every printer/drone/camera command is audited."""

    __tablename__ = "hardware_audits"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    command: Mapped[str] = mapped_column(String(64), index=True)
    args: Mapped[dict] = mapped_column(JSONType, default=dict)
    result: Mapped[dict] = mapped_column(JSONType, default=dict)
    actor: Mapped[str] = mapped_column(String(64), default="owner")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PublicFeed(Base):
    """Owner-picked public RSS/NWS feed — not a private scanner."""

    __tablename__ = "public_feeds"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32), default="rss", index=True)
    url: Mapped[str] = mapped_column(String(1024))
    label: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_items: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OwnerCamera(Base):
    """Owner-added camera only. Never LAN-discovered."""

    __tablename__ = "owner_cameras"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128), index=True)
    vault_ref: Mapped[str | None] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(32), default="upload")
    clip_attachment_id: Mapped[UUID | None] = mapped_column(ForeignKey("attachments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HudPush(Base):
    """Last validated HUD payload pushed to lookout / Watch / widget."""

    __tablename__ = "hud_pushes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    schema_version: Mapped[str] = mapped_column(String(32), default="ev.hud.card.v1")
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversation_threads.id"), index=True
    )
    prefer_haptic: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(64), default="tool")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# ============================================================================
# EVIE OS G1 — CORE STATE (Projects / Goals / Steps / Commitments)
# Canonical current-state projections. Durable history lives in `events`.
# ============================================================================

class Project(Base):
    """A persistent area of coordinated work. Same schema for every project;
    importance is expressed through priority, never through special cases."""

    __tablename__ = "projects"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor: Mapped[str] = mapped_column(String(64), index=True, default="master")
    title: Mapped[str] = mapped_column(String(256), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # ACTIVE | PAUSED | COMPLETED | ARCHIVED
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    # CRITICAL | HIGH | NORMAL | LOW
    priority: Mapped[str] = mapped_column(String(16), default="NORMAL", index=True)
    privacy_level: Mapped[str] = mapped_column(String(32), default="normal")
    source: Mapped[str] = mapped_column(String(32), default="owner")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # --- G2 EVIE EVERYWHERE (additive): optimistic-lock revision. Bumped on
    # every canonical change; clients send expected_version to avoid lost
    # conflicting writes. 0 for pre-G2 rows.
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Goal(Base):
    """An outcome to accomplish. May belong to a Project or stand alone.
    Operational truth for goal state — memory(type=goal) is recall only."""

    __tablename__ = "goals"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor: Mapped[str] = mapped_column(String(64), index=True, default="master")
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    parent_goal_id: Mapped[UUID | None] = mapped_column(ForeignKey("goals.id"), index=True)
    title: Mapped[str] = mapped_column(String(512), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # PLANNED | ACTIVE | BLOCKED | PAUSED | COMPLETED | CANCELLED
    state: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    priority: Mapped[str] = mapped_column(String(16), default="NORMAL", index=True)
    success_criteria: Mapped[str] = mapped_column(Text, default="")
    progress_note: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str] = mapped_column(Text, default="")
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    privacy_level: Mapped[str] = mapped_column(String(32), default="normal")
    source: Mapped[str] = mapped_column(String(32), default="owner")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # --- G2 EVIE EVERYWHERE (additive): optimistic-lock revision (see Project).
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class GoalStep(Base):
    """A concrete subordinate work item. Product state — NOT an execution job
    (ResearchSession/FleetTask/RoutineRun remain execution substrates)."""

    __tablename__ = "goal_steps"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    goal_id: Mapped[UUID] = mapped_column(ForeignKey("goals.id"), index=True)
    title: Mapped[str] = mapped_column(String(512))
    # PENDING | DONE | SKIPPED
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Commitment(Base):
    """A promise/obligation. Explicit creation only in G1 — no silent
    extraction from casual conversation."""

    __tablename__ = "commitments"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor: Mapped[str] = mapped_column(String(64), index=True, default="master")
    description: Mapped[str] = mapped_column(Text)
    # OPEN | FULFILLED | CANCELLED | MISSED
    status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    project_id: Mapped[UUID | None] = mapped_column(ForeignKey("projects.id"), index=True)
    goal_id: Mapped[UUID | None] = mapped_column(ForeignKey("goals.id"), index=True)
    entity_id: Mapped[UUID | None] = mapped_column(ForeignKey("entities.id"), index=True)
    source_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("events.id"))
    privacy_level: Mapped[str] = mapped_column(String(32), default="normal")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
