// Cycle EAC-42 — iPhone runtime listener status.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieRuntimeStatus: String, Sendable {
    case listening, verifying, speaking, offline

    public var displayLine: String {
        switch self {
        case .listening: return "EV listening"
        case .verifying: return "EV checking"
        case .speaking: return "EV speaking"
        case .offline: return "EV offline"
        }
    }
}
