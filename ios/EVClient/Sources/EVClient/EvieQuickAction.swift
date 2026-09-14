// Cycle EAC-32 — iPhone quick-action catalog.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public struct EvieQuickAction: Equatable, Sendable, Identifiable {
    public var id: String
    public var title: String
    public var systemImage: String

    public init(id: String, title: String, systemImage: String) {
        self.id = id
        self.title = title
        self.systemImage = systemImage
    }

    public static func defaults() -> [EvieQuickAction] {
        [
            EvieQuickAction(id: "capture", title: "Capture", systemImage: "plus.circle"),
            EvieQuickAction(id: "ask", title: "Ask", systemImage: "bubble.left"),
            EvieQuickAction(id: "call", title: "Call", systemImage: "phone"),
            EvieQuickAction(id: "message", title: "Message", systemImage: "message"),
        ]
    }
}
