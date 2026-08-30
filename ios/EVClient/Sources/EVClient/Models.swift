/// EV v1 API models shared by the iPhone and Watch clients.
///
/// The server serializes with snake_case keys and ISO-8601 timestamps; the
/// client decodes with `.convertFromSnakeCase` and keeps timestamps as strings
/// so every surface renders them exactly as the backend emitted them.

import Foundation

public enum AnyCodable: Codable, Sendable, Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: AnyCodable])
    case array([AnyCodable])
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([AnyCodable].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: AnyCodable].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unsupported JSON value"
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }

    public var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    public var objectValue: [String: AnyCodable]? {
        if case .object(let value) = self { return value }
        return nil
    }

    public var arrayValue: [AnyCodable]? {
        if case .array(let value) = self { return value }
        return nil
    }

    public var boolValue: Bool? {
        if case .bool(let value) = self { return value }
        return nil
    }

    public var numberValue: Double? {
        if case .number(let value) = self { return value }
        return nil
    }

    public static func wrap(_ value: Any?) -> AnyCodable {
        switch value {
        case nil: return .null
        case let value as AnyCodable: return value
        case let value as String: return .string(value)
        case let value as Bool: return .bool(value)
        case let value as Double: return .number(value)
        case let value as Int: return .number(Double(value))
        case let value as [Any]: return .array(value.map { wrap($0) })
        case let value as [String: Any]:
            return .object(value.mapValues { wrap($0) })
            default:
                return .string(String(describing: value as Any))
        }
    }

    public static func dictionary(_ value: [String: Any]?) -> [String: AnyCodable]? {
        guard let value else { return nil }
        return value.mapValues { wrap($0) }
    }

    public func jsonObject() -> Any {
        switch self {
        case .string(let value): return value
        case .number(let value): return value
        case .bool(let value): return value
        case .object(let value): return value.mapValues { $0.jsonObject() }
        case .array(let value): return value.map { $0.jsonObject() }
        case .null: return NSNull()
        }
    }
}

/// The only camera states the client is allowed to present as fact.
/// `unknown` is deliberately retained for a missing/stale provider report;
/// it must never be rendered as `active`.
public enum CameraState: String, Codable, CaseIterable, Sendable, Equatable {
    case off
    case paused
    case active
    case denied
    case unavailable
    case error
    case unknown

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = CameraState(rawValue: raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()) ?? .unknown
    }

    public var label: String {
        switch self {
        case .off: return "off"
        case .paused: return "paused"
        case .active: return "active"
        case .denied: return "permission denied"
        case .unavailable: return "unavailable"
        case .error: return "error"
        case .unknown: return "unknown"
        }
    }

    public var isTruthfullyActive: Bool { self == .active }
}

/// Agent 2's camera-state contract, carried over live events and runtime
/// snapshots. The fields are intentionally descriptive so a denied or failed
/// camera cannot be mistaken for a successful observation.
public struct CameraStateSnapshot: Codable, Sendable, Equatable {
    public let state: CameraState
    public let visible: Bool
    public let deviceId: String?
    public let platform: String?
    public let permissionState: String?
    public let explicitRequest: Bool
    public let pausedReason: String?
    public let consentState: String?
    public let rawFramesPersisted: Bool
    public let lastError: String?
    public let updatedAt: String?

    public init(
        state: CameraState,
        visible: Bool = false,
        deviceId: String? = nil,
        platform: String? = nil,
        permissionState: String? = nil,
        explicitRequest: Bool = false,
        pausedReason: String? = nil,
        consentState: String? = nil,
        rawFramesPersisted: Bool = false,
        lastError: String? = nil,
        updatedAt: String? = nil
    ) {
        self.state = state
        self.visible = visible
        self.deviceId = deviceId
        self.platform = platform
        self.permissionState = permissionState
        self.explicitRequest = explicitRequest
        self.pausedReason = pausedReason
        self.consentState = consentState
        self.rawFramesPersisted = rawFramesPersisted
        self.lastError = lastError
        self.updatedAt = updatedAt
    }

    public static let unknown = CameraStateSnapshot(state: .unknown)

    /// `visible` is part of the contract rather than inferred from `state`.
    public var isTruthfullyActive: Bool {
        state == .active && visible && permissionState?.lowercased() != "denied"
    }

    public var presentationLabel: String {
        if state == .active && !isTruthfullyActive {
            return "active · visibility unconfirmed"
        }
        return state.label
    }

