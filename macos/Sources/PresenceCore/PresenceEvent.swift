import Foundation

/// Typed `evie-presence/1` contract events (`docs/contracts/PRESENCE_EVENTS.md`).
///
/// Additive-tolerant by construction: unknown event types decode to
/// `.unknown` (never throw), unknown `data` keys are ignored, and every
/// payload field the contract marks optional-or-later has a default.
public enum PresenceContract {
    public static let version = "evie-presence/1"
}

/// Minimal JSON scalar bag for free-form `evidence` objects.
public enum PresenceJSON: Sendable, Equatable, Codable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null
    case array([PresenceJSON])
    case object([String: PresenceJSON])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null; return }
        if let b = try? c.decode(Bool.self) { self = .bool(b); return }
        if let n = try? c.decode(Double.self) { self = .number(n); return }
        if let s = try? c.decode(String.self) { self = .string(s); return }
        if let a = try? c.decode([PresenceJSON].self) { self = .array(a); return }
        self = .object((try? c.decode([String: PresenceJSON].self)) ?? [:])
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): try c.encode(n)
        case .bool(let b): try c.encode(b)
        case .null: try c.encodeNil()
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    public var stringValue: String? {
        if case .string(let s) = self { return s }
        return nil
    }
}

extension KeyedDecodingContainer {
    /// Additive-tolerant decode: absent keys fall back to the default
    /// instead of throwing, so minimal-but-valid contract payloads decode.
    func decode<T: Decodable>(_ type: T.Type, forKey key: Key, default value: T) -> T {
        (try? decodeIfPresent(T.self, forKey: key)) ?? value
    }
}

// MARK: - Payloads

public struct StreamHello: Sendable, Equatable, Codable {
    public var contract: String
    public var serverTime: String
    public var cursor: String
    public var resumed: Bool
    public var gap: Bool
    public var restarted: Bool
    public var capabilities: [String]
    public init(
        contract: String = PresenceContract.version, serverTime: String = "",
        cursor: String = "", resumed: Bool = false, gap: Bool = false,
        restarted: Bool = false, capabilities: [String] = []
    ) {
        self.contract = contract; self.serverTime = serverTime; self.cursor = cursor
        self.resumed = resumed; self.gap = gap; self.restarted = restarted
        self.capabilities = capabilities
    }
    enum CodingKeys: String, CodingKey {
        case contract, cursor, resumed, gap, restarted, capabilities
        case serverTime = "server_time"
    }
}

public struct TurnStarted: Sendable, Equatable, Codable {
    public var source: String
    public var utterancePreview: String
    public var label: String
    public init(source: String = "", utterancePreview: String = "", label: String = "") {
        self.source = source; self.utterancePreview = utterancePreview; self.label = label
    }
    enum CodingKeys: String, CodingKey {
        case source, label
        case utterancePreview = "utterance_preview"
    }
}

public struct TurnListening: Sendable, Equatable, Codable {
    public var level: Double
    public var degraded: Bool
    public init(level: Double = 0, degraded: Bool = false) {
        self.level = level; self.degraded = degraded
    }
}

public struct TranscriptPartial: Sendable, Equatable, Codable {
    public var text: String
    public var sequence: Int
    public var stable: Bool
    public var confidence: Double
    public init(text: String = "", sequence: Int = 0, stable: Bool = false, confidence: Double = 0) {
        self.text = text; self.sequence = sequence; self.stable = stable; self.confidence = confidence
    }
}

public struct TranscriptFinal: Sendable, Equatable, Codable {
    public var text: String
    public var confidence: Double
    public var provider: String?
    public init(text: String = "", confidence: Double = 0, provider: String? = nil) {
        self.text = text; self.confidence = confidence; self.provider = provider
    }
}

public struct TurnThinking: Sendable, Equatable, Codable {
    public var label: String
    public var elapsedMs: Int
    public init(label: String = "", elapsedMs: Int = 0) {
        self.label = label; self.elapsedMs = elapsedMs
    }
    enum CodingKeys: String, CodingKey {
        case label
        case elapsedMs = "elapsed_ms"
    }
}

public enum PlanStepStatus: Sendable, Equatable, Codable {
    case queued, active, done, failed, skipped
    case other(String)

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        switch try c.decode(String.self) {
        case "queued": self = .queued
        case "active": self = .active
        case "done": self = .done
        case "failed": self = .failed
        case "skipped": self = .skipped
        case let s: self = .other(s)
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .queued: try c.encode("queued")
        case .active: try c.encode("active")
        case .done: try c.encode("done")
        case .failed: try c.encode("failed")
        case .skipped: try c.encode("skipped")
        case .other(let s): try c.encode(s)
        }
    }
}

