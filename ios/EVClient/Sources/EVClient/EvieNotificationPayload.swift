/// iPhone-only (EAC97): gateway inbox payload model.
///
/// (Names EvieInbox* to avoid clashing with the existing EvieNotificationRow
/// used by the native notification inbox UI.)
///
/// Decodes GET /v1/device-gateway/inbox (poll channel) into typed rows
/// with kind-aware summaries for widgets/complications.

import Foundation

public struct EvieInboxRow: Codable, Equatable, Sendable {
    public var id: String
    public var kind: String?
    public var title: String
    public var body: String
    public var createdAt: String?
    public var unread: Bool

    public init(id: String, kind: String?, title: String, body: String, createdAt: String?, unread: Bool) {
        self.id = id
        self.kind = kind
        self.title = title
        self.body = body
        self.createdAt = createdAt
        self.unread = unread
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case kind
        case title
        case body
        case createdAt = "created_at"
        case unread
    }
}

public struct EvieInboxPayload: Codable, Equatable, Sendable {
    public var items: [EvieInboxRow]
    public var pushDelivery: String
    public var pushRegistered: Bool

    public init(items: [EvieInboxRow], pushDelivery: String, pushRegistered: Bool) {
        self.items = items
        self.pushDelivery = pushDelivery
        self.pushRegistered = pushRegistered
    }

    private enum CodingKeys: String, CodingKey {
        case items
        case pushDelivery = "push_delivery"
        case pushRegistered = "push_registered"
    }

    public var unreadCount: Int {
        items.filter { $0.unread }.count
    }

    /// "3 unread · poll" style line; delivery mode only when registered.
    public func renderSummary() -> String {
        var parts: [String] = []
        let unread = unreadCount
        if unread > 0 {
            parts.append("\(unread) unread")
        }
        if pushRegistered && pushDelivery == "apns" {
            parts.append("push on")
        } else {
            parts.append("poll")
        }
        return parts.isEmpty ? "clear" : parts.joined(separator: " · ")
    }
}