    public init(json object: [String: Any]) {
        let rawState = (object["state"] as? String ?? "unknown").lowercased()
        self.init(
            state: CameraState(rawValue: rawState) ?? .unknown,
            visible: object["visible"] as? Bool ?? false,
            deviceId: object["device_id"] as? String ?? object["deviceId"] as? String,
            platform: object["platform"] as? String,
            permissionState: object["permission_state"] as? String ?? object["permissionState"] as? String,
            explicitRequest: object["explicit_request"] as? Bool ?? object["explicitRequest"] as? Bool ?? false,
            pausedReason: object["paused_reason"] as? String ?? object["pausedReason"] as? String,
            consentState: object["consent_state"] as? String ?? object["consentState"] as? String,
            rawFramesPersisted: object["raw_frames_persisted"] as? Bool ?? object["rawFramesPersisted"] as? Bool ?? false,
            lastError: object["last_error"] as? String ?? object["lastError"] as? String,
            updatedAt: object["updated_at"] as? String ?? object["updatedAt"] as? String
        )
    }
}

public enum CapabilityAvailability: String, Codable, Sendable, Equatable {
    case enabled
    case needsPermission = "needs_permission"
    case unavailable
    case refused
    case unknown
}

/// A client-facing view of Agent 1's live capability manifest. The client
/// displays this data; it does not decide policy or manufacture capabilities.
public struct CapabilityManifest: Codable, Sendable, Equatable {
    public let enabled: [String]
    public let needsPermission: [String]
    public let unavailable: [String]
    public let refused: [String]
    public let devices: [String]
    public let providers: [String]
    public let requiresConfirmation: [String]
    public let fallbacks: [String: String]
    public let quietHoursActive: Bool?

    public init(
        enabled: [String] = [],
        needsPermission: [String] = [],
        unavailable: [String] = [],
        refused: [String] = [],
        devices: [String] = [],
        providers: [String] = [],
        requiresConfirmation: [String] = [],
        fallbacks: [String: String] = [:],
        quietHoursActive: Bool? = nil
    ) {
        // These arrays are rendered as mutually exclusive status buckets.
        // Live payloads can contain the same capability through both an
        // explicit list and a protocol-status projection, so normalize at
        // the presentation boundary instead of making each view reconcile
        // the lists independently.
        let normalizedEnabled = Self.uniquePresentationValues(enabled)
        let enabledKeys = Self.presentationKeys(for: normalizedEnabled)
        let normalizedNeedsPermission = Self.uniquePresentationValues(needsPermission)
            .filter { !enabledKeys.contains(Self.presentationKey($0)) }
        let needsPermissionKeys = Self.presentationKeys(for: normalizedNeedsPermission)
        let normalizedUnavailable = Self.uniquePresentationValues(unavailable)
            .filter {
                let key = Self.presentationKey($0)
                return !enabledKeys.contains(key) && !needsPermissionKeys.contains(key)
            }
        let unavailableKeys = Self.presentationKeys(for: normalizedUnavailable)
        let normalizedRefused = Self.uniquePresentationValues(refused)
            .filter {
                let key = Self.presentationKey($0)
                return !enabledKeys.contains(key)
                    && !needsPermissionKeys.contains(key)
                    && !unavailableKeys.contains(key)
            }

        self.enabled = normalizedEnabled
        self.needsPermission = normalizedNeedsPermission
        self.unavailable = normalizedUnavailable
        self.refused = normalizedRefused
        self.devices = devices
        self.providers = providers
        self.requiresConfirmation = requiresConfirmation
        self.fallbacks = fallbacks
        self.quietHoursActive = quietHoursActive
    }

    private static func presentationKey(_ value: String) -> String {
        value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    }

    private static func uniquePresentationValues(_ values: [String]) -> [String] {
        var seen = Set<String>()
        return values.compactMap { rawValue in
            let value = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
            let key = presentationKey(value)
            guard !key.isEmpty, seen.insert(key).inserted else { return nil }
            return value
        }
    }

    private static func presentationKeys(for values: [String]) -> Set<String> {
        Set(values.map(presentationKey))
    }

