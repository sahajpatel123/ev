from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field

PrivacyLevel = Literal["private", "normal", "sensitive", "never_send_to_model"]
MemoryType = Literal[
    "episodic",
    "semantic",
    "fact",
    "decision",
    "goal",
    "preference",
    "observation",
    "pattern",
    "summary",
    "message",
    "lesson",
]
SourceType = Literal["explicit", "inferred", "derived"]


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


class EventCreate(BaseModel):
    source: str = Field(min_length=1, max_length=32)
    event_type: str = Field(min_length=1, max_length=64)
    content: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    occurred_at: datetime | None = None
    device_id: str | None = None
    conversation_id: UUID | None = None
    privacy_level: PrivacyLevel = "normal"
    text: str | None = None

    def effective_content(self) -> dict:
        content = dict(self.content)
        if self.text is not None:
            content["text"] = self.text
        return content


class EventOut(BaseModel):
    id: UUID
    occurred_at: datetime
    ingested_at: datetime
    source: str
    event_type: str
    content: dict
    metadata: dict = Field(validation_alias=AliasChoices("metadata_", "metadata"))
    device_id: str | None
    conversation_id: UUID | None
    privacy_level: str
    sha256: str
    tombstoned_at: datetime | None
    tombstone_reason: str | None

    model_config = {"from_attributes": True}


class EventCreateResponse(BaseModel):
    event: EventOut
    memory_delta: list[MemoryDelta] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Memories
# --------------------------------------------------------------------------- #


class EventRef(BaseModel):
    id: UUID
    occurred_at: datetime
    source: str
    event_type: str
    text: str | None = None


class EntityRefOut(BaseModel):
    id: UUID
    name: str
    entity_type: str
    role: str
    weight: float


class MemoryOut(BaseModel):
    id: UUID
    memory_type: str
    text: str
    payload: dict
    importance: float
    confidence: float
    source_type: str
    privacy_level: str
    event_time: datetime
    created_time: datetime
    updated_time: datetime
    valid_from: datetime
    valid_until: datetime | None
    version_group: UUID
    version: int
    supersedes_id: UUID | None
    superseded_by_id: UUID | None
    reason_for_change: str | None
    is_current: bool
    redacted: bool = False
    source_events: list[EventRef] = Field(default_factory=list)
    entities: list[EntityRefOut] = Field(default_factory=list)


class MemoryDelta(BaseModel):
    id: UUID
    memory_type: str
    action: Literal["created", "updated", "conflict"]
    text: str


class ProvenanceItem(BaseModel):
    memory_id: UUID | None = None
    text: str
    memory_type: str
    score: float = 0.0
    components: dict[str, float] = Field(default_factory=dict)
    kind: str = "memory"
    attachment_id: UUID | None = None
    perception_event_id: UUID | None = None
    source_event_id: UUID | None = None
    raw_sent: bool = False


# --------------------------------------------------------------------------- #
# Intelligence filter
# --------------------------------------------------------------------------- #


class FilterFlagOut(BaseModel):
    stage: str
    name: str
    severity: str
    detail: str = ""
    action: str = "allow"


class ClaimOut(BaseModel):
    text: str
    kind: str
    supported: bool
    evidence: list[str] = Field(default_factory=list)
    score: float = 0.0
    action: str = "keep"


class FilterReportOut(BaseModel):
    draft: str
    final_text: str
    edits: list[dict] = Field(default_factory=list)
    claims: list[ClaimOut] = Field(default_factory=list)
    structural: dict = Field(default_factory=dict)
    persona: dict = Field(default_factory=dict)
    safety: dict = Field(default_factory=dict)
    critic: dict = Field(default_factory=dict)
    iterations: int = 0
    passed: bool = True
    flags: list[FilterFlagOut] = Field(default_factory=list)


class FilterLedgerOut(BaseModel):
    id: UUID
    request_id: str
    conversation_id: UUID | None
    stage: str
    action: str
    name: str
    severity: str
    detail: dict
    draft: str | None
    final_text: str | None
    scores: dict | None
    iterations: int
    costs: dict | None
    envelope_hash: str | None
    model: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class FilterLedgerAggregate(BaseModel):
    total: int
    by_stage: dict[str, int] = Field(default_factory=dict)
    by_action: dict[str, int] = Field(default_factory=dict)
    blocked_inputs: int = 0
    redactions: int = 0
    softenings: int = 0
    repairs: int = 0
    refinements: int = 0
    over_refinement_rate: float | None = None


class FilterEvaluateRequest(BaseModel):
    message: str = Field(min_length=1, max_length=200_000)
    draft: str | None = Field(default=None, max_length=200_000)
    conversation_id: UUID | None = None


class FilterEvaluateResponse(BaseModel):
    request_id: str
    input: dict
    output: FilterReportOut | None = None
    context_tokens: int = 0
    ledger_ids: list[UUID] = Field(default_factory=list)
    blocked: bool = False
    block_reason: str | None = None


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=200_000)
    conversation_id: UUID | None = None
    device_id: str | None = None
    stream: bool = False
    model: str | None = None
    context_depth: Literal["auto", "standard", "deep", "deepest"] = "auto"
    attachment_id: UUID | None = None
    allow_raw_media: bool = False
    allow_sensitive_tools: bool = False


class ChatResponse(BaseModel):
    reply: str
    conversation_id: UUID | None
    model: str | None
    context_tokens: int
    context_depth: str = "standard"
    request_id: str | None = None
    memory_delta: list[MemoryDelta] = Field(default_factory=list)
    provenance: list[ProvenanceItem] = Field(default_factory=list)
    filter_report: FilterReportOut | None = None
    context_plan: dict | None = None
    surfaces: dict | None = None


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


class TimelineResponse(BaseModel):
    events: list[EventOut]
    next_cursor: datetime | None = None


class MemoryListResponse(BaseModel):
    memories: list[MemoryOut]
    total: int


class WeekRecallMemoryOut(BaseModel):
    id: UUID
    memory_type: str
    text: str
    importance: float
    event_time: datetime
    payload: dict = Field(default_factory=dict)


class WeekRecallConsolidationOut(BaseModel):
    period_start: datetime
    period_end: datetime
    summary: str
    topics: list[dict] = Field(default_factory=list)
    event_count: int = 0


class WeekRecallOut(BaseModel):
    week_start: datetime
    week_end: datetime
    as_of: datetime
    events: list[EventOut] = Field(default_factory=list)
    memories: list[WeekRecallMemoryOut] = Field(default_factory=list)
    decisions: list[WeekRecallMemoryOut] = Field(default_factory=list)
    goals: list[WeekRecallMemoryOut] = Field(default_factory=list)
    consolidation: WeekRecallConsolidationOut | None = None
    event_count: int = 0
    memory_count: int = 0
    top_topics: list[str] = Field(default_factory=list)


class MemoryChangeGroup(BaseModel):
    version_group: UUID
    memory_type: str
    versions: list[MemoryOut]


class MemoryChangesResponse(BaseModel):
    since: datetime
    memory_type: str | None = None
    total: int
    groups: list[MemoryChangeGroup]


class ConflictOut(BaseModel):
    id: UUID
    memory_id_a: UUID
    memory_id_b: UUID
    reason: str
    status: str
    resolution: str | None
    created_time: datetime
    resolved_time: datetime | None

    model_config = {"from_attributes": True}


class AccessLogOut(BaseModel):
    id: UUID
    occurred_at: datetime
    actor: str
    action: str
    endpoint: str | None
    resource_type: str | None
    resource_ids: list
    details: dict

    model_config = {"from_attributes": True}


class AuditOut(BaseModel):
    memory: MemoryOut
    versions: list[MemoryOut]
    source_events: list[EventOut]
    conflicts: list[ConflictOut]
    access_log: list[AccessLogOut]


class ExportBundle(BaseModel):
    exported_at: datetime
    version: str = "1.0"
    events: list[EventOut]
    memories: list[MemoryOut]
    entities: list[dict]
    relationships: list[dict]
    conflicts: list[ConflictOut] = Field(default_factory=list)
    attachments: list[AttachmentOut] = Field(default_factory=list)
    devices: list[DeviceOut] = Field(default_factory=list)
    access_log: list[AccessLogOut] = Field(default_factory=list)


class RebuildOut(BaseModel):
    completed_at: datetime
    reason: str
    events_total: int
    events_replayed: int
    tombstoned_events: int
    redacted_memories: int
    memories_created: int
    patterns_created: int
    summaries_created: int
    lessons_created: int
    operations_applied: int
    deleted_memories: int
    deleted_entities: int
    deleted_relationships: int
    deleted_conflicts: int


class ImportResponse(BaseModel):
    mode: str
    events_imported: int
    events_skipped: int
    completed_at: datetime
    reason: str
    events_total: int
    events_replayed: int
    tombstoned_events: int
    redacted_memories: int
    memories_created: int
    patterns_created: int
    summaries_created: int
    lessons_created: int
    operations_applied: int


class ConsolidationOut(BaseModel):
    granularity: str
    period_start: datetime
    period_end: datetime
    executed_at: datetime
    written: list[UUID]


# --------------------------------------------------------------------------- #
# Backup & restore
# --------------------------------------------------------------------------- #


class BackupCreateRequest(BaseModel):
    passphrase: str = Field(min_length=8, max_length=512)
    destination: str | None = Field(default=None, max_length=1024)


class BackupOut(BaseModel):
    path: str
    schema_version: str
    created_at: datetime
    checksum: str
    size_bytes: int
    counts: dict


class BackupVerifyRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    passphrase: str = Field(min_length=1, max_length=512)


class BackupVerifyOut(BaseModel):
    valid: bool
    schema_version: str
    created_at: datetime | None = None
    counts: dict = Field(default_factory=dict)
    checksum_match: bool = False
    reason: str | None = None


class BackupRestoreRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    passphrase: str = Field(min_length=8, max_length=512)
    mode: Literal["merge", "wipe"] = "merge"
    confirm_wipe: bool = False


class BackupRestoreOut(BaseModel):
    mode: str
    restored_at: datetime
    events_restored: int
    events_skipped: int
    attachments_restored: int
    blobs_restored: int = 0
    devices_restored: int
    access_log_restored: int
    backup_counts: dict
    rebuild: dict


# --------------------------------------------------------------------------- #
# Devices & attachments
# --------------------------------------------------------------------------- #


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    capabilities: list[str] = Field(default_factory=list)
    trust_level: Literal["device", "owner"] = "device"
    # --- AGENT 14 PULSE (WAVE LIFE, additive) ---
    device_type: Literal["mac", "phone", "watch", "desktop", "unknown"] = "unknown"
    platform: Literal["apple", "android", "web", "unknown"] | None = None


