// Cycle EAC-72 — iPhone reminder item model.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
///
/// Reminder state is derived from the server-provided due date and completion
/// bit.  The phone never invents a due time and never treats a display row as
/// proof that a reminder was delivered.
import Foundation

public struct EvieReminderItem: Codable, Equatable, Sendable, Identifiable {
    public enum State: String, Codable, Sendable, CaseIterable {
        case pending
        case overdue
        case completed
        case undated
    }

    public let id: String
    public let title: String
    public let dueAt: Date?
    public let completed: Bool

    public init(
        id: String,
        title: String,
        dueAt: Date? = nil,
        completed: Bool = false
    ) {
        self.id = id
        self.title = title.trimmingCharacters(in: .whitespacesAndNewlines)
        self.dueAt = dueAt
        self.completed = completed
    }

    public func state(at now: Date = Date()) -> State {
        if completed { return .completed }
        guard let dueAt else { return .undated }
        return dueAt < now ? .overdue : .pending
    }

    public func isActionable(at now: Date = Date()) -> Bool {
        state(at: now) == .pending || state(at: now) == .overdue
    }

    public var displayTitle: String {
        title.isEmpty ? "Untitled reminder" : title
    }

    public static func dueFirst(_ items: [EvieReminderItem]) -> [EvieReminderItem] {
        items.sorted { left, right in
            switch (left.dueAt, right.dueAt) {
            case let (leftDate?, rightDate?):
                if leftDate != rightDate { return leftDate < rightDate }
            case (_?, nil):
                return true
            case (nil, _?):
                return false
            case (nil, nil):
                break
            }
            return left.id < right.id
        }
    }
}