public struct PlanStep: Sendable, Equatable, Codable {
    public var id: String
    public var label: String
    public var status: PlanStepStatus
    public init(id: String = "", label: String = "", status: PlanStepStatus = .queued) {
        self.id = id; self.label = label; self.status = status
    }
}

public struct PlanUpdated: Sendable, Equatable, Codable {
    public var label: String
    public var steps: [PlanStep]
    public init(label: String = "", steps: [PlanStep] = []) {
        self.label = label; self.steps = steps
    }
}

public struct UndoInfo: Sendable, Equatable, Codable {
    public var kind: String
    public var label: String
    public var entryId: String?
    public init(kind: String = "none", label: String = "", entryId: String? = nil) {
        self.kind = kind; self.label = label; self.entryId = entryId
    }
    public var isUndoable: Bool { kind != "none" }
    enum CodingKeys: String, CodingKey {
        case kind, label
        case entryId = "entry_id"
    }
}

public struct ToolStarted: Sendable, Equatable, Codable {
    public var callId: String
    public var stepId: String?
    public var name: String
    public var label: String
    public var target: String?
    public init(callId: String = "", stepId: String? = nil, name: String = "", label: String = "", target: String? = nil) {
        self.callId = callId; self.stepId = stepId; self.name = name; self.label = label; self.target = target
    }
    enum CodingKeys: String, CodingKey {
        case name, label, target
        case callId = "call_id"
        case stepId = "step_id"
    }
}

public struct ToolFinished: Sendable, Equatable, Codable {
    public var callId: String
    public var stepId: String?
    public var name: String
    public var label: String
    public var ok: Bool
    public var verified: Bool
    public var evidence: [String: PresenceJSON]
    public var error: String?
    public var receiptId: String?
    public var undo: UndoInfo
    public init(
        callId: String = "", stepId: String? = nil, name: String = "", label: String = "",
        ok: Bool = false, verified: Bool = false, evidence: [String: PresenceJSON] = [:],
        error: String? = nil, receiptId: String? = nil, undo: UndoInfo = .init()
    ) {
        self.callId = callId; self.stepId = stepId; self.name = name; self.label = label
        self.ok = ok; self.verified = verified; self.evidence = evidence
        self.error = error; self.receiptId = receiptId; self.undo = undo
    }
    enum CodingKeys: String, CodingKey {
        case name, label, ok, verified, evidence, error, undo
        case callId = "call_id"
        case stepId = "step_id"
        case receiptId = "receipt_id"
    }
}

public struct LookingAt: Sendable, Equatable, Codable {
    public var app: String
    public var window: String
    public var bundleId: String
    public init(app: String = "", window: String = "", bundleId: String = "") {
        self.app = app; self.window = window; self.bundleId = bundleId
    }
    public var isEmpty: Bool { app.isEmpty && window.isEmpty }
    enum CodingKeys: String, CodingKey {
        case app, window
        case bundleId = "bundle_id"
    }
}

public struct ConfirmRecipient: Sendable, Equatable, Codable {
    public var name: String
    public var address: String
    public var addressKind: String
    public var firstContact: Bool
    public init(name: String = "", address: String = "", addressKind: String = "", firstContact: Bool = false) {
        self.name = name; self.address = address; self.addressKind = addressKind; self.firstContact = firstContact
    }
    enum CodingKeys: String, CodingKey {
        case name, address
        case addressKind = "address_kind"
        case firstContact = "first_contact"
    }
}

public struct ConfirmDiff: Sendable, Equatable, Codable {
    public var kind: String
    public var body: String
    public var truncated: Bool
    public init(kind: String = "", body: String = "", truncated: Bool = false) {
        self.kind = kind; self.body = body; self.truncated = truncated
    }
}

public struct ConfirmEndpoint: Sendable, Equatable, Codable {
    public var method: String
    public var path: String
    public var needsReverify: Bool
    public init(method: String = "POST", path: String = "", needsReverify: Bool = false) {
        self.method = method; self.path = path; self.needsReverify = needsReverify
    }
    enum CodingKeys: String, CodingKey {
        case method, path
        case needsReverify = "needs_reverify"
    }
}