    private enum CodingKeys: String, CodingKey {
        case enabled
        case needsPermission
        case unavailable
        case refused
        case devices
        case providers
        case requiresConfirmation
        case fallbacks
        case quietHoursActive
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            enabled: try container.decodeIfPresent([String].self, forKey: .enabled) ?? [],
            needsPermission: try container.decodeIfPresent([String].self, forKey: .needsPermission) ?? [],
            unavailable: try container.decodeIfPresent([String].self, forKey: .unavailable) ?? [],
            refused: try container.decodeIfPresent([String].self, forKey: .refused) ?? [],
            devices: try container.decodeIfPresent([String].self, forKey: .devices) ?? [],
            providers: try container.decodeIfPresent([String].self, forKey: .providers) ?? [],
            requiresConfirmation: try container.decodeIfPresent([String].self, forKey: .requiresConfirmation) ?? [],
            fallbacks: try container.decodeIfPresent([String: String].self, forKey: .fallbacks) ?? [:],
            quietHoursActive: try container.decodeIfPresent(Bool.self, forKey: .quietHoursActive)
        )
    }

    public var isEmpty: Bool {
        enabled.isEmpty && needsPermission.isEmpty && unavailable.isEmpty && refused.isEmpty
    }

    // Contract-language aliases used by the live capability manifest.
    public var missingPermissions: [String] { needsPermission }
    public var currentDevices: [String] { devices }
    public var activeProviders: [String] { providers }
    public var requiredConfirmation: [String] { requiresConfirmation }
    public var currentUnavailableState: [String] { unavailable }

    public func availability(of capability: String) -> CapabilityAvailability {
        let value = Self.presentationKey(capability)
        if enabled.contains(where: { Self.presentationKey($0) == value }) { return .enabled }
        if needsPermission.contains(where: { Self.presentationKey($0) == value }) { return .needsPermission }
        if unavailable.contains(where: { Self.presentationKey($0) == value }) { return .unavailable }
        if refused.contains(where: { Self.presentationKey($0) == value }) { return .refused }
        return .unknown
    }

    public init(json object: [String: Any]) {
        func strings(_ key: String, statuses: Set<String>? = nil) -> [String] {
            if let values = object[key] as? [String] { return values }
            if let value = object[key] as? String { return [value] }
            if let values = object[key] as? [[String: Any]] {
                return values.compactMap { item in
                    if let statuses,
                       let status = (item["status"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(),
                       !statuses.contains(status) {
                        return nil
                    }
                    return (item["title"] as? String) ?? (item["key"] as? String)
                }
            }
            return []
        }
        func protocolTitles(with status: String) -> [String] {
            guard let values = object["protocols"] as? [[String: Any]] else { return [] }
            return values.compactMap { item in
                guard (item["status"] as? String)?.lowercased() == status else { return nil }
                return (item["title"] as? String) ?? (item["key"] as? String)
            }
        }
        let fallback = (object["fallbacks"] as? [String: String])
            ?? Dictionary(uniqueKeysWithValues: ((object["fallbacks"] as? [String]) ?? []).map { ($0, $0) })
        let activeProviders = (object["active_providers"] as? [String: Any])?.compactMapValues {
            $0 as? String
        } ?? [:]
        let permissionStatuses: Set<String> = [
            "needs_permission",
            "permission_denied",
            "denied",
            "not_granted",
        ]
        let unavailableStatuses: Set<String> = [
            "needs_setup",
            "locked",
            "unavailable",
            "not_connected",
            "error",
        ]
        self.init(
            enabled: strings("enabled"),
            needsPermission: strings("needs_permission", statuses: permissionStatuses)
                + strings("needsPermission", statuses: permissionStatuses)
                + strings("missing_permissions", statuses: permissionStatuses)
                + strings("missingPermissions", statuses: permissionStatuses)
                + protocolTitles(with: "needs_permission"),
            unavailable: strings("unavailable", statuses: unavailableStatuses)
                + strings("current_unavailable_state", statuses: unavailableStatuses)
                + strings("currentUnavailableState", statuses: unavailableStatuses)
                + strings("missing_permissions", statuses: unavailableStatuses)
                + strings("missingPermissions", statuses: unavailableStatuses)
                + strings("missing_setup", statuses: unavailableStatuses)
                + strings("missingSetup", statuses: unavailableStatuses)
                + protocolTitles(with: "needs_setup")
                + protocolTitles(with: "locked")
                + protocolTitles(with: "unavailable"),
            refused: strings("refused") + protocolTitles(with: "refused"),
            devices: strings("devices")
                + strings("current_devices")
                + strings("currentDevices"),
            providers: strings("providers")
                + strings("activeProviders")
                + Array(activeProviders.values),
            requiresConfirmation: strings("requires_confirmation")
                + strings("requiresConfirmation")
                + strings("required_confirmation")
                + strings("requiredConfirmation"),
            fallbacks: fallback,
            quietHoursActive: object["quiet_hours_active"] as? Bool ?? object["quietHoursActive"] as? Bool
        )
    }
}

public enum DevicePresence: String, Codable, Sendable, Equatable {
    case online
    case away
    case unknown

    public init(rawValue: String) {
        switch rawValue.lowercased() {
        case "online": self = .online
        case "away": self = .away
        default: self = .unknown
        }
    }
}

public enum DeviceMeshRole: String, Codable, Sendable, Equatable {
    case macPrimary = "mac_primary"
    case iphone16ProPrimaryCamera = "iphone_16_pro_primary_camera"
    case iphoneSEFallback = "iphone_se_fallback"
    case other

    public var label: String {
        switch self {
        case .macPrimary: return "Mac primary"
        case .iphone16ProPrimaryCamera: return "iPhone 16 Pro camera"
        case .iphoneSEFallback: return "iPhone SE fallback"
        case .other: return "device"
        }
    }
}

public struct DeviceMeshNode: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let name: String
    public let role: DeviceMeshRole
    public let presence: DevicePresence
    public let capabilities: [String]
    public let deviceType: String?
    public let platform: String?
    public let batteryPercent: Double?
    public let lastSeenAt: String?
    public let lastHeartbeatAt: String?

    public init(
        id: String,
        name: String,
        role: DeviceMeshRole? = nil,
        presence: DevicePresence = .unknown,
        capabilities: [String] = [],
        deviceType: String? = nil,
        platform: String? = nil,
        batteryPercent: Double? = nil,
        lastSeenAt: String? = nil,
        lastHeartbeatAt: String? = nil
    ) {
        self.id = id
        self.name = name
        self.role = role ?? Self.classify(name: name, deviceType: deviceType, capabilities: capabilities)
        self.presence = presence
        self.capabilities = capabilities
        self.deviceType = deviceType
        self.platform = platform
        self.batteryPercent = batteryPercent
        self.lastSeenAt = lastSeenAt
        self.lastHeartbeatAt = lastHeartbeatAt
    }

    public var isReachable: Bool { presence == .online }

    public var supportsCamera: Bool {
        let caps = Set(capabilities.map { $0.lowercased() })
        return caps.contains("camera") || caps.contains("camera:read") || role != .other
    }

    public static func classify(
        name: String,
        deviceType: String? = nil,
        capabilities: [String] = []
    ) -> DeviceMeshRole {
        let value = "\(name) \(deviceType ?? "")".lowercased()
        if value.contains("mac") || value.contains("desktop") { return .macPrimary }
        if value.contains("16 pro") || value.contains("iphone 16") || value.contains("phone a") {
            return .iphone16ProPrimaryCamera
        }
        if value.contains("se") || value.contains("phone b") { return .iphoneSEFallback }
        _ = capabilities
        return .other
    }
}

