from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

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
    metadata: dict = Field(validation_alias="metadata_")
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
    memory_id: UUID
    text: str
    memory_type: str
    score: float
    components: dict[str, float]


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


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


class TimelineResponse(BaseModel):
    events: list[EventOut]
    next_cursor: datetime | None = None


class MemoryListResponse(BaseModel):
    memories: list[MemoryOut]
    total: int


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
    conflicts: list[ConflictOut]


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


# --------------------------------------------------------------------------- #
# Devices & attachments
# --------------------------------------------------------------------------- #


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    capabilities: list[str] = Field(default_factory=list)


class DeviceOut(BaseModel):
    id: UUID
    name: str
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None
    capabilities: list

    model_config = {"from_attributes": True}


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
    error: str | None
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


# --------------------------------------------------------------------------- #
# Voice & speech (EVIE)
# --------------------------------------------------------------------------- #


class VoiceWakeRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    wake_word: str = Field(default="evie", min_length=1, max_length=32)
    text_hint: str | None = Field(default=None, max_length=256)
    audio_ref: str | None = None
    wake_confidence: float | None = Field(default=None, ge=0, le=1)


class VoiceWakeResponse(BaseModel):
    session_id: UUID | None = None
    state: str
    owner_enrolled: bool
    challenge_nonce: str | None = None
    challenge_phrase: str | None = None
    message: str | None = None


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
    content_type: str | None = None
    ssml: str | None = None
    duration_ms: int | None = None


class VoiceUtteranceRequest(BaseModel):
    session_id: UUID
    text: str | None = Field(default=None, max_length=200_000)
    audio_ref: str | None = None
    language: str = "en"
    conversation_id: UUID | None = None
    follow_up: bool = False


class VoiceUtteranceResponse(BaseModel):
    session_id: UUID
    state: str
    transcript: str
    transcript_confidence: float = 0.0
    reply: str
    conversation_id: UUID | None = None
    tts: TtsOut | None = None
    style: SpeechStyleOut | None = None
    model: str | None = None
    context_tokens: int = 0
    memory_deltas: list[MemoryDelta] = Field(default_factory=list)


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
    recovery_locked: bool = False


class RecoveryRedeemRequest(BaseModel):
    code: str = Field(min_length=8, max_length=256)
    device_name: str = Field(min_length=1, max_length=128)
    capabilities: list[str] = Field(default_factory=list)


class RecoveryRedeemResponse(BaseModel):
    device: DeviceOut
    token: str
    owner_id: UUID


class ReverificationRequest(BaseModel):
    purpose: Literal["memory.delete", "voice.revoke", "voice.delete", "recovery.rotate"]
    voice_session_id: UUID | None = None


class ReverificationResponse(BaseModel):
    token: str
    purpose: str
    expires_at: str


class ReverificationConsumeRequest(BaseModel):
    token: str = Field(min_length=8, max_length=256)
    purpose: Literal["memory.delete", "voice.revoke", "voice.delete", "recovery.rotate"]


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
    related_memories: list[dict] = Field(default_factory=list)
    total_events: int = 0


class UserStateOut(BaseModel):
    activity: str | None = None
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
    device_id: UUID
    status: Literal["ok", "degraded", "error"] = "ok"
    listener_state: Literal["listening", "sleep", "off"] = "listening"
    battery_percent: float | None = Field(default=None, ge=0, le=100)
    latency_ms: int | None = Field(default=None, ge=0)
    details: dict = Field(default_factory=dict)


class RuntimeHeartbeatOut(BaseModel):
    id: UUID
    device_id: UUID
    reported_at: datetime
    status: str
    listener_state: str
    battery_percent: float | None
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


class RuntimeTransitionRequest(BaseModel):
    to_state: Literal["verifying", "awake", "processing", "responding", "follow_up", "idle"]
    reason: str | None = None


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
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


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
    errors: list[str] = Field(default_factory=list)
    runs: list[RoutineRunOut] = Field(default_factory=list)


class RoutineRunDecisionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=256)
    result: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Training & personalization — consent lifecycle and voice enrollment
# --------------------------------------------------------------------------- #

TrainingTrack = Literal[
    "voice_enrollment",
    "training_corpus",
    "life_data_personalization",
    "adapter_fine_tuning",
    "filter_self_improvement",
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
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    redacted: bool = False
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