public struct NeedsConfirm: Sendable, Equatable, Codable {
    public var ticketId: String
    public var riskClass: String
    public var channel: String
    public var label: String
    public var recipient: ConfirmRecipient?
    public var text: String?
    public var diff: ConfirmDiff?
    public var attachments: [String]
    public var undo: UndoInfo
    public var expiresAt: String?
    public var approve: ConfirmEndpoint?
    public var deny: ConfirmEndpoint?
    public init(
        ticketId: String = "", riskClass: String = "", channel: String = "", label: String = "",
        recipient: ConfirmRecipient? = nil, text: String? = nil, diff: ConfirmDiff? = nil,
        attachments: [String] = [], undo: UndoInfo = .init(), expiresAt: String? = nil,
        approve: ConfirmEndpoint? = nil, deny: ConfirmEndpoint? = nil
    ) {
        self.ticketId = ticketId; self.riskClass = riskClass; self.channel = channel; self.label = label
        self.recipient = recipient; self.text = text; self.diff = diff; self.attachments = attachments
        self.undo = undo; self.expiresAt = expiresAt; self.approve = approve; self.deny = deny
    }
    /// True only when the server sent the full rich payload. Until the
    /// `rich_confirm` capability ships, the card renders label-only and the
    /// approve control stays disabled (contract §3).
    public var isRich: Bool { recipient != nil || text != nil || diff != nil }
    enum CodingKeys: String, CodingKey {
        case channel, label, recipient, text, diff, attachments, undo, approve, deny
        case ticketId = "ticket_id"
        case riskClass = "risk_class"
        case expiresAt = "expires_at"
    }
}

public struct TurnProgress: Sendable, Equatable, Codable {
    public var text: String
    public var spoken: Bool
    public var sequence: Int
    public init(text: String = "", spoken: Bool = false, sequence: Int = 0) {
        self.text = text; self.spoken = spoken; self.sequence = sequence
    }
}

public struct TurnCompleted: Sendable, Equatable, Codable {
    public var summary: String
    public var receiptIds: [String]
    public var verified: Bool
    public var label: String
    public init(summary: String = "", receiptIds: [String] = [], verified: Bool = false, label: String = "") {
        self.summary = summary; self.receiptIds = receiptIds; self.verified = verified; self.label = label
    }
    enum CodingKeys: String, CodingKey {
        case summary, verified, label
        case receiptIds = "receipt_ids"
    }
}

public struct TurnFailed: Sendable, Equatable, Codable {
    public var reason: String
    public var category: String
    public var failedStepId: String?
    public var recoverable: Bool
    public var nextStep: String
    public var receiptIds: [String]
    public init(
        reason: String = "", category: String = "other", failedStepId: String? = nil,
        recoverable: Bool = false, nextStep: String = "", receiptIds: [String] = []
    ) {
        self.reason = reason; self.category = category; self.failedStepId = failedStepId
        self.recoverable = recoverable; self.nextStep = nextStep; self.receiptIds = receiptIds
    }
    enum CodingKeys: String, CodingKey {
        case reason, category, recoverable
        case failedStepId = "failed_step_id"
        case nextStep = "next_step"
        case receiptIds = "receipt_ids"
    }
}

public struct BackgroundRef: Sendable, Equatable, Codable {
    public var type: String
    public var id: String
    public init(type: String = "", id: String = "") {
        self.type = type; self.id = id
    }
}

public struct BackgroundUpdate: Sendable, Equatable, Codable {
    public var kind: String
    public var label: String
    public var ref: BackgroundRef?
    public init(kind: String = "", label: String = "", ref: BackgroundRef? = nil) {
        self.kind = kind; self.label = label; self.ref = ref
    }
}

public struct PanicStateEvent: Sendable, Equatable, Codable {
    public var panicked: Bool
    public var at: String?
    public var reason: String?
    public init(panicked: Bool = false, at: String? = nil, reason: String? = nil) {
        self.panicked = panicked; self.at = at; self.reason = reason
    }
}

// MARK: - Envelope

// MARK: - Tolerant leaf decoders
//
// The contract shows these structs in minimal form (`"undo": {"kind":
// "none"}`, a deny endpoint without `needs_reverify`), so absent keys
// decode to defaults instead of throwing the whole frame. Malformed
// frames (missing envelope `id`, wrong types) still throw and are
// skipped+counted by the stream parser, never rendered.

extension UndoInfo {
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            kind: c.decode(String.self, forKey: .kind, default: "none"),
            label: c.decode(String.self, forKey: .label, default: ""),
            entryId: try? c.decodeIfPresent(String.self, forKey: .entryId)
        )
    }
}

extension ConfirmEndpoint {
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            method: c.decode(String.self, forKey: .method, default: "POST"),
            path: c.decode(String.self, forKey: .path, default: ""),
            needsReverify: c.decode(Bool.self, forKey: .needsReverify, default: false)
        )
    }
}

