// Cycle EAC-59 — iPhone tab title catalog.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieTabTitle: String, Sendable, CaseIterable {
    case today, chat, capture, memory, voice

    public var title: String {
        switch self {
        case .today: return "Today"
        case .chat: return "Chat"
        case .capture: return "Capture"
        case .memory: return "Memory"
        case .voice: return "Voice"
        }
    }
}