public struct DeviceMeshSnapshot: Codable, Sendable, Equatable {
    public let generatedAt: String?
    public let nodes: [DeviceMeshNode]

    public init(generatedAt: String? = nil, nodes: [DeviceMeshNode] = []) {
        self.generatedAt = generatedAt
        self.nodes = nodes
    }

    public var onlineNodes: [DeviceMeshNode] { nodes.filter(\.isReachable) }

    /// Mac owns the primary perception route. Mobile camera requests fall
    /// back to iPhone 16 Pro, then the SE, only when that node is online.
    public func preferredCameraNode(preferMac: Bool = true) -> DeviceMeshNode? {
        let priority: [DeviceMeshRole]
        if preferMac {
            priority = [.macPrimary, .iphone16ProPrimaryCamera, .iphoneSEFallback]
        } else {
            priority = [.iphone16ProPrimaryCamera, .iphoneSEFallback, .macPrimary]
        }
        for role in priority {
            if let node = onlineNodes.first(where: { $0.role == role && $0.supportsCamera }) {
                return node
            }
        }
        return onlineNodes.first(where: \.supportsCamera)
    }

    public func node(id: String?) -> DeviceMeshNode? {
        guard let id else { return nil }
        return nodes.first { $0.id == id }
    }
}

public struct CapturePayload: Codable, Sendable, Equatable {
    public var source: String
    public var eventType: String
    public var text: String
    public var privacyLevel: String
    public var deviceID: String?

    public init(
        source: String = "ios",
        eventType: String = "note",
        text: String,
        privacyLevel: String = "normal",
        deviceID: String? = nil
    ) {
        self.source = source
        self.eventType = eventType
        self.text = text
        self.privacyLevel = privacyLevel
        self.deviceID = deviceID
    }
}