class DeviceOut(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None
    capabilities: list
    trust_level: str = "device"
    owner_id: UUID | None = None
    # --- AGENT 14 PULSE (WAVE LIFE, additive) ---
    device_type: str | None = None
    platform: str | None = None
    paired_at: datetime | None = None
    push_platform: str | None = None
    push_registered: bool = False
    bootstrapped_at: datetime | None = None
    bootstrapped_spoken_at: datetime | None = None

    model_config = {"from_attributes": True}


class OwnerPrefsOut(BaseModel):
    nickname: str
    quiet_hours: dict = Field(default_factory=dict)
    hud_layout: dict = Field(default_factory=dict)
    feature_gates: list[dict] = Field(default_factory=list)
    tts_voice: str | None = "default"
    live_conversation_id: str | None = None
    volume_percent: int = 70


class DeviceBootstrapOut(BaseModel):
    device_id: str
    prefs: dict
    spoken: bool = False
    spoken_text: str | None = None
    tts_device_id: str | None = None
    bootstrapped_spoken_at: str | None = None
    prefs_loaded: bool = True
    actor: str | None = None


class TranscriptEventOut(BaseModel):
    id: str
    event_type: str
    occurred_at: str
    text: str = ""
    source: str | None = None


class TranscriptOut(BaseModel):
    conversation_id: str
    events: list[TranscriptEventOut] = Field(default_factory=list)


class AttachmentOut(BaseModel):
    id: UUID
    event_id: UUID
    filename: str
    content_type: str | None
    size_bytes: int
    storage_key: str
    sha256: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------- #
# Gateway
# --------------------------------------------------------------------------- #


class GatewayMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None


class GatewayToolCall(BaseModel):
    id: str
    name: str
    arguments: dict


class GatewayChatRequest(BaseModel):
    messages: list[GatewayMessage]
    model: str | None = None
    temperature: float = 0.7
    tools: list[dict] = Field(default_factory=list)
    request_id: str | None = None
    strategy: dict | None = None
    memories: list[dict] = Field(default_factory=list)
    context: dict = Field(default_factory=dict)
    conversation_id: UUID | None = None
    device_id: str | None = None
    allow_sensitive_tools: bool = False


class GatewayChatResponse(BaseModel):
    text: str
    tool_calls: list[GatewayToolCall] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)
    request_id: str
    provider: str
    model: str | None = None
    latency_ms: float = 0.0
    status: str = "ok"
    error: str | None = None
    tool_validation: list[dict] = Field(default_factory=list)
    envelope: dict = Field(default_factory=dict)


class ModelCallOut(BaseModel):
    id: UUID
    request_id: str
    actor: str
    provider: str
    model: str | None
    status: str
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    tool_calls: list
    envelope: dict
    envelope_hash: str | None
    error: str | None
    media_refs: list[dict] = Field(default_factory=list)
    created_at: datetime

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------- #
# Interaction intelligence
# --------------------------------------------------------------------------- #


CommunicationMode = Literal[
    "casual",
    "technical",
    "analytical",
    "coaching",
    "emergency",
    "collaborative",
    "social",
]


class InteractionModeRequest(BaseModel):
    message: str = Field(min_length=1, max_length=50_000)
    context: str | None = None
    conversation_id: UUID | None = None


class InteractionStrategy(BaseModel):
    mode: CommunicationMode
    intent: str
    urgency: float = Field(ge=0.0, le=1.0)
    emotional_state: str
    length_target: str
    directness: str
    assertiveness: int = Field(ge=0, le=4)
    ask_question: bool = False
    challenge: bool = False
    rationale: str
    surface_hint: str | None = None


class InteractionModeResponse(BaseModel):
    message: str
    mode: CommunicationMode
    strategy: InteractionStrategy


# --------------------------------------------------------------------------- #
# Tactical mode / HUD briefings
# --------------------------------------------------------------------------- #


class TacticalBriefRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    stakes: str | None = None
    context: str | None = None
    include_options: bool = True


class TacticalQuickRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=500)
    stakes: str | None = None
    context: str | None = None
    ttl_seconds: int = Field(default=3600, ge=0, le=86400 * 7)


class TacticalRisk(BaseModel):
    description: str
    likelihood: float = Field(ge=0.0, le=1.0)
    impact: float = Field(ge=0.0, le=1.0)
    mitigation: str | None = None


class TacticalOption(BaseModel):
    label: str
    summary: str
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class TacticalBriefOut(BaseModel):
    schema_version: Literal["ev.hud.briefing.v1"] = "ev.hud.briefing.v1"
    generated_at: datetime
    objective: str
    context: str
    people: list[dict] = Field(default_factory=list)
    risks: list[TacticalRisk] = Field(default_factory=list)
    options: list[TacticalOption] = Field(default_factory=list)
    recommendation: str | None = None
    decision_history: list[dict] = Field(default_factory=list)
    talking_points: list[str] = Field(default_factory=list)
    provenance: list[ProvenanceItem] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Diagnostics / calibration
# --------------------------------------------------------------------------- #


class DiagnosticCheck(BaseModel):
    name: str
    status: Literal["ok", "degraded", "failed"]
    latency_ms: float = 0.0
    details: dict = Field(default_factory=dict)


class CalibrationReport(BaseModel):
    schema_version: Literal["ev.calibration.v1"] = "ev.calibration.v1"
    generated_at: datetime
    overall: Literal["ok", "degraded", "failed"]
    checks: list[DiagnosticCheck] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class DiagnosticsLastOut(BaseModel):
    schema_version: Literal["ev.diagnostics.last.v1"] = "ev.diagnostics.last.v1"
    generated_at: datetime | None = None
    stale: bool = True
    overall: Literal["ok", "degraded", "failed", "unknown"] = "unknown"
    report: CalibrationReport | None = None
    hud: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Health radar
# --------------------------------------------------------------------------- #


class HealthSnapshotCreate(BaseModel):
    occurred_at: datetime | None = None
    source: str = "api"
    device_id: str | None = None
    metrics: dict[str, float] = Field(default_factory=dict)


class HealthSnapshotOut(BaseModel):
    id: UUID
    occurred_at: datetime
    source: str
    device_id: str | None
    metrics: dict
    readiness: float | None
    band: str | None
    anomalies: list
    created_at: datetime

    model_config = {"from_attributes": True}


class AttachmentCreateResponse(BaseModel):
    attachment: AttachmentOut
    event: EventOut


# --------------------------------------------------------------------------- #
# Memory correction & forgetting
# --------------------------------------------------------------------------- #


class MemoryCorrectionCreate(BaseModel):
    corrected_text: str = Field(min_length=1, max_length=20_000)
    reason: str = "user correction"


class MemoryForgetCreate(BaseModel):
    reason: str = "user requested"


class ContinueResponse(BaseModel):
    resolved: bool
    focus: str
    conversation_id: UUID | None = None
    state: UserStateOut
    summary: str | None = None
    recent_context: list[dict] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class ToolSelectionRequest(BaseModel):
    message: str = Field(min_length=1, max_length=50_000)


class ToolSelectionResponse(BaseModel):
    message: str
    selected: str
    alternatives: list[str] = Field(default_factory=list)
    rationale: str


# --------------------------------------------------------------------------- #
# Single continuous conversation
# --------------------------------------------------------------------------- #


class ConversationOut(BaseModel):
    id: UUID
    title: str
    is_default: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationMessageOut(BaseModel):
    id: UUID
    role: Literal["user", "assistant"]
    text: str
    occurred_at: datetime


class ConversationStateOut(BaseModel):
    focus: str | None = None
    recent_topics: list[str] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)
    working_context: dict = Field(default_factory=dict)
    updated_at: datetime | None = None


class ConversationRollupOut(BaseModel):
    summary: str
    covered_turn_count: int
    token_count: int
    updated_at: datetime | None = None


class ConversationDetail(BaseModel):
    conversation: ConversationOut
    messages: list[ConversationMessageOut] = Field(default_factory=list)
    state: ConversationStateOut
    rollup: ConversationRollupOut | None = None
    next_actions: list[str] = Field(default_factory=list)


class ConversationResetRequest(BaseModel):
    reason: str = "start fresh"


# --------------------------------------------------------------------------- #
# Live data recording
# --------------------------------------------------------------------------- #


class LiveChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: Literal["screen", "audio", "health", "app", "vision", "location"] = "app"
    privacy_level: PrivacyLevel = "normal"
    metadata: dict = Field(default_factory=dict)


class LiveChannelOut(BaseModel):
    id: UUID
    name: str
    kind: str
    active: bool
    privacy_level: str
    metadata: dict = Field(validation_alias="metadata_")
    created_at: datetime
    last_event_at: datetime | None

    model_config = {"from_attributes": True}


class LiveEventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)
    occurred_at: datetime | None = None
    device_id: str | None = None
    privacy_level: PrivacyLevel = "normal"


class LiveEventOut(BaseModel):
    id: UUID
    channel_id: UUID
    occurred_at: datetime
    ingested_at: datetime
    event_type: str
    payload: dict
    device_id: str | None
    collector: str | None = None
    privacy_level: str
    sha256: str
    consumed: bool

    model_config = {"from_attributes": True}


class LiveEventBatchRequest(BaseModel):
    channel: str = Field(min_length=1, max_length=128)
    kind: Literal["screen", "audio", "health", "app", "vision", "location"] = "app"
    events: list[LiveEventCreate] = Field(min_length=1, max_length=500)
    privacy_level: PrivacyLevel = "normal"


class LiveChannelStatus(BaseModel):
    channel: LiveChannelOut
    event_count: int = 0
    last_event_at: datetime | None = None


class LiveStatusOut(BaseModel):
    channels: list[LiveChannelStatus] = Field(default_factory=list)
    total_events_24h: int = 0
    consumed_24h: int = 0


class LiveChannelDerivedOut(BaseModel):
    channel_id: UUID
    channel_name: str
    kind: str
    event_count: int = 0
    consumed_count: int = 0
    first_event_at: datetime | None = None
    last_event_at: datetime | None = None
    latest_event_id: UUID | None = None
    signals: list = Field(default_factory=list)


class LiveRebuildOut(BaseModel):
    completed_at: datetime
    reason: str
    events_total: int = 0
    events_replayed: int = 0
    consumed_count: int = 0
    channels_rebuilt: int = 0
    deleted_derived_rows: int = 0
    channels: list[LiveChannelDerivedOut] = Field(default_factory=list)


class LiveRetentionOut(BaseModel):
    completed_at: datetime
    days: int
    cutoff: datetime
    dry_run: bool
    events_scanned: int = 0
    events_deleted: int = 0
    events_kept_latest: int = 0
    events_protected: int = 0
    channels_updated: int = 0


# --------------------------------------------------------------------------- #
# Voice & speech (EVIE)
# --------------------------------------------------------------------------- #


class VoiceWakeRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    wake_word: str = Field(default="evie", min_length=1, max_length=32)
    priority: float = Field(default=0.5, ge=0, le=1)
    text_hint: str | None = Field(default=None, max_length=256)
    audio_ref: str | None = None
    audio_b64: str | None = None
    push_to_talk: bool = False
    wake_confidence: float | None = Field(default=None, ge=0, le=1)


class VoiceWakeResponse(BaseModel):
    session_id: UUID | None = None
    state: str
    owner_enrolled: bool
    challenge_nonce: str | None = None
    challenge_phrase: str | None = None
    message: str | None = None
    greeting: str | None = None
    onboarding: str | None = None
    conversation_id: UUID | None = None


class VoiceLiveOpenRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)


class VoiceLiveOpenResponse(BaseModel):
    session_id: UUID
    state: str
    conversation_id: UUID | None = None
    live: bool = True
    message: str | None = None
    greeting: str | None = None
    onboarding: str | None = None


class VoiceSessionVerifyRequest(BaseModel):
    session_id: UUID
    nonce: str = Field(min_length=1, max_length=256)
    samples: list[str] = Field(min_length=1, max_length=20)
    phrase: str | None = Field(default=None, max_length=512)
    audio_ref: str | None = None
    liveness_proof: str | None = None
    live_score: float | None = Field(default=None, ge=0, le=1)
    audio_sha256: str | None = Field(default=None, max_length=64)


class VoiceSessionVerifyResponse(BaseModel):
    session_id: UUID | None = None
    state: str
    verified: bool
    confidence: float = 0.0
    reason: str = ""
    conversation_id: UUID | None = None
    greeting: str | None = None
    onboarding: str | None = None


class SpeechStyleOut(BaseModel):
    urgency: float = 0.0
    warmth: float = 0.6
    brevity: float = 0.4
    mode: str = "casual"
    length_target: str = "one to two sentences"
    directness: str = "low to medium"


class TtsOut(BaseModel):
    provider: str
    audio_ref: str | None = None
    audio_b64: str | None = None
    content_type: str | None = None
    ssml: str | None = None
    duration_ms: int | None = None
    degraded: bool = False


class VoicePartialOut(BaseModel):
    """One incremental ASR hypothesis for the streaming voice surface."""

    text: str
    provider: str
    sequence: int
    stable: bool = False
    confidence: float = 0.0
    degraded: bool = False
    timestamp_ms: int | None = None


class VoiceUtteranceRequest(BaseModel):
    session_id: UUID
    text: str | None = Field(default=None, max_length=200_000)
    audio_b64: str | None = None
    audio_ref: str | None = None
    reverify_token: str | None = None
    language: str = "en"
    conversation_id: UUID | None = None
    follow_up: bool = False
    # Agent 4 Voice — Wave Life: explicit push-to-talk bypasses the
    # per-utterance VAD + owner-verification addressivity gate.
    push_to_talk: bool = False


class VoiceUtteranceResponse(BaseModel):
    session_id: UUID
    state: str
    transcript: str
    transcript_confidence: float = 0.0
    transcript_degraded: bool = False
    transcript_provider: str | None = None
    reply: str
    conversation_id: UUID | None = None
    tts: TtsOut | None = None
    tts_device_id: UUID | None = None
    style: SpeechStyleOut | None = None
    model: str | None = None
    context_tokens: int = 0
    memory_deltas: list[MemoryDelta] = Field(default_factory=list)
    error: str | None = None


class VoiceStatusOut(BaseModel):
    session_id: UUID | None = None
    state: str
    owner_enrolled: bool
    owner_verified: bool = False
    device_id: str | None = None
    speaker_confidence: float | None = None
    follow_up_remaining_seconds: int = 0
    expires_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: str | None = None


# --------------------------------------------------------------------------- #
# E.D.I.T.H.-inspired: focus, fleet, recognition, ops, twin
# --------------------------------------------------------------------------- #


class FocusDesignationCreate(BaseModel):
    label: str = Field(min_length=1, max_length=256)
    kind: Literal["task", "project", "person", "topic", "goal"] = "task"
    target_id: str | None = None
    reason: str | None = None


class FocusDesignationOut(BaseModel):
    id: UUID
    label: str
    kind: str
    target_id: str | None
    active: bool
    reason: str | None
    started_at: datetime
    ended_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class FocusSuggestion(BaseModel):
    label: str
    kind: Literal["task", "project", "person", "topic", "goal"]
    reason: str
    source: str
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    target_id: str | None = None


class FocusSuggestResponse(BaseModel):
    generated_at: datetime
    suggestions: list[FocusSuggestion] = Field(default_factory=list)


class FleetTaskCreate(BaseModel):
    device_id: UUID
    task_type: str = Field(min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)


class FleetTaskCompleteRequest(BaseModel):
    result: dict = Field(default_factory=dict)


class FleetTaskFailRequest(BaseModel):
    error: str = Field(min_length=1, max_length=2000)


class FleetTaskOut(BaseModel):
    id: UUID
    device_id: UUID
    task_type: str
    status: str
    payload: dict
    result: dict | None
    requested_by: str
    accepted_by: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class FleetDeviceOut(BaseModel):
    device_id: UUID
    name: str
    capabilities: list
    last_seen_at: datetime | None
    latest_gear: dict | None = None
    presence: Literal["online", "away", "unknown"] = "unknown"


class FleetStatusOut(BaseModel):
    devices: list[FleetDeviceOut] = Field(default_factory=list)
    online_count: int = 0
    active_tasks: int = 0


class RecognitionCreate(BaseModel):
    label: str = Field(min_length=1, max_length=256)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    entity_type: Literal["person", "place", "project", "topic", "thing"] = "topic"
    event_id: UUID | None = None
    live_event_id: UUID | None = None
    attachment_id: UUID | None = None
    source: str = "user"


class RecognitionOut(BaseModel):
    id: UUID
    event_id: UUID | None
    live_event_id: UUID | None
    attachment_id: UUID | None
    entity_id: UUID | None
    label: str
    confidence: float
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}


class VisionAnalyzeRequest(BaseModel):
    """Request vision analysis of one user-owned attachment."""

    attachment_id: UUID
    permission: bool = False
    allow_raw: bool = False
    prompt: str | None = Field(default=None, max_length=2000)


class VisionPerceptionOut(BaseModel):
    """One derived perception record with full provenance."""

    id: UUID
    attachment_id: UUID | None = None
    source_event_id: UUID | None = None
    summary: str
    labels: list[dict] = Field(default_factory=list)
    confidence: float = 0.0
    provider: str
    raw_sent: bool = False
    permission_granted_by: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    ocr_text: str | None = None
    ocr_provider: str | None = None
    derived_text_used: bool = False
    request_id: str | None = None
    created_at: datetime


class ConfirmRecognitionRequest(BaseModel):
    entity_type: Literal["person", "place", "project", "topic", "thing"] = "thing"


class OpsCenterOut(BaseModel):
    generated_at: datetime
    state: UserStateOut
    focus: FocusDesignationOut | None = None
    health: HealthSummaryOut | None = None
    alerts: list[AlertOut] = Field(default_factory=list)
    fleet: FleetStatusOut | None = None
    open_decisions: list[dict] = Field(default_factory=list)
    patterns: list[dict] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    recent_commands: list[CommandOut] = Field(default_factory=list)
    metrics: dict | None = None


class CommandOut(BaseModel):
    id: UUID
    command_type: str
    target_type: str | None = None
    target_id: str | None = None
    actor: str
    status: str
    request: dict = Field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class TwinOut(BaseModel):
    generated_at: datetime
    facts: list[dict] = Field(default_factory=list)
    preferences: list[dict] = Field(default_factory=list)
    goals: list[dict] = Field(default_factory=list)
    patterns: list[dict] = Field(default_factory=list)
    relationship: RelationshipOut | None = None
    health: dict | None = None
    confidence: float = 0.0


class HudFocusOut(BaseModel):
    schema_version: Literal["ev.hud.focus.v1"] = "ev.hud.focus.v1"
    generated_at: datetime
    focus: FocusDesignationOut | None = None
    locked: bool
    context: str
    next_action: str | None = None
    meta: dict = Field(default_factory=dict)


class DeviceCreateResponse(BaseModel):
    device: DeviceOut
    token: str


# --------------------------------------------------------------------------- #
# Identity & trust lifecycle
# --------------------------------------------------------------------------- #


class OwnerCreateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)


class RecoveryCodeOut(BaseModel):
    code: str
    expires_at: str | None = None
    label: str | None = None


class OwnerCreateResponse(BaseModel):
    owner_id: UUID
    display_name: str
    recovery_codes: list[RecoveryCodeOut] = Field(default_factory=list)


class RecoveryCodesResponse(BaseModel):
    owner_id: UUID
    recovery_codes: list[RecoveryCodeOut] = Field(default_factory=list)


class IdentityStatusOut(BaseModel):
    owner_established: bool
    owner_id: UUID | None = None
    display_name: str | None = None
    trust_level: str = "device"
    actor: str = ""
    devices_active: int = 0
    recovery_codes_remaining: int = 0
    passkeys_active: int = 0
    recovery_locked: bool = False


class PasskeyRegisterRequest(BaseModel):
    credential_id: str = Field(min_length=8, max_length=1024)
    name: str = Field(min_length=1, max_length=128)
    device_id: UUID | None = None


class PasskeyOut(BaseModel):
    id: UUID
    name: str
    device_id: UUID | None = None
    created_at: datetime
    revoked_at: datetime | None = None

    model_config = {"from_attributes": True}


class PasskeyRegisterResponse(BaseModel):
    passkey: PasskeyOut


class RecoveryRedeemRequest(BaseModel):
    code: str = Field(min_length=8, max_length=256)
    device_name: str = Field(min_length=1, max_length=128)
    capabilities: list[str] = Field(default_factory=list)


class RecoveryRedeemResponse(BaseModel):
    device: DeviceOut
    token: str
    owner_id: UUID


class ReverificationRequest(BaseModel):
    purpose: Literal[
        "integration.action",
        "memory.delete",
        "memory.export",
        "runtime.action",
        "voice.revoke",
        "voice.delete",
        "voice.sensitive_action",
        "face.revoke",
        "face.delete",
        "recovery.rotate",
        "vault.rotate",
        "backup.restore",
        "compliance.erasure",
        "adapter.activate",
        "adapter.delete",
        "person.delete",
        "fleet.write",
        "life.action",
    ]
    voice_session_id: UUID | None = None


class ReverificationResponse(BaseModel):
    token: str
    purpose: str
    expires_at: str


class ReverificationConsumeRequest(BaseModel):
    token: str = Field(min_length=8, max_length=256)
    purpose: Literal[
        "integration.action",
        "memory.delete",
        "memory.export",
        "runtime.action",
        "voice.revoke",
        "voice.delete",
        "voice.sensitive_action",
        "face.revoke",
        "face.delete",
        "recovery.rotate",
        "vault.rotate",
        "backup.restore",
        "compliance.erasure",
        "adapter.activate",
        "adapter.delete",
        "person.delete",
        "fleet.write",
        "life.action",
    ]


class ReverificationConsumeResponse(BaseModel):
    valid: bool = True
    purpose: str


class TrustMatrixOut(BaseModel):
    owner_required_actions: list[str]
    reverify_required_actions: list[str]
    levels: dict[str, int]


class HealthMetricPoint(BaseModel):
    occurred_at: datetime
    value: float


class HealthTrendOut(BaseModel):
    metric: str
    points: list[HealthMetricPoint]
    baseline_median: float | None = None
    current: float | None = None
    z_scores: list[float] = Field(default_factory=list)
    anomalies: list[dict] = Field(default_factory=list)


