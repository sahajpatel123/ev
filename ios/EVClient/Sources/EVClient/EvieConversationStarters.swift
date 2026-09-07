// Cycle EAC-26 — iPhone conversation starter catalog.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieConversationStarters: Sendable {
    public static let starters: [String] = [
        "What's on today?",
        "Remind me to stretch",
        "Summarize my week",
    ]

    public static func randomStarter() -> String {
        starters.first ?? "What's on today?"
    }
}
