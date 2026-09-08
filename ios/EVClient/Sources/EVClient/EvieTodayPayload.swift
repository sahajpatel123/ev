/// iPhone-only (EAC80): `GET /v1/device-gateway/today` payload model.
///
/// One decode of the phone dashboard used by native surfaces (widget line,
/// watch complication, notification summary). Tolerant: every optional
/// section degrades to nil/empty instead of failing the whole decode, and
/// `renderSummary()` never fabricates content it does not have.

import Foundation

public struct EvieTodayPayload: Codable, Equatable, Sendable {
    public struct HudLine: Codable, Equatable, Sendable {
        public var title: String
        public var body: String

        public init(title: String, body: String) {
            self.title = title
            self.body = body
        }
    }

    public struct HealthLine: Codable, Equatable, Sendable {
        public var available: Bool
        public var freshness: String
        public var metrics: [String: Double]

        public init(available: Bool, freshness: String, metrics: [String: Double]) {
            self.available = available
            self.freshness = freshness
            self.metrics = metrics
        }
    }

    public struct CalendarEventLine: Codable, Equatable, Sendable {
        public var title: String
        public var start: String?

        public init(title: String, start: String?) {
            self.title = title
            self.start = start
        }
    }

    public struct ReminderLine: Codable, Equatable, Sendable {
        public var id: String
        public var text: String

        public init(id: String, text: String) {
            self.id = id
            self.text = text
        }
    }

    public struct MemoryLine: Codable, Equatable, Sendable {
        public var id: String
        public var memoryType: String?
        public var text: String

        public init(id: String, memoryType: String?, text: String) {
            self.id = id
            self.memoryType = memoryType
            self.text = text
        }
    }

    public var deviceName: String
    public var memoryEnabled: Bool
    public var memoryScope: String
    public var hud: HudLine?
    public var health: HealthLine?
    public var calendarEvents: [CalendarEventLine]
    public var reminders: [ReminderLine]
    public var memories: [MemoryLine]
    public var inboxPending: Int

    public init(
        deviceName: String,
        memoryEnabled: Bool,
        memoryScope: String,
        hud: HudLine?,
        health: HealthLine?,
        calendarEvents: [CalendarEventLine],
        reminders: [ReminderLine],
        memories: [MemoryLine],
        inboxPending: Int
    ) {
        self.deviceName = deviceName
        self.memoryEnabled = memoryEnabled
        self.memoryScope = memoryScope
        self.hud = hud
        self.health = health
        self.calendarEvents = calendarEvents
        self.reminders = reminders
        self.memories = memories
        self.inboxPending = inboxPending
    }

    private enum CodingKeys: String, CodingKey {
        case deviceName = "device"
        case memoryEnabled = "memory_enabled"
        case memoryScope = "memory_scope"
        case hud
        case health
        case calendarEvents = "calendar"
        case reminders
        case memories
        case inboxPending = "inbox_pending"
    }

    private enum DeviceKeys: String, CodingKey {
        case displayName = "display_name"
    }

    private enum CalendarKeys: String, CodingKey {
        case events
    }

    private enum HealthKeys: String, CodingKey {
        case available
        case freshness
        case metrics
    }

    public init(from decoder: Decoder) throws {
        let root = try decoder.container(keyedBy: CodingKeys.self)
        memoryEnabled = (try? root.decode(Bool.self, forKey: .memoryEnabled)) ?? false
        memoryScope = (try? root.decode(String.self, forKey: .memoryScope)) ?? "sandbox"
        inboxPending = (try? root.decode(Int.self, forKey: .inboxPending)) ?? 0
        hud = try? root.decodeIfPresent(HudLine.self, forKey: .hud)
        health = try? root.decodeIfPresent(HealthLine.self, forKey: .health)
        reminders = (try? root.decode([ReminderLine].self, forKey: .reminders)) ?? []
        memories = (try? root.decode([MemoryLine].self, forKey: .memories)) ?? []

        if let device = try? root.nestedContainer(keyedBy: DeviceKeys.self, forKey: .deviceName) {
            deviceName = (try? device.decode(String.self, forKey: .displayName)) ?? "iPhone"
        } else {
            deviceName = "iPhone"
        }
        if let calendar = try? root.nestedContainer(keyedBy: CalendarKeys.self, forKey: .calendarEvents) {
            calendarEvents = (try? calendar.decode([CalendarEventLine].self, forKey: .events)) ?? []
        } else {
            calendarEvents = []
        }
    }

    /// Compact one-liner for widgets and complications. Only real content.
    public func renderSummary() -> String {
        var parts: [String] = []
        if let hud, !hud.body.isEmpty {
            parts.append(hud.body)
        } else if let health {
            if health.available {
                let steps = health.metrics["steps"].map { Int($0) } ?? 0
                parts.append("\(steps) steps")
            } else {
                parts.append("health unavailable")
            }
        }
        if !reminders.isEmpty {
            parts.append("\(reminders.count) reminder" + (reminders.count == 1 ? "" : "s"))
        }
        if !calendarEvents.isEmpty {
            parts.append("\(calendarEvents.count) event" + (calendarEvents.count == 1 ? "" : "s"))
        }
        if inboxPending > 0 {
            parts.append("\(inboxPending) unread")
        }
        return parts.joined(separator: " · ")
    }

    public func encode(to encoder: Encoder) throws {
        var root = encoder.container(keyedBy: CodingKeys.self)
        try root.encode(memoryEnabled, forKey: .memoryEnabled)
        try root.encode(memoryScope, forKey: .memoryScope)
        try root.encode(inboxPending, forKey: .inboxPending)
        try root.encodeIfPresent(hud, forKey: .hud)
        try root.encodeIfPresent(health, forKey: .health)
        try root.encode(reminders, forKey: .reminders)
        try root.encode(memories, forKey: .memories)
    }
}