class HealthSummaryOut(BaseModel):
    generated_at: datetime
    readiness: float | None = None
    band: str | None = None
    sleep_hours: float | None = None
    hrv_ms: float | None = None
    resting_hr: float | None = None
    recommendation: str
    open_question: str | None = None
    anomalies: list[dict] = Field(default_factory=list)
    heart_rate: float | None = None
    spo2: float | None = None
    stress: float | None = None
    source: str | None = None
    emergency: bool = False


# --------------------------------------------------------------------------- #
# Alert radar
# --------------------------------------------------------------------------- #


class WatchlistCreate(BaseModel):
    kind: Literal["topic", "project", "person", "product", "company", "deadline", "date"]
    value: str = Field(min_length=1, max_length=256)
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    sources: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    active: bool = True


class WatchlistOut(BaseModel):
    id: UUID
    kind: str
    value: str
    priority: float
    sources: list
    active: bool
    metadata: dict = Field(validation_alias="metadata_")
    created_at: datetime
    last_matched_at: datetime | None

    model_config = {"from_attributes": True}


class AlertOut(BaseModel):
    id: UUID
    kind: str
    title: str
    body: str
    priority: float
    tier: str
    status: str
    source: str | None
    trigger_ids: list
    rationale: str | None
    fingerprint: str
    created_at: datetime
    delivered_at: datetime | None
    dismissed_at: datetime | None
    dismissed_reason: str | None
    details: dict

    model_config = {"from_attributes": True}


class AlertScanResponse(BaseModel):
    scanned_events: int
    scanned_memories: int
    alerts_created: list[AlertOut] = Field(default_factory=list)
    existing_alerts: int = 0


class AlertDismissRequest(BaseModel):
    reason: str = "dismissed"


# --------------------------------------------------------------------------- #
# EV Sense / predictions
# --------------------------------------------------------------------------- #


class SensePredictRequest(BaseModel):
    context: str | None = None
    window_days: int = Field(default=30, ge=1, le=365)


class SensePrediction(BaseModel):
    kind: str
    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    intervention_score: float = Field(ge=0.0, le=1.0)
    why_now: str
    basis_ids: list[str] = Field(default_factory=list)
    tier: Literal["do_nothing", "mention_later", "notify", "notify_card"] = "do_nothing"
    deliver: bool = False


class SensePredictResponse(BaseModel):
    predictions: list[SensePrediction] = Field(default_factory=list)
    model_used: str = "rule-based"
    generated_at: datetime


class PredictionOut(BaseModel):
    id: UUID
    kind: str
    text: str
    confidence: float
    basis_ids: list
    rationale: str | None
    intervention_score: float | None
    created_at: datetime
    delivered_at: datetime | None
    outcome: str
    reviewed_at: datetime | None
    details: dict

    model_config = {"from_attributes": True}


class PredictionOutcomeUpdate(BaseModel):
    outcome: Literal["correct", "incorrect", "unknown", "pending"]


# --------------------------------------------------------------------------- #
# Gear telemetry
# --------------------------------------------------------------------------- #


class GearSnapshotCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    reported_at: datetime | None = None
    battery_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    storage_free_bytes: int | None = None
    memory_used_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    cpu_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    uptime_seconds: int | None = None
    details: dict = Field(default_factory=dict)


class GearSnapshotOut(BaseModel):
    id: UUID
    device_id: str
    reported_at: datetime
    battery_percent: float | None
    storage_free_bytes: int | None
    memory_used_percent: float | None
    cpu_percent: float | None
    uptime_seconds: int | None
    details: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class GearScanResponse(BaseModel):
    scanned_devices: int
    alerts_created: list[AlertOut] = Field(default_factory=list)
    duplicates_skipped: int = 0


# --------------------------------------------------------------------------- #
# Decision intelligence
# --------------------------------------------------------------------------- #


class DecisionOutcomeCreate(BaseModel):
    expected_outcome: str | None = None
    actual_outcome: str = Field(min_length=1)
    lesson: str | None = None


class DecisionOutcomeOut(BaseModel):
    id: UUID
    decision_memory_id: UUID | None
    decision_topic: str
    expected_outcome: str | None
    actual_outcome: str | None
    reviewed_at: datetime | None
    lesson: str | None
    lesson_memory_id: UUID | None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------- #
# Person finder & user state
# --------------------------------------------------------------------------- #


class PersonWhereaboutsOut(BaseModel):
    name: str
    entity_id: UUID | None = None
    relationship: str | None = None
    last_seen: dict | None = None
    recent_mentions: list[dict] = Field(default_factory=list)
    sightings: list[dict] = Field(default_factory=list)
    related_memories: list[dict] = Field(default_factory=list)
    total_events: int = 0
    # AGENT 7 ROSTER fusion: enrolled identity, face/voice sightings, biodata.
    enrolled: dict | None = None
    face_sightings: list[dict] = Field(default_factory=list)
    voice_sightings: list[dict] = Field(default_factory=list)
    public_biodata: dict | None = None
    biodata_merged: bool = False


# --------------------------------------------------------------------------- #
# AGENT 7 ROSTER — consented face enrollment, recognition, public-figure biodata
# --------------------------------------------------------------------------- #


class FacePhotoIn(BaseModel):
    """One aligned face crop from Agent 6's YuNet detector (never raw stranger scans)."""

    image_b64: str = Field(min_length=1)
    quality: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    source: str = Field(default="photo", max_length=32)
    attachment_id: UUID | None = None
    live_event_id: UUID | None = None


class FaceEnrollmentCreate(BaseModel):
    person_name: str = Field(min_length=1, max_length=256)
    photos: list[FacePhotoIn] = Field(min_length=5, max_length=40)
    reason: str | None = Field(default=None, max_length=512)


class FaceEnrollmentDetailOut(BaseModel):
    id: UUID
    entity_id: UUID
    person_name: str
    version: int
    is_current: bool
    algorithm: str
    embedding_dim: int
    threshold: float
    sample_count: int
    status: str
    privacy_level: str
    consent_id: UUID | None = None
    supersedes_id: UUID | None = None
    superseded_by_id: UUID | None = None
    reason_for_change: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class FaceEnrollResponse(BaseModel):
    enrollment: FaceEnrollmentDetailOut
    sample_count: int
    raw_photos_stored: bool = False
    provider: str
    degraded: bool


class FaceRecognitionRequest(BaseModel):
    """A single aligned crop to match ONLY against enrolled templates."""

    image_b64: str = Field(min_length=1)
    quality: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: str = Field(default="live_frame", max_length=32)
    attachment_id: UUID | None = None
    live_event_id: UUID | None = None
    write_log: bool = True


class FaceRecognitionResponse(BaseModel):
    resolved: bool
    unknown: bool
    label: str | None = None
    entity_id: UUID | None = None
    confidence: float = 0.0
    threshold: float = 0.0
    provider: str
    degraded: bool
    candidates: list[dict] = Field(default_factory=list)
    recognition_id: UUID | None = None


class FaceRecognitionConfirmRequest(BaseModel):
    correct_label: str | None = Field(default=None, max_length=256)
    correct_entity_id: UUID | None = None
    reason: str | None = Field(default=None, max_length=512)


class FaceRecognitionConfirmOut(BaseModel):
    recognition_id: UUID
    label: str
    entity_id: UUID | None = None
    confidence: float
    source: str
    confirmed: bool = True
    created_at: datetime


class FaceCalibrationTrial(BaseModel):
    """One person's aligned crops; same-person pairs are genuine trials."""

    person: str = Field(min_length=1, max_length=256)
    images: list[str] = Field(min_length=2)


class FaceCalibrationRequest(BaseModel):
    trials: list[FaceCalibrationTrial] = Field(min_length=2)
    target_far: float = Field(default=1e-3, ge=1e-6, le=0.5)
    apply: bool = False


class FaceCalibrationReport(BaseModel):
    provider: str
    degraded: bool
    threshold: float
    tar_at_target_far: float
    target_far: float
    genuine_pairs: int
    impostor_pairs: int
    roc: list[dict] = Field(default_factory=list)
    calibrated_at: datetime


class AttributedFieldOut(BaseModel):
    value: str
    source_url: str
    license: str


class PublicFigureBiodataOut(BaseModel):
    name: str
    occupations: list[AttributedFieldOut] = Field(default_factory=list)
    notable_works: list[AttributedFieldOut] = Field(default_factory=list)
    dates: AttributedFieldOut | None = None
    summary: AttributedFieldOut | None = None
    source_url: str
    license: str
    fetched_at: datetime
    cached: bool = False
    provider: str = "wikidata"
    degraded: bool = False
    merged: bool = False


class PublicFigureLinkRequest(BaseModel):
    entity_id: UUID
    reason: str | None = Field(default=None, max_length=512)


# PEOPLE FROM LIFE — resolution, person context, roster (no People tab)


class PersonCandidateOut(BaseModel):
    entity_id: UUID | None = None
    name: str
    relationship: str | None = None
    provenance: str  # roster | memory | contact
    face_enrolled: bool = False
    consent_id: UUID | None = None
    last_seen: dict | None = None
    confidence: float | None = None
    candidate_only: bool = False


class PersonResolveOut(BaseModel):
    query: str
    candidates: list[PersonCandidateOut] = Field(default_factory=list)
    contacts_available: bool = False


class PersonContextOut(BaseModel):
    name: str
    entity_id: UUID | None = None
    relationship: str | None = None
    how_known: list[dict] = Field(default_factory=list)
    last_seen: dict | None = None
    enrolled: dict | None = None
    consent: dict | None = None
    match_state: str = "unknown"
    provenance: list[dict] = Field(default_factory=list)
    face_sightings: list[dict] = Field(default_factory=list)
    voice_sightings: list[dict] = Field(default_factory=list)


class PersonRosterEntryOut(BaseModel):
    entity_id: UUID
    name: str
    relationship: str | None = None
    face_enrolled: bool = False
    consent_id: UUID | None = None
    sample_count: int = 0
    last_seen: dict | None = None
    provenance: list[dict] = Field(default_factory=list)


class PersonRosterOut(BaseModel):
    people: list[PersonRosterEntryOut] = Field(default_factory=list)
    total: int = 0


class UserStateOut(BaseModel):
    activity: str | None = None
    current_focus: str | None = None
    active_project: str | None = None
    active_goal: str | None = None
    current_task: str | None = None
    recent_topics: list[str] = Field(default_factory=list)
    open_decisions: list[dict] = Field(default_factory=list)
    known_constraints: list[str] = Field(default_factory=list)
    recent_failures: list[str] = Field(default_factory=list)
    recent_successes: list[str] = Field(default_factory=list)
    live_context: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Research assistant
# --------------------------------------------------------------------------- #