public struct EventOut: Codable, Sendable, Equatable {
    public let id: String
    public let occurredAt: String
    public let ingestedAt: String
    public let source: String
    public let eventType: String
    public let content: [String: String]
    public let metadata: [String: AnyCodable]?
    public let deviceID: String?
    public let conversationId: String?
    public let privacyLevel: String
    public let sha256: String
    public let tombstonedAt: String?
    public let tombstoneReason: String?
}

public struct MemoryDelta: Codable, Sendable, Equatable {
    public let id: String
    public let memoryType: String
    public let action: String
    public let text: String
}

public struct CaptureResponse: Codable, Sendable, Equatable {
    public let event: EventOut
    public let memoryDelta: [MemoryDelta]
}

public struct AttachmentOut: Codable, Sendable, Equatable {
    public let id: String
    public let eventId: String
    public let filename: String
    public let contentType: String?
    public let sizeBytes: Int
    public let storageKey: String
    public let sha256: String
    public let createdAt: String
}

public struct AttachmentCreateResponse: Codable, Sendable, Equatable {
    public let attachment: AttachmentOut
    public let event: EventOut
}

public struct ProvenanceItem: Codable, Sendable, Equatable {
    public let memoryId: String
    public let text: String
    public let memoryType: String
    public let score: Double
    public let components: [String: Double]?
}

public struct ChatResponse: Codable, Sendable, Equatable {
    public let reply: String
    public let conversationId: String?
    public let model: String?
    public let contextTokens: Int
    public let contextDepth: String?
    public let requestID: String?
    public let memoryDelta: [MemoryDelta]
    public let provenance: [ProvenanceItem]
    public let filterReport: AnyCodable?
}

public struct MemoryOut: Codable, Sendable, Equatable {
    public let id: String
    public let memoryType: String
    public let text: String
    public let version: Int
    public let importance: Double?
    public let confidence: Double?
    public let sourceType: String?
    public let privacyLevel: String?
    public let eventTime: String?
    public let createdTime: String?
    public let updatedTime: String?
    public let isCurrent: Bool?
    public let provenance: [ProvenanceItem]?
}

public struct MemoryListResponse: Codable, Sendable, Equatable {
    public let memories: [MemoryOut]
    public let total: Int
}

public struct TimelineResponse: Codable, Sendable, Equatable {
    public let events: [EventOut]
    public let nextCursor: String?
}

public struct AuditResponse: Codable, Sendable, Equatable {
    public let memory: MemoryOut
    public let versions: [MemoryOut]
    public let sourceEvents: [EventOut]
    public let conflicts: [AnyCodable]?
    public let accessLog: [AnyCodable]?
}

public struct HealthResponse: Codable, Sendable, Equatable {
    public let status: String
    public let app: String
    public let environment: String
    public let version: String
    public let capabilities: [String]
    public let providers: [String: AnyCodable]?
    public let runtime: [String: AnyCodable]?
}

/// Backend-owned integration state. A macOS TCC grant is intentionally not
/// represented here: the two states must remain visibly independent.
public struct IntegrationRecord: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let slug: String
    public let adapter: String
    public let name: String
    public let scopes: [String]
    public let status: String
    public let privacyLevel: String
    public let config: [String: AnyCodable]
    public let credentialConfigured: Bool
    public let createdAt: String?
    public let updatedAt: String?
}

public struct IntegrationOAuthAuthorize: Codable, Sendable, Equatable {
    public let authorizeURL: String
    public let state: String
    public let expiresAt: String?
}

public struct IntegrationOAuthStatus: Codable, Sendable, Equatable {
    public let provider: String?
    public let authorized: Bool
    public let configured: Bool
    public let expiresAt: String?
    public let expired: Bool
    public let reauthRequired: Bool
}

public extension HealthResponse {
    /// Converts the health payload into the same honest presentation model
    /// used by live `ready` events. Health is not camera evidence, so this
    /// never marks a camera enabled by inference.
    var capabilityManifest: CapabilityManifest {
        CapabilityManifest(
            enabled: capabilities,
            providers: providers.map { Array($0.keys).sorted() } ?? []
        )
    }
}

public struct ConversationOut: Codable, Sendable, Equatable {
    public let id: String
    public let title: String?
    public let createdAt: String?
}

