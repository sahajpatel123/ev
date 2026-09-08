/// iPhone-only (EAC81): gateway memory browser payload models.
///
/// Decodes GET /v1/device-gateway/memories and /memories/{id}. Tolerant —
/// a sandboxed phone receives memory_enabled=false and no rows; surfaces
/// must render the off state instead of fabricating an empty memory.

import Foundation

public struct EvieMemoryRow: Codable, Equatable, Sendable {
    public var id: String
    public var memoryType: String?
    public var text: String
    public var importance: Double?
    public var confidence: Double?
    public var sourceType: String?
    public var updatedTime: String?

    public init(
        id: String,
        memoryType: String?,
        text: String,
        importance: Double? = nil,
        confidence: Double? = nil,
        sourceType: String? = nil,
        updatedTime: String? = nil
    ) {
        self.id = id
        self.memoryType = memoryType
        self.text = text
        self.importance = importance
        self.confidence = confidence
        self.sourceType = sourceType
        self.updatedTime = updatedTime
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case memoryType = "memory_type"
        case text
        case importance
        case confidence
        case sourceType = "source_type"
        case updatedTime = "updated_time"
    }
}

public struct EvieMemorySource: Codable, Equatable, Sendable {
    public var id: String
    public var kind: String?
    public var text: String?
    public var occurredAt: String?

    public init(id: String, kind: String?, text: String?, occurredAt: String?) {
        self.id = id
        self.kind = kind
        self.text = text
        self.occurredAt = occurredAt
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case kind
        case text
        case occurredAt = "occurred_at"
    }
}

public struct EvieMemoryList: Codable, Equatable, Sendable {
    public var memoryEnabled: Bool
    public var memories: [EvieMemoryRow]
    public var total: Int

    public init(memoryEnabled: Bool, memories: [EvieMemoryRow], total: Int) {
        self.memoryEnabled = memoryEnabled
        self.memories = memories
        self.total = total
    }

    private enum CodingKeys: String, CodingKey {
        case memoryEnabled = "memory_enabled"
        case memories
        case total
    }
}

public struct EvieMemoryDetail: Codable, Equatable, Sendable {
    public var memoryEnabled: Bool
    public var memory: EvieMemoryRow?
    public var sources: [EvieMemorySource]

    public init(memoryEnabled: Bool, memory: EvieMemoryRow?, sources: [EvieMemorySource]) {
        self.memoryEnabled = memoryEnabled
        self.memory = memory
        self.sources = sources
    }

    private enum CodingKeys: String, CodingKey {
        case memoryEnabled = "memory_enabled"
        case memory
        case sources
    }

    /// Provenance one-liner: source kinds joined, honest when absent.
    public func renderProvenance() -> String {
        let kinds = sources.compactMap { $0.kind }.filter { !$0.isEmpty }
        if kinds.isEmpty {
            return "No source events recorded"
        }
        let unique = NSOrderedSet(array: kinds).array as? [String] ?? kinds
        return "From " + unique.joined(separator: ", ")
    }
}