class ResearchSessionCreate(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ResearchNoteCreate(BaseModel):
    note: str = Field(min_length=1, max_length=10_000)
    source_url: str | None = Field(default=None, max_length=1024)
    source_title: str | None = Field(default=None, max_length=512)


class ResearchNoteOut(BaseModel):
    id: UUID
    session_id: UUID
    event_id: UUID
    note: str
    source_url: str | None
    source_title: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ResearchSessionOut(BaseModel):
    id: UUID
    question: str
    status: str
    conclusion: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ResearchSessionDetail(ResearchSessionOut):
    notes: list[ResearchNoteOut] = Field(default_factory=list)


class ResearchConclude(BaseModel):
    conclusion: str = Field(min_length=1, max_length=10_000)


# --------------------------------------------------------------------------- #
# Maker companion
# --------------------------------------------------------------------------- #


class MakerProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    description: str | None = None
    status: Literal[
        "idea",
        "planning",
        "sourcing",
        "building",
        "testing",
        "done",
        "paused",
    ] = "idea"
    current_step: str | None = None


class MakerProjectOut(BaseModel):
    id: UUID
    name: str
    description: str | None
    status: str
    current_step: str | None
    goal_memory_id: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MakerProjectStatusUpdate(BaseModel):
    status: Literal[
        "idea",
        "planning",
        "sourcing",
        "building",
        "testing",
        "done",
        "paused",
    ]
    current_step: str | None = None


class MakerNextStepOut(BaseModel):
    project_id: UUID
    name: str
    current_status: str
    next_status: str
    next_step: str


class BomItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    qty: float = Field(default=1.0, ge=0)
    unit: str | None = None
    location: str | None = None
    reorder_at: float | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)


class BomItemOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    qty: float
    unit: str | None
    location: str | None
    reorder_at: float | None
    cost: float | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PrintJobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    estimated_minutes: int | None = Field(default=None, ge=1)
    filament_grams: float | None = Field(default=None, ge=0)


class PrintJobStatusUpdate(BaseModel):
    status: Literal["queued", "printing", "done", "failed"]
    error_log: str | None = None


class PrintJobOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    status: str
    estimated_minutes: int | None
    filament_grams: float | None
    error_log: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------- #
# Personality, relationship, self-evaluation
# --------------------------------------------------------------------------- #


class PersonalityUpdate(BaseModel):
    directness: int = Field(default=3, ge=1, le=5)
    humor: int = Field(default=2, ge=0, le=5)
    formality: int = Field(default=2, ge=1, le=5)
    technicality: int = Field(default=4, ge=1, le=5)
    assertiveness: int = Field(default=3, ge=1, le=5)
    verbosity: int = Field(default=3, ge=1, le=5)
    proactivity: int = Field(default=3, ge=1, le=5)
    challenge_level: int = Field(default=3, ge=1, le=5)
    emotional_style: Literal["calm", "warm", "brisk", "neutral"] = "calm"
    reason_for_change: str | None = None


class AssistantProfileOut(BaseModel):
    nickname: str
    owner_preferred_name: str | None = None
    greeting_enabled: bool = True
    live_conversation_id: UUID | None = None
    onboarding_completed_at: datetime | None = None
    dedication_text: str | None = None
    dedication_played_at: datetime | None = None
    training_wheels_started_at: datetime | None = None
    training_wheels_completed_at: datetime | None = None
    tts_voice: str | None = "default"
    hud_layout: dict = Field(default_factory=dict)
    volume_percent: int = 70


class AssistantNameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)


class DedicationSetRequest(BaseModel):
    text: str | None = Field(default=None, max_length=500)
    blob_id: str | None = Field(default=None, max_length=256)


class QuietHoursRequest(BaseModel):
    until: str | None = None
    start: str | None = None
    end: str | None = None


class ProtocolOut(BaseModel):
    key: str
    title: str
    status: str
    detail: str = ""


class ProtocolSheetOut(BaseModel):
    protocols: list[ProtocolOut] = Field(default_factory=list)
    enabled: list[str] = Field(default_factory=list)
    hud: dict = Field(default_factory=dict)


class CalloutOut(BaseModel):
    id: UUID
    text: str
    source: str
    source_item: str | None = None
    spoken: bool
    emergency: bool = False
    hud: dict = Field(default_factory=dict)
    created_at: datetime

    model_config = {"from_attributes": True}


class PersonalityOut(BaseModel):
    id: UUID
    version: int
    is_current: bool
    directness: int
    humor: int
    formality: int
    technicality: int
    assertiveness: int
    verbosity: int
    proactivity: int
    challenge_level: int
    emotional_style: str
    reason_for_change: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RelationshipOut(BaseModel):
    total_interactions: int
    topics: list[str] = Field(default_factory=list)
    corrections: int = 0
    useful_ratings: int = 0
    followed_rate: float | None = None
    challenge_acceptance_rate: float | None = None
    prediction_reviews: int = 0
    prediction_accuracy: float | None = None
    decision_review_rate: float | None = None
    devices: int = 0
    updated_at: datetime | None = None


class ProactiveTuningOut(BaseModel):
    """Derived, inspectable calibration of assertiveness and proactive delivery."""

    challenge_ceiling: int = Field(default=3, ge=0, le=3)
    budget_adjustment: int = Field(default=0, ge=-2, le=2)
    daily_budget: int = Field(default=5, ge=0, le=20)
    proactivity_factor: float = Field(default=1.0, ge=0.0, le=2.0)
    challenge_acceptance_rate: float | None = None
    intervention_appropriate_rate: float | None = None
    useful_rate: float | None = None
    followed_rate: float | None = None
    correction_rate: float | None = None
    prediction_accuracy: float | None = None
    rationale: str


class EvaluationUpdate(BaseModel):
    was_useful: bool | None = None
    followed_recommendation: bool | None = None
    was_correction: bool | None = None
    intervention_appropriate: bool | None = None


class ResponseLogOut(BaseModel):
    id: UUID
    request_text: str
    reply_text: str
    mode: str
    strategy: dict
    provenance_ids: list
    context_tokens: int
    model: str | None
    was_useful: bool | None
    followed_recommendation: bool | None
    was_correction: bool | None
    intervention_appropriate: bool | None
    created_at: datetime

    model_config = {"from_attributes": True}


class SelfEvalAggregate(BaseModel):
    total: int
    useful_rate: float | None = None
    followed_rate: float | None = None
    correction_rate: float | None = None
    intervention_appropriate_rate: float | None = None
    by_mode: dict[str, dict] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# HUD, route briefings, companionship guardrails
# --------------------------------------------------------------------------- #


class HudCardOut(BaseModel):
    schema_version: Literal["ev.hud.card.v1"] = "ev.hud.card.v1"
    generated_at: datetime
    title: str
    body: str
    priority: float = 0.0
    meta: dict = Field(default_factory=dict)


HudAlertTier = Literal["urgent", "useful", "background", "notify", "notify_card", "digest"]


class HudAlertOut(BaseModel):
    """One pending alert rendered as a strict HUD card (ev.hud.alert.v1)."""

    schema_version: Literal["ev.hud.alert.v1"] = "ev.hud.alert.v1"
    generated_at: datetime
    alert_id: UUID
    title: str
    body: str
    priority: float = Field(default=0.0, ge=0.0, le=1.0)
    tier: HudAlertTier = "notify"
    kind: str | None = None
    rationale: str | None = None
    meta: dict = Field(default_factory=dict)


class HudQuickCardOut(BaseModel):
    """Compact tactical quick card (ev.hud.quickcard.v1) for <800 ms HUD reads."""

    schema_version: Literal["ev.hud.quickcard.v1"] = "ev.hud.quickcard.v1"
    generated_at: datetime
    objective: str
    summary: str
    next_action: str | None = None
    top_risk: str | None = None
    people_count: int = Field(default=0, ge=0)
    options_count: int = Field(default=0, ge=0)
    decision_history_count: int = Field(default=0, ge=0)
    meta: dict = Field(default_factory=dict)


class HudLookoutWindow(BaseModel):
    id: str
    kind: str
    size: str
    time_type: str
    placement: str
    title: str
    body: str
    ttl_ms: int | None = None
    items: list[str] = Field(default_factory=list)
    recommendation: str | None = None
    source: str | None = None
    lookout: bool = False
    priority: float = 0.4
    questions: list[str] = Field(default_factory=list)
    response: str | None = None
    layout: str = "stack"
    drift_x: int = 0
    drift_y: int = 0
    tilt: float = 0.0


class HudLookoutOut(BaseModel):
    """Intelligence directive for one or more HUD windows (ev.hud.lookout.v1)."""

    schema_version: Literal["ev.hud.lookout.v1"] = "ev.hud.lookout.v1"
    generated_at: datetime
    open: bool
    windows: list[HudLookoutWindow] = Field(default_factory=list)
    rationale: str = ""
    explicit: bool = False
    needed: bool = False
    meta: dict = Field(default_factory=dict)


class HudOpsCardOut(BaseModel):
    """Unified ops center rendered as a strict HUD card (ev.hud.ops.v1)."""

    schema_version: Literal["ev.hud.ops.v1"] = "ev.hud.ops.v1"
    generated_at: datetime
    title: str = "Ops center"
    summary: str
    focus_locked: bool = False
    online_devices: int = Field(default=0, ge=0)
    pending_alerts: int = Field(default=0, ge=0)
    open_decisions: int = Field(default=0, ge=0)
    command_cards: list[str] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)


class RouteBriefingOut(BaseModel):
    schema_version: Literal["ev.hud.route.v1"] = "ev.hud.route.v1"
    generated_at: datetime
    destination: str | None = None
    leave_by: str | None = None
    travel_time_minutes: int | None = None
    prep_checklist: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class IsolationScanOut(BaseModel):
    detected: bool
    signals: list[dict] = Field(default_factory=list)
    recommendation: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0


# --------------------------------------------------------------------------- #
# Tool orchestration
# --------------------------------------------------------------------------- #


class ToolSpecOut(BaseModel):
    name: str
    description: str
    parameters: dict
    sensitive: bool = False
    read_only: bool = True
    permission: str = "memory:read"
    undoable: bool = False
    output: dict = Field(default_factory=dict)


class ToolCallRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    arguments: dict = Field(default_factory=dict)
    allow_sensitive: bool = False
    request_id: str | None = Field(default=None, max_length=128)
    life_verified: bool = False


class ToolCallResponse(BaseModel):
    name: str
    ok: bool
    result: dict | None = None
    error: str | None = None
    latency_ms: float
    request_id: str | None = None
    actor: str | None = None
    model: str | None = None


class ModelsResponse(BaseModel):
    provider: str
    models: list[str]


# --------------------------------------------------------------------------- #
# 24/7 runtime & devices
# --------------------------------------------------------------------------- #


class RuntimeHeartbeatCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    status: Literal["ok", "degraded", "error"] = "ok"
    listener_state: Literal["listening", "sleep", "off"] = "listening"
    battery_percent: float | None = Field(default=None, ge=0, le=100)
    battery_pct: float | None = Field(default=None, ge=0, le=100)
    storage_free_bytes: int | None = Field(default=None, ge=0)
    storage_free_b: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    details: dict = Field(default_factory=dict)


class RuntimeHeartbeatOut(BaseModel):
    id: UUID
    device_id: UUID
    reported_at: datetime
    status: str
    listener_state: str
    battery_percent: float | None
    storage_free_bytes: int | None = None
    latency_ms: int | None
    details: dict

    model_config = {"from_attributes": True}


