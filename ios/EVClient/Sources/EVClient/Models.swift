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
}
