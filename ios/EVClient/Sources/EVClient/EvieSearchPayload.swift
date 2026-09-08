/// iPhone-only (EAC107): gateway search payload models.
///
/// Decodes GET /v1/device-gateway/search — grouped hits across memories,
/// events, reminders, and the phone's own contacts snapshot.

import Foundation

public struct EvieSearchResultGroup: Codable, Equatable, Sendable {
    public struct MemoryHit: Codable, Equatable, Sendable {
        public var id: String
        public var memoryType: String?
        public var text: String

        public init(id: String, memoryType: String?, text: String) {
            self.id = id
            self.memoryType = memoryType
            self.text = text
        }

        private enum CodingKeys: String, CodingKey {
            case id
            case memoryType = "memory_type"
            case text
        }
    }

    public struct EventHit: Codable, Equatable, Sendable {
        public var id: String
        public var kind: String?
        public var text: String
        public var occurredAt: String?

        public init(id: String, kind: String?, text: String, occurredAt: String?) {
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

    public struct ReminderHit: Codable, Equatable, Sendable {
        public var id: String
        public var text: String

        public init(id: String, text: String) {
            self.id = id
            self.text = text
        }
    }

    public struct ContactHit: Codable, Equatable, Sendable {
        public var name: String

        public init(name: String) {
            self.name = name
        }
    }

    public var query: String
    public var memoryEnabled: Bool
    public var memories: [MemoryHit]
    public var events: [EventHit]
    public var reminders: [ReminderHit]
    public var contacts: [ContactHit]

    public init(
        query: String,
        memoryEnabled: Bool,
        memories: [MemoryHit],
        events: [EventHit],
        reminders: [ReminderHit],
        contacts: [ContactHit]
    ) {
        self.query = query
        self.memoryEnabled = memoryEnabled
        self.memories = memories
        self.events = events
        self.reminders = reminders
        self.contacts = contacts
    }

    private enum CodingKeys: String, CodingKey {
        case query
        case memoryEnabled = "memory_enabled"
        case memories
        case events
        case reminders
        case contacts
    }

    public var totalHits: Int {
        memories.count + events.count + reminders.count + contacts.count
    }
}