class WakeIntent(BaseModel):
    device_id: UUID
    signal_score: float = Field(default=0.5, ge=0, le=1)
    battery_percent: float | None = Field(default=None, ge=0, le=100)
    proximity_score: float | None = Field(default=None, ge=0, le=1)
    priority: float = Field(default=0.5, ge=0, le=1)
    payload: dict = Field(default_factory=dict)
    # Real wake evidence (Agent 3/5 path). The runtime arbitrates from the
    # engine's detection, not from client-supplied signal floats.
    text_hint: str | None = Field(default=None, max_length=256)
    audio_ref: str | None = Field(default=None, max_length=4096)
    frames_b64: str | None = Field(default=None, max_length=8 * 1024 * 1024)
    sample_rate: int = Field(default=16000, ge=8000, le=48000)


class WakeCandidateOut(BaseModel):
    device_id: UUID
    name: str
    score: float = 0.0
    selected: bool = False
    reason: str = ""


class WakeArbitrationOut(BaseModel):
    winner: WakeCandidateOut | None = None
    candidates: list[WakeCandidateOut] = Field(default_factory=list)
    state: str = "idle"
    session_id: UUID | None = None
    blocked: bool = False
    block_reason: str | None = None
    challenge_nonce: str | None = None
    challenge_phrase: str | None = None


class RuntimeTransitionRequest(BaseModel):
    to_state: Literal["verifying", "awake", "processing", "responding", "follow_up", "idle"]
    reason: str | None = None


class RuntimeVerifyRequest(BaseModel):
    session_id: UUID
    nonce: str = Field(min_length=1, max_length=256)
    samples: list[str] = Field(min_length=1, max_length=20)
    phrase: str | None = Field(default=None, max_length=512)
    liveness_proof: str | None = None
    live_score: float | None = Field(default=None, ge=0, le=1)
    audio_sha256: str | None = Field(default=None, max_length=64)


class RuntimeVerifyResponse(BaseModel):
    session_id: UUID
    verified: bool
    state: str
    confidence: float = 0.0
    reason: str = ""


class RuntimeUtteranceRequest(BaseModel):
    session_id: UUID | None = None
    text: str | None = Field(default=None, max_length=200_000)
    audio_b64: str | None = None
    audio_ref: str | None = None
    reverify_token: str | None = None
    language: str = "en"
    conversation_id: UUID | None = None
    follow_up: bool = False


class RuntimeUtteranceResponse(BaseModel):
    session_id: UUID
    state: str
    transcript: str
    transcript_confidence: float = 0.0
    reply: str
    conversation_id: UUID | None = None
    tts: TtsOut | None = None
    tts_device_id: UUID | None = None
    style: SpeechStyleOut | None = None
    model: str | None = None
    context_tokens: int = 0
    memory_deltas: list[MemoryDelta] = Field(default_factory=list)


class RuntimeSessionOut(BaseModel):
    id: UUID
    state: str
    device_id: UUID | None
    wake_signal: float | None
    priority: float
    started_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    end_reason: str | None
    last_heartbeat_at: datetime | None

    model_config = {"from_attributes": True}


class RuntimeDeviceOut(BaseModel):
    device_id: UUID
    name: str
    presence: Literal["online", "away", "unknown"] = "unknown"
    listener_state: str | None = None
    battery_percent: float | None = None
    last_seen_at: datetime | None = None
    last_heartbeat_at: datetime | None = None


class RuntimeStatusOut(BaseModel):
    state: str = "idle"
    session: RuntimeSessionOut | None = None
    devices: list[RuntimeDeviceOut] = Field(default_factory=list)
    online_count: int = 0
    quiet_hours_active: bool = False
    attention: dict = Field(default_factory=dict)
    actions_pending: int = 0
    dead_letters: dict = Field(default_factory=dict)
    generated_at: datetime


class ApprovedActionCreate(BaseModel):
    action_type: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=256)
    payload: dict = Field(default_factory=dict)
    auto_approve: bool = False
    device_id: UUID | None = None


class ApprovedActionOut(BaseModel):
    id: UUID
    action_type: str
    title: str | None
    payload: dict
    status: str
    requires_approval: bool
    requested_by: str | None
    device_id: UUID | None
    session_id: UUID | None
    approved_at: datetime | None
    approved_by: str | None
    denied_at: datetime | None
    denied_reason: str | None
    executed_at: datetime | None
    result: dict | None
    error: str | None
    rolled_back_at: datetime | None
    rolled_back_reason: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ActionSpecOut(BaseModel):
    name: str
    description: str
    payload: dict = Field(default_factory=dict)
    output: dict = Field(default_factory=dict)
    requires_approval: bool = True
    undoable: bool = False
    permission: str
    read_only: bool = True


class ActionDecisionRequest(BaseModel):
    reason: str | None = None
    result: dict = Field(default_factory=dict)


class DeadLetterCreate(BaseModel):
    queue: str = Field(min_length=1, max_length=64)
    job_id: str | None = None
    payload: dict = Field(default_factory=dict)
    error: str = Field(min_length=1)


class DeadLetterOut(BaseModel):
    id: UUID
    queue: str
    job_id: str | None
    payload: dict
    error: str
    attempts: int
    status: str
    last_error_at: datetime
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------- #
# Routines & automations
# --------------------------------------------------------------------------- #


class RoutineCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: Literal["scheduled", "trigger"] = "scheduled"
    schedule: str | None = Field(default=None, max_length=128)
    timezone: str = Field(default="UTC", max_length=64)
    quiet_hours_skip: bool = True
    backfill_max: int = Field(default=1, ge=0, le=30)
    cooldown_seconds: int = Field(default=0, ge=0)
    trigger: dict = Field(default_factory=dict)
    action_type: str = Field(min_length=1, max_length=64)
    action_title: str | None = Field(default=None, max_length=256)
    action_payload: dict = Field(default_factory=dict)
    requires_approval: bool = False
    undoable: bool = False
    metadata: dict = Field(default_factory=dict)


class RoutineUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    kind: Literal["scheduled", "trigger"] | None = None
    enabled: bool | None = None
    schedule: str | None = Field(default=None, max_length=128)
    timezone: str | None = Field(default=None, max_length=64)
    quiet_hours_skip: bool | None = None
    backfill_max: int | None = Field(default=None, ge=0, le=30)
    cooldown_seconds: int | None = Field(default=None, ge=0)
    trigger: dict | None = None
    action_type: str | None = Field(default=None, min_length=1, max_length=64)
    action_title: str | None = Field(default=None, max_length=256)
    action_payload: dict | None = None
    requires_approval: bool | None = None
    undoable: bool | None = None
    metadata: dict | None = None


class RoutineOut(BaseModel):
    id: UUID
    name: str
    kind: str
    enabled: bool
    schedule: str | None
    timezone: str
    quiet_hours_skip: bool
    backfill_max: int
    cooldown_seconds: int
    trigger: dict
    action_type: str
    action_title: str | None
    action_payload: dict
    requires_approval: bool
    undoable: bool
    metadata: dict = Field(validation_alias="metadata_")
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RoutineRunOut(BaseModel):
    id: UUID
    routine_id: UUID
    kind: str
    status: str
    scheduled_for: datetime | None
    triggered_at: datetime | None
    trigger_event_id: UUID | None
    trigger_live_event_id: UUID | None
    trigger_snapshot: dict
    dedupe_key: str
    action_id: UUID | None
    attempts: int
    error: str | None
    result: dict | None
    undoable: bool
    undo_status: str
    undo_payload: dict | None
    rolled_back_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RoutineManualRunRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=256)


class RoutineTickOut(BaseModel):
    now: datetime
    created: int = 0
    skipped: int = 0
    failed: int = 0
    failure_alerts: int = 0
    errors: list[str] = Field(default_factory=list)
    runs: list[RoutineRunOut] = Field(default_factory=list)


class RoutineRunDecisionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=256)
    result: dict = Field(default_factory=dict)


class RoutineTemplateOut(BaseModel):
    slug: str
    name: str
    description: str
    kind: str
    schedule: str | None
    timezone: str
    quiet_hours_skip: bool
    backfill_max: int
    cooldown_seconds: int
    trigger: dict
    action_type: str
    action_title: str | None
    action_payload: dict
    requires_approval: bool
    undoable: bool
    tags: list[str]
    personalization_hints: str | None


class RoutineTemplateInstantiateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    overrides: dict = Field(default_factory=dict)


class RoutineOverviewOut(BaseModel):
    routines_total: int = 0
    routines_enabled: int = 0
    routines_scheduled: int = 0
    routines_trigger: int = 0
    runs_total: int = 0
    runs_last_24h: int = 0
    runs_failed_last_24h: int = 0
    awaiting_approval: int = 0
    pending_failure_alerts: int = 0
    latest_error: str | None = None
    generated_at: datetime


# --------------------------------------------------------------------------- #
# Training & personalization — consent lifecycle and voice enrollment
# --------------------------------------------------------------------------- #

TrainingTrack = Literal[
    "voice_enrollment",
    "face_enrollment",  # AGENT 7 ROSTER — consented face templates
    "training_corpus",
    "life_data_personalization",
    "adapter_fine_tuning",
    "filter_self_improvement",
    "chat_egress",  # AGENT 19 VAULT — consent track for remote chat egress
]


class ConsentGrant(BaseModel):
    track: TrainingTrack
    purpose: str = "personalize EV to the owner"
    scope: dict = Field(default_factory=dict)
    source: str = "privacy_center"
    consent_version: str = "1.0"


class ConsentOut(BaseModel):
    id: UUID
    track: str
    granted_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None
    consent_version: str
    purpose: str
    scope: dict
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ConsentRevoke(BaseModel):
    reason: str = "user revoked"


class VoiceSampleIn(BaseModel):
    text: str | None = Field(default=None, max_length=512)
    features: list[float] | None = None
    audio_b64: str | None = None
    liveness_proof: Literal["live", "replay", "synthetic", "converted"] | None = None
    live_score: float | None = Field(default=None, ge=0.0, le=1.0)


class VoiceEnrollmentCreate(BaseModel):
    samples: list[VoiceSampleIn] = Field(min_length=5, max_length=20)
    reason: str | None = Field(default=None, max_length=512)


class VoiceEnrollmentDetailOut(BaseModel):
    id: UUID
    version: int
    is_current: bool
    algorithm: str
    embedding_dim: int
    threshold: float
    sample_count: int
    status: str
    privacy_level: str
    consent_id: UUID | None = None
    supersedes_id: UUID | None = None
    superseded_by_id: UUID | None = None
    reason_for_change: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class VoiceEnrollResponse(BaseModel):
    enrollment: VoiceEnrollmentDetailOut
    sample_count: int
    raw_samples_stored: bool = False


class VoiceVerifyRequest(BaseModel):
    """Base64-encoded audio samples for stateless speaker verification."""

    samples: list[str] = Field(min_length=1, max_length=20)


