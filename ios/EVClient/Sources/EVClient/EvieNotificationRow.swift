// Cycle EAC-17 — iPhone notification inbox row.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Value model for the APNs inbox list on both iPhones: one row per
/// notification with read tracking as a copy (structs stay value-semantic for
/// SwiftUI). The real UNUserNotificationCenter / push-token path is untouched;
/// this is display state only. No backend change.

import Foundation

public struct EvieNotificationRow: Equatable, Sendable, Identifiable {
    public var id: String
    public var title: String
    public var body: String
    public var receivedAt: String
    public var isRead: Bool

    public init(
        id: String,
        title: String,
        body: String,
        receivedAt: String = "",
        isRead: Bool = false
    ) {
        self.id = id
        self.title = title
        self.body = body
        self.receivedAt = receivedAt
        self.isRead = isRead
    }

    /// Unread count helper for the inbox badge.
    public static func unreadCount(_ rows: [EvieNotificationRow]) -> Int {
        rows.filter { !$0.isRead }.count
    }

    /// Returns a read copy, leaving the original untouched.
    public func markedRead() -> EvieNotificationRow {
        var copy = self
        copy.isRead = true
        return copy
    }

    /// One-line row preview. Example: "EV · digest ready".
    public func previewLine() -> String {
        let t = title.trimmingCharacters(in: .whitespacesAndNewlines)
        let b = body.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.isEmpty { return String(b.prefix(80)) }
        if b.isEmpty { return t }
        return "\(t) · \(String(b.prefix(60)))"
    }
}