public struct ConversationMessage: Codable, Sendable, Equatable {
    public let id: String
    public let role: String
    public let text: String
    public let occurredAt: String?
}

public struct ConversationState: Codable, Sendable, Equatable {
    public let focus: String?
    public let pendingQuestions: [String]?
}

public struct ConversationDetail: Codable, Sendable, Equatable {
    public let conversation: ConversationOut
    public let messages: [ConversationMessage]
    public let state: ConversationState?
    public let nextActions: [String]?
}

public struct VoiceWakeResponse: Codable, Sendable, Equatable {
    public let sessionId: String?
    public let state: String
    public let ownerEnrolled: Bool
    public let challengeNonce: String?
    public let challengePhrase: String?
    public let message: String?
}

public struct VoiceLiveOpenResponse: Codable, Sendable, Equatable {
    public let sessionId: String
    public let state: String
    public let conversationId: String?
    public let live: Bool
    public let message: String?
}

public struct VoiceSessionVerifyResponse: Codable, Sendable, Equatable {
    public let sessionId: String?
    public let state: String
    public let verified: Bool
    public let confidence: Double
    public let reason: String
}

/// TTS synthesis metadata returned with a voice reply
/// (mirrors ``backend/app/schemas.py::TtsOut``).
public struct TtsOut: Codable, Sendable, Equatable {
    public let provider: String
    public let audioRef: String?
    public let audioB64: String?
    public let contentType: String?
    public let ssml: String?
    public let durationMs: Int?
    public let degraded: Bool
}

/// Voice style metadata returned with a voice reply
/// (mirrors ``backend/app/schemas.py::SpeechStyleOut``).
public struct SpeechStyleOut: Codable, Sendable, Equatable {
    public let urgency: Double
    public let warmth: Double
    public let brevity: Double
    public let mode: String
    public let lengthTarget: String
    public let directness: String
}

public struct OwnerPrefs: Codable, Sendable, Equatable {
    public let nickname: String
    public let quietHours: [String: String?]?
    public let hudLayout: [String: AnyCodable]?
    public let featureGates: [AnyCodable]?
    public let ttsVoice: String?
    public let liveConversationId: String?
    public let volumePercent: Int?
}

public struct DeviceCreateResponse: Codable, Sendable, Equatable {
    public let device: DeviceRegistryRow
    public let token: String
}

public struct DeviceRegistryRow: Codable, Sendable, Equatable {
    public let id: String
    public let name: String
}

public struct DeviceBootstrap: Codable, Sendable, Equatable {
    public let deviceId: String
    public let prefs: OwnerPrefs?
    public let spoken: Bool
    public let spokenText: String?
    public let ttsDeviceId: String?
    public let bootstrappedSpokenAt: String?
    public let prefsLoaded: Bool?
}

public struct DevicePanic: Codable, Sendable, Equatable {
    public let ok: Bool
    public let revoked: Bool?
    public let spoken: String?
    public let lookout: String?
}

public struct ToolDispatchResponse: Codable, Sendable, Equatable {
    public let name: String
    public let ok: Bool
    public let result: [String: AnyCodable]?
    public let error: String?
    public let latencyMs: Double?
    public let requestId: String?
}

public struct ApprovedActionOut: Codable, Sendable, Equatable {
    public let id: String
    public let actionType: String?
    public let status: String
    public let error: String?
}

public struct ReverificationResponse: Codable, Sendable, Equatable {
    public let token: String
    public let purpose: String
    public let expiresAt: String
}

public struct WebauthnAssertion: Encodable, Sendable {
    public let challengeId: String
    public let credentialId: String
    public let clientDataJson: String
    public let authenticatorData: String
    public let signature: String

    public init(
        challengeId: String,
        credentialId: String,
        clientDataJson: String,
        authenticatorData: String,
        signature: String
    ) {
        self.challengeId = challengeId
        self.credentialId = credentialId
        self.clientDataJson = clientDataJson
        self.authenticatorData = authenticatorData
        self.signature = signature
    }
}

public struct VoiceUtteranceResponse: Codable, Sendable, Equatable {
    public let sessionId: String
    public let state: String
    public let transcript: String
    public let transcriptConfidence: Double
    public let transcriptDegraded: Bool?
    public let transcriptProvider: String?
    public let reply: String
    public let conversationId: String?
    public let tts: TtsOut?
    public let ttsDeviceId: String?
    public let style: SpeechStyleOut?
    public let model: String?
    public let contextTokens: Int
    public let memoryDeltas: [MemoryDelta]
    public let error: String?
}