class TrainingVoiceEnrollRequest(BaseModel):
    """Base64 audio samples plus shared liveness evidence for training enroll."""

    samples: list[str] = Field(min_length=5, max_length=20)
    liveness_proof: Literal["live", "replay", "synthetic", "converted"] | None = None
    live_score: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str | None = Field(default=None, max_length=512)


class VoiceVerifyResponse(BaseModel):
    accepted: bool
    score: float
    threshold: float
    enrollment_id: UUID | None = None
    version: int | None = None
    reason: str = "ok"


class VoiceRollbackRequest(BaseModel):
    enrollment_id: UUID | None = None
    target_version: int = Field(ge=1)
    reason: str = "rollback re-enrollment"


class VoiceRevokeRequest(BaseModel):
    enrollment_id: UUID | None = None
    reason: str = "user revoked voice enrollment"


class VoiceDeleteRequest(BaseModel):
    enrollment_id: UUID | None = None
    reason: str = "user deleted biometric data"


class VoicePrintExportOut(BaseModel):
    """Export view of one encrypted voiceprint (decrypted for portability/restore)."""

    id: str
    version: int
    algorithm: str
    embedding_dim: int
    threshold: float
    sample_count: int
    embedding: list[float] | None = None
    is_current: bool
    supersedes_id: str | None = None
    reason_for_change: str | None = None
    created_at: str


class VoiceExportOut(BaseModel):
    exported_at: datetime
    consents: list[ConsentOut] = Field(default_factory=list)
    enrollments: list[VoiceEnrollmentDetailOut] = Field(default_factory=list)
    voiceprints: list[VoicePrintExportOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Integrations & ecosystem
# --------------------------------------------------------------------------- #


class IntegrationCatalogItem(BaseModel):
    adapter: str
    name: str
    description: str
    capabilities: list[str]
    default_scopes: list[str]
    min_privacy: str
    privacy_kind: str
    event_types: list[str]
    actions: list[dict]


class IntegrationCreate(BaseModel):
    adapter: str = Field(min_length=1, max_length=64)
    slug: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    name: str = Field(min_length=1, max_length=128)
    scopes: list[str] = Field(min_length=1)
    privacy_level: PrivacyLevel = "normal"
    config: dict = Field(default_factory=dict)


class IntegrationOut(BaseModel):
    id: UUID
    slug: str
    adapter: str
    name: str
    scopes: list
    status: str
    privacy_level: str
    config: dict
    live_channel_id: UUID | None = None
    credential_configured: bool = False
    webhook_configured: bool = False
    last_used_at: datetime | None = None
    last_webhook_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class IntegrationCredentialCreate(BaseModel):
    access_token: str = Field(min_length=8)
    refresh_token: str | None = None
    token_type: str = "Bearer"
    provider_account_id: str | None = Field(default=None, max_length=256)
    scopes: list[str] | None = None
    expires_at: datetime | None = None


class IntegrationCredentialOut(BaseModel):
    kind: str
    configured: bool
    scopes: list = Field(default_factory=list)
    provider_account_id: str | None = None
    token_fingerprint_prefix: str | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WebhookSecretOut(BaseModel):
    configured: bool
    rotated_at: datetime | None = None
    # Returned exactly once, on creation/rotation; never re-served later.
    secret: str | None = None


class IntegrationScopeUpdate(BaseModel):
    scopes: list[str] = Field(min_length=1)


class VaultRotateRequest(BaseModel):
    new_key: str = Field(min_length=16, max_length=512)


class VaultRotateOut(BaseModel):
    rotated: bool
    reencrypted_credentials: int


class IntegrationActionRequest(BaseModel):
    action: str = Field(min_length=1, max_length=64)
    args: dict = Field(default_factory=dict)


class IntegrationActionOut(BaseModel):
    adapter: str
    action: str
    result: dict
    executed_at: datetime


class WebhookIngestOut(BaseModel):
    integration_id: UUID
    adapter: str
    accepted: int
    deduplicated: int
    channel_id: UUID | None = None
    event_ids: list[UUID] = Field(default_factory=list)


class PluginManifest(BaseModel):
    schema_version: Literal["ev.plugin.v1"] = "ev.plugin.v1"
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    version: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=512)
    permissions: list[str] = Field(default_factory=list)
    commands: list[dict] = Field(min_length=1, max_length=50)


class PluginOut(BaseModel):
    id: UUID
    slug: str
    name: str
    version: str
    status: str
    permissions: list
    checksum: str
    manifest: dict
    approved_at: datetime | None = None
    approved_by: str | None = None
    rejected_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PluginCommandRequest(BaseModel):
    args: dict = Field(default_factory=dict)


class PluginRejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=256)


class PluginCommandOut(BaseModel):
    plugin_id: UUID
    plugin: str
    command: str
    result: dict
    emitted_events: list[LiveEventOut] = Field(default_factory=list)
    executed_at: datetime


# --------------------------------------------------------------------------- #
# Training & personalization — life-data importance/retrieval calibration
# --------------------------------------------------------------------------- #


class PersonalizationCalibrationOut(BaseModel):
    id: UUID
    version: int
    is_current: bool
    calibrations: dict[str, float]
    evidence: dict
    reason_for_change: str
    consent_id: UUID | None = None
    supersedes_id: UUID | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PersonalizationCalibrateResponse(BaseModel):
    calibration: PersonalizationCalibrationOut
    evidence: dict
    applied: bool


class PersonalizationRollbackRequest(BaseModel):
    target_version: int = Field(ge=1)
    reason: str = "rollback personalization calibration"


class PersonalizationDeleteResponse(BaseModel):
    deleted: int
    applied: bool = False


# --------------------------------------------------------------------------- #
# Training & personalization — consent-gated corpus harvesting
# --------------------------------------------------------------------------- #


class TrainingCorpusEntryOut(BaseModel):
    kind: str
    role: Literal["user", "assistant", "system"]
    text: str
    source: str
    signals: dict = Field(default_factory=dict)
    hash: str


class TrainingCorpusSnapshotOut(BaseModel):
    id: UUID
    version: int
    is_current: bool
    name: str
    entry_count: int
    source_counts: dict
    content_hash: str | None
    reason_for_change: str
    consent_id: UUID | None = None
    supersedes_id: UUID | None = None
    redacted: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class TrainingCorpusBuildResponse(BaseModel):
    snapshot: TrainingCorpusSnapshotOut
    entry_count: int
    excluded_never_send_to_model: int


class TrainingCorpusRollbackRequest(BaseModel):
    target_version: int = Field(ge=1)
    reason: str = "rollback training corpus snapshot"


class TrainingCorpusDeleteResponse(BaseModel):
    deleted: int
    redacted: bool = True


class TrainingCorpusExportOut(BaseModel):
    snapshot: TrainingCorpusSnapshotOut
    entries: list[TrainingCorpusEntryOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Training & personalization — filter self-improvement from the ledger
# --------------------------------------------------------------------------- #


class FilterThresholdProposalOut(BaseModel):
    name: str
    direction: Literal["increase", "decrease", "keep"]
    current_value: float | None = None
    target_value: float | None = None
    rationale: str
    evidence: dict = Field(default_factory=dict)


class FilterRecalibrationOut(BaseModel):
    id: UUID
    version: int
    is_current: bool
    metrics: dict
    proposals: list[FilterThresholdProposalOut] = Field(default_factory=list)
    policy: dict = Field(default_factory=dict)
    applied_at: datetime | None = None
    applied_by: str | None = None
    reason_for_change: str
    consent_id: UUID | None = None
    supersedes_id: UUID | None = None
    redacted: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class FilterRecalibrationBuildResponse(BaseModel):
    recalibration: FilterRecalibrationOut
    proposals: list[FilterThresholdProposalOut] = Field(default_factory=list)
    applied: bool = False


class FilterRecalibrationRollbackRequest(BaseModel):
    target_version: int = Field(ge=1)
    reason: str = "rollback filter recalibration"


class FilterRecalibrationApplyRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=512)


class FilterRecalibrationDeleteResponse(BaseModel):
    deleted: int
    redacted: bool = True


# --------------------------------------------------------------------------- #
# Training & personalization — versioned adapter registry (LoRA path)
# --------------------------------------------------------------------------- #


class AdapterRegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    provider: str = Field(default="local-lora", min_length=1, max_length=64)
    base_model: str | None = Field(default=None, max_length=128)
    adapter_ref: str | None = Field(default=None, max_length=512)
    corpus_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=512)


class AdapterDryRunRequest(BaseModel):
    corpus_version: int = Field(ge=1)
    provider: str = Field(default="local-lora", min_length=1, max_length=64)
    base_model: str | None = Field(default=None, max_length=128)
    adapter_ref: str | None = Field(default=None, max_length=512)


class AdapterTrainRequest(AdapterDryRunRequest):
    adapter_id: UUID | None = None
    cost_approved: bool = False
    reason: str | None = Field(default=None, max_length=512)


class AdapterRunOut(BaseModel):
    mode: Literal["dry_run", "train"]
    provider: str
    corpus_version: int
    passed: bool
    gates: dict = Field(default_factory=dict)
    dataset: dict = Field(default_factory=dict)
    plan: dict | None = None
    result: dict | None = None


class AdapterOut(BaseModel):
    id: UUID
    name: str
    version: int
    is_current: bool
    status: str
    provider: str
    base_model: str | None = None
    adapter_ref: str | None = None
    corpus_snapshot_id: UUID | None = None
    eval_metrics: dict = Field(default_factory=dict)
    reason_for_change: str | None = None
    supersedes_id: UUID | None = None
    redacted: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AdapterActivateRequest(BaseModel):
    adapter_id: UUID
    reason: str = "activate adapter"


class AdapterRollbackRequest(BaseModel):
    adapter_id: UUID
    reason: str = "rollback adapter"


class AdapterDeleteResponse(BaseModel):
    deleted: int
    redacted: bool = True


# ============================================================================
# SHARED APPEND-ONLY SECTION — docs/FLEET_LAW.md §3
# Additive only. Append inside YOUR block; never modify, reorder, reformat, or
# delete another agent's lines, schemas, or endpoint contracts.
#
# --- AGENT 1 CONDUCTOR ---
# Reserved by Agent 1 (Conductor): fleet governance, integration, contract.
#
# --- AGENT 2 FOUNDRY ---
# --- AGENT 3 EARS ---
# Ears-to-runtime audio delivery contract (docs/AUDIO.md). The ears process
# posts VAD-segmented, wake-passing utterances here; Agent 4 wires the
# /v1/ears/wake endpoint (dependency note in the Agent 3 report).
class EarsWakeRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    frames_b64: str | None = Field(default=None, max_length=8 * 1024 * 1024)
    audio_ref: str | None = None
    text_hint: str | None = Field(default=None, max_length=256)
    wake_confidence: float = Field(default=0.0, ge=0, le=1)
    scene: str | None = Field(default=None, max_length=32)
    scene_confidence: float | None = Field(default=None, ge=0, le=1)
    consent: bool = False
    # A local spotter may hear both the wake word and a command. In that case
    # return the listen ack immediately and stream the command separately.
    defer_command: bool = False


