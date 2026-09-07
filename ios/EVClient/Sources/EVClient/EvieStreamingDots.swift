// Cycle EAC-45 — iPhone streaming indicator state.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieStreamingDots: String, Sendable {
    case idle, thinking, streaming

    public var accessibilityLabel: String {
        switch self {
        case .idle: return "idle"
        case .thinking: return "EV thinking"
        case .streaming: return "EV replying"
        }
    }
}
