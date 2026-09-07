// Cycle EAC-56 — iPhone sleep band mapper.
/// iPhone-only additive helper; backward compatible (new type, no existing API touched).
import Foundation

public enum EvieSleepBand: String, Sendable {
    case unknown, short, okay, solid

    public static func band(hours: Double?) -> EvieSleepBand {
        guard let hours, hours >= 0 else { return .unknown }
        if hours < 5 { return .short }
        if hours < 7 { return .okay }
        return .solid
    }
}