extension ConfirmRecipient {
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            name: c.decode(String.self, forKey: .name, default: ""),
            address: c.decode(String.self, forKey: .address, default: ""),
            addressKind: c.decode(String.self, forKey: .addressKind, default: ""),
            firstContact: c.decode(Bool.self, forKey: .firstContact, default: false)
        )
    }
}

extension ConfirmDiff {
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            kind: c.decode(String.self, forKey: .kind, default: ""),
            body: c.decode(String.self, forKey: .body, default: ""),
            truncated: c.decode(Bool.self, forKey: .truncated, default: false)
        )
    }
}

extension BackgroundRef {
    private enum Keys: String, CodingKey { case type, id }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        self.init(
            type: c.decode(String.self, forKey: .type, default: ""),
            id: c.decode(String.self, forKey: .id, default: "")
        )
    }
}

public enum PresenceEventPayload: Sendable, Equatable {
    case streamHello(StreamHello)
    case turnStarted(TurnStarted)
    case turnListening(TurnListening)
    case transcriptPartial(TranscriptPartial)
    case transcriptFinal(TranscriptFinal)
    case turnThinking(TurnThinking)
    case planUpdated(PlanUpdated)
    case toolStarted(ToolStarted)
    case toolFinished(ToolFinished)
    case lookingAt(LookingAt)
    case needsConfirm(NeedsConfirm)
    case turnProgress(TurnProgress)
    case turnCompleted(TurnCompleted)
    case turnFailed(TurnFailed)
    case backgroundUpdate(BackgroundUpdate)
    case panicState(PanicStateEvent)
    /// Additive rule: unknown names decode, never throw. The reducer ignores them.
    case unknown(type: String)
}

public struct PresenceEvent: Sendable, Equatable, Decodable {
    public var v: String
    public var id: String
    public var seq: Int
    public var ts: String
    public var turnId: String?
    public var type: String
    public var payload: PresenceEventPayload

    public init(v: String = PresenceContract.version, id: String, seq: Int = 0,
                ts: String = "", turnId: String? = nil, type: String,
                payload: PresenceEventPayload) {
        self.v = v; self.id = id; self.seq = seq; self.ts = ts
        self.turnId = turnId; self.type = type; self.payload = payload
    }

    enum CodingKeys: String, CodingKey {
        case v, id, seq, ts, type, data
        case turnId = "turn_id"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        v = (try? c.decode(String.self, forKey: .v)) ?? PresenceContract.version
        id = try c.decode(String.self, forKey: .id)
        seq = (try? c.decode(Int.self, forKey: .seq)) ?? 0
        ts = (try? c.decode(String.self, forKey: .ts)) ?? ""
        turnId = try? c.decode(String.self, forKey: .turnId)
        type = try c.decode(String.self, forKey: .type)
        switch type {
        case "stream.hello": payload = .streamHello(try c.decode(StreamHello.self, forKey: .data))
        case "turn.started": payload = .turnStarted(try c.decode(TurnStarted.self, forKey: .data))
        case "turn.listening": payload = .turnListening(try c.decode(TurnListening.self, forKey: .data))
        case "turn.transcript.partial": payload = .transcriptPartial(try c.decode(TranscriptPartial.self, forKey: .data))
        case "turn.transcript.final": payload = .transcriptFinal(try c.decode(TranscriptFinal.self, forKey: .data))
        case "turn.thinking": payload = .turnThinking(try c.decode(TurnThinking.self, forKey: .data))
        case "plan.updated": payload = .planUpdated(try c.decode(PlanUpdated.self, forKey: .data))
        case "tool.started": payload = .toolStarted(try c.decode(ToolStarted.self, forKey: .data))
        case "tool.finished": payload = .toolFinished(try c.decode(ToolFinished.self, forKey: .data))
        case "ambient.looking_at": payload = .lookingAt(try c.decode(LookingAt.self, forKey: .data))
        case "turn.needs_confirm": payload = .needsConfirm(try c.decode(NeedsConfirm.self, forKey: .data))
        case "turn.progress": payload = .turnProgress(try c.decode(TurnProgress.self, forKey: .data))
        case "turn.completed": payload = .turnCompleted(try c.decode(TurnCompleted.self, forKey: .data))
        case "turn.failed": payload = .turnFailed(try c.decode(TurnFailed.self, forKey: .data))
        case "background.update": payload = .backgroundUpdate(try c.decode(BackgroundUpdate.self, forKey: .data))
        case "panic.state": payload = .panicState(try c.decode(PanicStateEvent.self, forKey: .data))
        default: payload = .unknown(type: type)
        }
    }
}
