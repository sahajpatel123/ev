// Cycle EAC-43 — iPhone live event kind label.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieLiveEventKind: String, Sendable {
    case wake, focusChange = "focus_change", capture, digest

    public var label: String {
        switch self {
        case .wake: return "wake"
        case .focusChange: return "focus"
        case .capture: return "capture"
        case .digest: return "digest"
        }
    }
}