class EarsWakeResponse(BaseModel):
    accepted: bool
    message: str | None = None
    session_id: UUID | None = None
    state: str | None = None
    listening: bool = False
    queued: bool = False
    transcript: str | None = None
    reply: str | None = None
    tts: TtsOut | None = None
    playback_owner: str = "ears"
    command_deferred: bool = False


# --- AGENT 4 VOICE ---
# --- AGENT 5 SENTRY ---
# --- AGENT 6 EYES ---
# --- AGENT 7 ROSTER ---
# --- AGENT 8 SYNAPSE ---
# --- AGENT 9 MNEMO ---
# --- AGENT 10 CORTEX ---
# --- AGENT 11 FORGE ---
# --- AGENT 12 CONDUIT ---
class OAuthAuthorizeOut(BaseModel):
    authorize_url: str
    state: str
    expires_at: datetime


class OAuthStatusOut(BaseModel):
    provider: str | None = None
    authorized: bool = False
    configured: bool = False
    expires_at: datetime | None = None
    expired: bool = False
    reauth_required: bool = True
    provider_account_id: str | None = None
    scopes: list = Field(default_factory=list)


class IntegrationSyncOut(BaseModel):
    integration_id: UUID
    adapter: str
    synced_at: datetime
    accepted: int = 0
    deduplicated: int = 0
    event_count: int = 0
    signals: dict = Field(default_factory=dict)


# --- AGENT 13 AMBIENT ---
# --- AGENT 14 PULSE ----------------------------------------------------------


class NotificationCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=4000)
    priority: float = Field(default=0.5, ge=0.0, le=1.0)
    tier: Literal["urgent", "useful", "background", "notify", "notify_card"] = "useful"
    kind: str = Field(default="manual", min_length=1, max_length=32)
    source: str | None = Field(default=None, max_length=64)
    emergency: bool = False


class NotificationOut(BaseModel):
    id: UUID
    kind: str
    title: str
    body: str
    priority: float
    tier: str
    source: str | None
    fingerprint: str
    status: str
    reason: str | None
    backend: str | None
    backend_ref: str | None
    alert_id: UUID | None
    action_id: UUID | None
    device_id: UUID | None
    attention_kind: str
    attempt_count: int
    queued_at: datetime
    last_attempt_at: datetime | None
    delivered_at: datetime | None
    details: dict

    model_config = {"from_attributes": True}


class PushTokenRegister(BaseModel):
    token: str = Field(min_length=1, max_length=4096)
    platform: Literal["apns", "fcm", "webpush"] = "apns"
    bundle_id: str | None = Field(default=None, max_length=256)


class PushTokenOut(BaseModel):
    device_id: UUID
    platform: str | None
    registered: bool
    updated_at: datetime | None


class LifeJobOut(BaseModel):
    id: UUID
    action: str
    device_id: UUID | None
    status: str
    lifecycle: str
    args: dict
    result: dict | None
    evidence: dict | None
    error: str | None
    created_at: datetime
    dispatched_at: datetime | None
    acknowledged_at: datetime | None
    delivered_at: datetime | None

    model_config = {"from_attributes": True}


class NotifyStatusOut(BaseModel):
    backend: str
    available: bool
    reason: str | None = None
    permission: str | None = None
    delivered_today: int = 0
    suppressed_today: int = 0
    failed_today: int = 0


# --- AGENT 15 ORACLE ---
# --- AGENT 16 CONSCIENCE ---
# --- AGENT 17 WORKBENCH ---
# --- AGENT 18 SUIT ---
# --- AGENT 19 VAULT ----------------------------------------------------------


class WebauthnRegisterOptionsOut(BaseModel):
    challenge_id: UUID
    challenge: str  # base64url, no padding
    rp: dict
    user: dict
    pub_key_cred_params: list[dict]
    timeout: int
    attestation: str
    authenticator_selection: dict


class WebauthnRegisterVerifyRequest(BaseModel):
    challenge_id: UUID
    credential_id: str = Field(min_length=1, max_length=1024)  # base64url
    client_data_json: str = Field(min_length=1, max_length=16384)  # base64url
    attestation_object: str = Field(min_length=1, max_length=65536)  # base64url
    name: str = Field(min_length=1, max_length=128)
    device_id: UUID | None = None


class WebauthnAuthOptionsOut(BaseModel):
    challenge_id: UUID
    challenge: str  # base64url, no padding
    rp_id: str
    timeout: int
    user_verification: str


class WebauthnAuthVerifyRequest(BaseModel):
    challenge_id: UUID
    credential_id: str = Field(min_length=1, max_length=1024)  # base64url
    client_data_json: str = Field(min_length=1, max_length=16384)  # base64url
    authenticator_data: str = Field(min_length=1, max_length=16384)  # base64url
    signature: str = Field(min_length=1, max_length=16384)  # base64url
    device_name: str = Field(min_length=1, max_length=128)
    capabilities: list[str] = Field(default_factory=list)


class WebauthnAuthResponse(BaseModel):
    verified: bool = True
    token: str
    device: DeviceOut
    owner_id: UUID
    trust_level: str


# --- AGENT 20 LAUNCH ---
# ============================================================================


# --------------------------------------------------------------------------- #
# --- AGENT 9 MNEMO (memory extraction, entities, rollups) ---
# --------------------------------------------------------------------------- #


class EntityMergeRequest(BaseModel):
    target_entity_id: UUID
    absorbed_entity_id: UUID
    reason: str = Field(default="user confirmed merge", min_length=1, max_length=512)


class StateOfMeOut(BaseModel):
    period_start: datetime
    period_end: datetime
    executed_at: datetime
    written: list[UUID]


# --------------------------------------------------------------------------- #
# --- AGENT 12 CONDUIT (WAVE LIFE) — Apple life bridges (additive) ---
# --------------------------------------------------------------------------- #


class LifePolicyOut(BaseModel):
    allowed: bool
    confirmation_required: bool
    reason: str
    contact: dict | None = None


class LifeDeviceResultIn(BaseModel):
    queue_id: UUID | None = None
    action: str | None = Field(default=None, max_length=64)
    status: Literal["delivered", "failed"]
    evidence: dict = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=512)
    message: dict | None = None


class LifeDeviceResultOut(BaseModel):
    accepted: bool
    queue_id: UUID | None = None
    status: str | None = None
    delivery: dict = Field(default_factory=dict)


class LifeOutboxEntryOut(BaseModel):
    id: UUID
    action: str
    args: dict
    created_at: datetime


class LifeOutboxOut(BaseModel):
    items: list[LifeOutboxEntryOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Workbench: telemetry, location shares, beacons, lookout utterance
# --------------------------------------------------------------------------- #


class TelemetrySessionCreate(BaseModel):
    label: str = Field(default="test", min_length=1, max_length=128)


class TelemetrySessionOut(BaseModel):
    id: UUID
    label: str
    status: str
    started_at: datetime
    ended_at: datetime | None = None

    model_config = {"from_attributes": True}


class TelemetrySampleCreate(BaseModel):
    source: Literal["drone", "vehicle", "phone"]
    battery: float | None = Field(default=None, ge=0, le=100)
    alt: float | None = None
    speed: float | None = None
    lat: float | None = None
    lon: float | None = None
    session_id: UUID | None = None
    details: dict = Field(default_factory=dict)


class TelemetrySampleOut(BaseModel):
    id: UUID
    session_id: UUID | None
    source: str
    battery: float | None
    alt: float | None
    speed: float | None
    lat: float | None
    lon: float | None
    reported_at: datetime
    details: dict

    model_config = {"from_attributes": True}


class LocationShareCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    last_lat: float
    last_lon: float
    token_expires: datetime | None = None
    source: str = "consent"
    owner_family_device: bool = False


class LocationShareOut(BaseModel):
    id: UUID
    person_name: str
    last_lat: float | None
    last_lon: float | None
    token_expires: datetime
    source: str

    model_config = {"from_attributes": True}


class BeaconCreate(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    kind: Literal["findmy", "ble", "ev_device"] = "ev_device"
    last_lat: float | None = None
    last_lon: float | None = None
    details: dict = Field(default_factory=dict)


class BeaconOut(BaseModel):
    id: UUID
    label: str
    kind: str
    last_lat: float | None
    last_lon: float | None
    owner_only: bool = True
    last_seen_at: datetime | None = None

    model_config = {"from_attributes": True}


class LookoutUtteranceIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    conversation_id: UUID | None = None
    prefer_haptic: bool = True


class OwnerCameraCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    vault_ref: str | None = Field(default=None, max_length=256)
    kind: str = "upload"
    clip_attachment_id: UUID | None = None


class PublicFeedCreate(BaseModel):
    kind: str = Field(default="rss", max_length=32)
    url: str = Field(min_length=1, max_length=1024)
    label: str = Field(min_length=1, max_length=256)
    items: list[dict] = Field(default_factory=list)


# --- PRESENCE (EVIE overlay) ---


class PresenceShowIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=4000)
    kind: str = Field(default="card", max_length=32)
    size: str | None = Field(default=None, max_length=16)
    time_type: str | None = Field(default=None, max_length=16)
    placement: str | None = Field(default=None, max_length=16)
    ttl_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    items: list[str] = Field(default_factory=list)
    recommendation: str | None = Field(default=None, max_length=400)
    source: str | None = Field(default=None, max_length=160)
    window_id: str | None = Field(default=None, max_length=64)
    lookout: bool | None = None
    auto: bool = False
    message: str | None = Field(default=None, max_length=2000)
    windows: list[dict] = Field(default_factory=list)
    lat: float | None = None
    lon: float | None = None
    dest_lat: float | None = None
    dest_lon: float | None = None
    questions: list[str] = Field(default_factory=list)
    response: str | None = Field(default=None, max_length=4000)
    layout: str | None = Field(default=None, max_length=16)


class PresenceShowOut(BaseModel):
    ok: bool
    opened: bool
    surface: str = "overlay"
    url: str | None = None
    via: str | None = None
    degraded: bool = False
    reason: str | None = None
    next_step: str | None = None
    windows: list[dict] = Field(default_factory=list)
    plan: dict | None = None


class LookoutComposeIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    title: str | None = Field(default=None, max_length=120)
    body: str | None = Field(default=None, max_length=4000)
    open: bool = True
    explicit: bool = True


class LookoutListOut(BaseModel):
    windows: list[dict] = Field(default_factory=list)


class LookoutDismissIn(BaseModel):
    window_id: str | None = Field(default=None, max_length=64)


class SurfaceRateIn(BaseModel):
    kind: str = Field(min_length=1, max_length=32)
    useful: bool
    message: str | None = Field(default=None, max_length=500)
    preferred_kind: str | None = Field(default=None, max_length=32)
    window_id: str | None = Field(default=None, max_length=64)


class SurfaceCalibrateOut(BaseModel):
    version: int
    urgency_threshold: float
    max_windows: int
    boost_kinds: dict = Field(default_factory=dict)
    suppress_kinds: dict = Field(default_factory=dict)
    evidence: dict = Field(default_factory=dict)
    smoke: dict | None = None
    updated_at: str | None = None
